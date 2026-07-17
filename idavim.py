"""idavim - vim-style keyboard navigation for IDA Pro.

Provides vim-like navigation (hjkl, f, /, n, */#, u/d half-page scrolling,
gg/G, counts, word motions, cw rename, m/` marks, yy address copy) in the
disassembly and Hex-Rays pseudocode views, similar to Vimium / IdeaVim.

Keys are intercepted with an application-level Qt event filter so they take
priority over IDA's own single-key shortcuts (n, d, u, g, ...) while idavim
is enabled. Press Shift+Esc to toggle idavim off (all keys go to IDA) and
back on.
"""

import bisect
import logging
import re

import ida_bytes
import ida_ida
import ida_idaapi
import ida_kernwin
import ida_lines
import ida_moves
import ida_name

try:
    import ida_hexrays

    HAS_HEXRAYS = True
except ImportError:
    HAS_HEXRAYS = False

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets

logger = logging.getLogger("idavim")

Qt = QtCore.Qt
QEvent = QtCore.QEvent

# widget types where vim navigation is active
VIM_WIDGET_TYPES = (
    ida_kernwin.BWN_DISASM,
    ida_kernwin.BWN_PSEUDOCODE,
)

# work budget for one `/`/n/N command in the disassembly view: total item
# heads walked, shared across a count's repeats — the work is capped, not
# the count (the pseudocode view searches all lines and wraps instead)
DISASM_SEARCH_LIMIT = 50000

# cap for one counted motion that does real work per repeat: w/e/b
# iterations and the disassembly head-walk of j/k/d/u. A runaway count
# (held-down digit key) must not freeze the UI; the cap is reported, never
# silent. Pseudocode j/k/gg/G and {n}n/N are exact arithmetic per command
# and stay uncapped.
MOTION_LIMIT = 10000

# characters handled while idavim is enabled (without a pending prefix key).
# `i` is deliberately absent: it is IDA's "insert comment line" in the
# disassembly view. `c` shadows IDA's MakeCode while enabled, `*` MakeArray,
# `#` OpNumber, `m` OpEnum and `y` SetType — the same trade as n/d/u/g
# (toggle idavim off to use them). The backtick is unbound in IDA.
VIM_KEYS = set("hjklGgcudfFwbe0$^;,/nN*#123456789m`y")

# keys intercepted only in the pseudocode view: in the disassembly view ':'
# is IDA's "enter comment" (and there are no line numbers to jump to). The
# gate lives in _in_vim_context (where the widget type is already resolved,
# keeping _wants free of IDA API calls) — see it for the pending-prefix
# exception.
PSEUDOCODE_ONLY_KEYS = set(":")

# per-view rename action invoked by cw (rename the item under the cursor)
HX_RENAME_ACTION = "hx:Rename"
DISASM_RENAME_ACTION = "MakeName"

# bare modifier presses must not cancel a pending command (Shift is held
# while typing an uppercase or symbol target for f/F)
MODIFIER_KEYS = (
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Meta,
    Qt.Key.Key_Alt,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_CapsLock,
)

WORD_RE = re.compile(r"\w+|[^\w\s]+")

# what */# consider an identifier: vim's default iskeyword (\w), which is
# exactly the C identifier class the listing views render
IDENT_RE = re.compile(r"\w+")

# m{a-z}/`{a-z} marks are stored as IDA bookmarks (ida_moves.bookmarks_t) so
# they live in the database and show up in IDA's Bookmarks widget. A mark
# remembers only an ea (breakpoint-style granularity): jumpto(ea) resolves
# in whichever view is current, so marks cross the disassembly/pseudocode
# boundary for free. Bookmarks have no letter field, so the letter is
# encoded in the description ("idavim: a"); marks are looked up by
# description match, never by stored index — deleting a bookmark in the
# widget renumbers the rest.
MARK_LETTERS = set("abcdefghijklmnopqrstuvwxyz")

# not exposed by ida_moves: bookmarks_t.mark() returns this on failure. Never
# pass it as the index to append — that is a silent no-op, not an append; a
# real append passes index = len(bookmarks_t(viewer)).
BOOKMARKS_BAD_INDEX = 0xFFFFFFFF


def _plain_curline(viewer):
    line = ida_kernwin.get_custom_viewer_curline(viewer, False)
    if line is None:
        return ""
    return ida_lines.tag_remove(line)


class VimEventFilter(QtCore.QObject):
    """Application-level event filter implementing the vim key handling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enabled = True
        self.pending = ""  # "f"/"F" (find char), "g" (gg), "c" (cw), "m"/"`" (marks), "y" (yy)
        self.pending_count = 0  # count typed before the pending prefix (3gg)
        self.count = ""  # accumulated count prefix, e.g. "12" for 12j
        self.last_find = None  # (cmd, char) for ; and ,
        self.search = ""  # last search pattern (/ or */#) for n and N
        self.search_re = None  # compiled form; whole-word for */#
        self.search_key = ""  # casefolded prefilter key (see _set_search)
        self._synthesizing = False

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    def toggle(self):
        self._set_enabled(not self.enabled)

    def _set_enabled(self, enabled):
        self.enabled = enabled
        self._reset_pending()
        if enabled:
            ida_kernwin.msg("[idavim] enabled\n")
        else:
            ida_kernwin.msg("[idavim] disabled - IDA keys active (Shift+Esc to re-enable)\n")

    def _reset_pending(self):
        self.pending = ""
        self.pending_count = 0
        self.count = ""

    def _on_focus_changed(self, _old, _new):
        # moving focus abandons a half-typed command: a pending f/F/g/c
        # target or count prefix must not hijack a key pressed much later.
        # The completed-command state (last_find, search) is kept.
        self._reset_pending()

    def _take_count(self):
        try:
            count = max(1, int(self.count))
        except ValueError:
            count = 1
        self.count = ""
        return count

    def _capped_count(self, count):
        """Clamp a per-repeat-work motion's count to MOTION_LIMIT, with a
        message — a silent cap lands motions short (a known bug class)."""
        if count > MOTION_LIMIT:
            ida_kernwin.msg(f"[idavim] count capped at {MOTION_LIMIT}\n")
            return MOTION_LIMIT
        return count

    # ------------------------------------------------------------------ #
    # event filtering
    # ------------------------------------------------------------------ #

    def eventFilter(self, obj, event):
        if self._synthesizing:
            return False

        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            # a click moves the caret or opens a context menu; popups take
            # no focus, so without this the half-typed state would survive
            # the menu and hijack a key pressed much later
            if self.pending or self.count:
                self._reset_pending()
            return False
        if etype not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False

        try:
            # cheap checks first: _wants makes NO IDA API calls (it reads
            # the enabled flag, the Qt event, the pending state — which it
            # clears for keys that cannot complete a pending command — and
            # last_find), so the IDA API probe in _in_vim_context runs only
            # for keys idavim might actually claim
            wanted = self._wants(event) and self._in_vim_context(event)

            if logger.isEnabledFor(logging.DEBUG) and etype == QEvent.Type.KeyPress:
                logger.debug(
                    "key=0x%x text=%r enabled=%s pending=%r wanted=%s",
                    event.key(), event.text(), self.enabled, self.pending,
                    wanted,
                )

            if not wanted:
                return False

            if etype == QEvent.Type.ShortcutOverride:
                # accepting the override suppresses IDA's shortcut for this
                # key, so it is redelivered to the widget as a KeyPress
                event.accept()
                return True

            return self._handle(event)
        except Exception:
            logger.exception("idavim key handling failed")
            if self.pending:
                # fail closed while a prefix is pending: an unaccepted
                # override would fire IDA's shortcut for the target key
                # (fc → MakeCode); swallow one key and reset instead
                self._reset_pending()
                event.accept()
                return True
            return False

    def _in_vim_context(self, event):
        app = QtWidgets.QApplication
        if app.activeModalWidget() is not None:
            return False

        # popups (context menus) are neither modal nor focus-taking: keys
        # are delivered to them via the popup grab while focusWidget still
        # reports the viewer, so without this check idavim would consume
        # the menu's type-ahead/accelerator keys
        if app.activePopupWidget() is not None:
            return False

        focus = app.focusWidget()
        if focus is None:
            return False

        # never steal keys from text inputs (rename boxes, CLI, filters, ...)
        if isinstance(
            focus,
            (
                QtWidgets.QLineEdit,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QComboBox,
                QtWidgets.QAbstractSpinBox,
            ),
        ):
            return False
        if isinstance(focus.window(), QtWidgets.QDialog):
            return False

        widget = ida_kernwin.get_current_widget()
        if widget is None:
            return False
        widget_type = ida_kernwin.get_widget_type(widget)
        if widget_type not in VIM_WIDGET_TYPES:
            return False

        # pseudocode-only keys stay with IDA everywhere else — but never
        # while a prefix is pending: the key is then an f/F target (or a
        # cancel) and must be consumed, not leaked to IDA ("f:" in the
        # disassembly view must find ':', not open the comment prompt)
        if (
            not self.pending
            and event.text() in PSEUDOCODE_ONLY_KEYS
            and widget_type != ida_kernwin.BWN_PSEUDOCODE
        ):
            return False

        # stay out of graph (and proximity) mode: line-oriented motions make
        # no sense there, so leave every key to IDA
        if widget_type == ida_kernwin.BWN_DISASM:
            viewer = ida_kernwin.get_current_viewer()
            if viewer is not None and ida_kernwin.get_view_renderer_type(
                viewer
            ) != ida_kernwin.TCCRT_FLAT:
                return False

        return True

    def _wants(self, event):
        # while disabled nothing is intercepted; the toggle itself is a
        # registered IDA action ("idavim:toggle", Shift-Esc), not a filter
        # key. This check comes first so a disabled idavim does no per-key
        # work at all.
        if not self.enabled:
            return False

        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        meta = bool(mods & Qt.KeyboardModifier.MetaModifier)

        text = event.text()
        if ctrl or alt or meta or not text:
            # chords and text-less keys (arrows, PgUp/PgDn, Home/End,
            # F-keys) cannot complete a pending command or extend a count;
            # abandon the half-typed state so it doesn't hijack a later
            # plain key — while the key still reaches IDA. Bare modifier
            # presses excepted: Shift is held while typing an uppercase
            # f-target.
            if event.key() not in MODIFIER_KEYS:
                self._reset_pending()
            return False

        if self.pending:
            if text.isprintable():
                # printable keys resolve the pending command (f/F target, gg)
                return True
            if event.key() == Qt.Key.Key_Escape and not bool(
                mods & Qt.KeyboardModifier.ShiftModifier
            ):
                # plain Esc cancels the pending command like vim, without
                # also triggering IDA's Esc (navigate back)
                return True
            # anything else (Shift+Esc toggle, Enter, Tab, ...) abandons the
            # pending command but must still reach IDA
            self._reset_pending()
            return False

        if text in ";," and self.last_find is None:
            return False  # keep IDA's `;` (comment) until f/F has been used

        return text in VIM_KEYS or text in PSEUDOCODE_ONLY_KEYS

    # ------------------------------------------------------------------ #
    # key dispatch
    # ------------------------------------------------------------------ #

    def _handle(self, event):
        # in Qt6 key events can be delivered to the top-level QWindow rather
        # than the widget, so always resolve the real focus widget ourselves
        widget = QtWidgets.QApplication.focusWidget()
        if widget is None:
            return False

        char = event.text()

        if self.pending:
            cmd, self.pending = self.pending, ""
            target_line, self.pending_count = self.pending_count, 0
            if cmd in ("f", "F") and char.isprintable():
                self.last_find = (cmd, char)
                # always 1: counts deliberately do not compose with f/F
                # (see the CLAUDE.md known limitations)
                self._find_in_line(cmd, char, 1)
            elif cmd == "g" and char == "g":
                if target_line:
                    self._goto_line(target_line, self._goto_top)
                else:
                    self._goto_top()
            elif cmd == "c" and char == "w":
                # cw: rename the identifier under the cursor, vim's
                # "change word" mapped onto IDA's semantic rename
                QtCore.QTimer.singleShot(0, self._rename_under_cursor)
            elif cmd == "m" and char in MARK_LETTERS:
                self._set_mark(char)
            elif cmd == "`" and char in MARK_LETTERS:
                self._jump_mark(char)
            elif cmd == "y" and char == "y":
                self._yank_address()
            # anything else cancels the pending command (vim-like)
            return True

        if char.isdigit() and (char != "0" or self.count):
            self.count += char
            return True

        has_count = bool(self.count)
        count = self._take_count()

        if char == "h":
            self._move_in_line(-count)
        elif char == "j":
            self._move_lines(count, 1)
        elif char == "k":
            self._move_lines(count, -1)
        elif char == "l":
            self._move_in_line(count)
        elif char == "d":
            self._move_lines(count * self._half_page(widget), 1)
        elif char == "u":
            self._move_lines(count * self._half_page(widget), -1)
        elif char == "g":
            self.pending = "g"
            self.pending_count = count if has_count else 0
        elif char == "G":
            if has_count:
                self._goto_line(count, self._goto_bottom)
            else:
                self._goto_bottom()
        elif char == "0":
            self._goto_line_start()
        elif char == "$":
            self._goto_line_end()
        elif char == "^":
            self._goto_first_nonblank()
        elif char == "w":
            self._word_forward(widget, self._capped_count(count))
        elif char == "e":
            self._word_end(widget, self._capped_count(count))
        elif char == "b":
            self._word_backward(widget, self._capped_count(count))
        elif char in ("f", "F"):
            self.pending = char
        elif char == "c":
            self.pending = "c"
        elif char in ("m", "`", "y"):
            self.pending = char
        elif char in (";", ","):
            self._repeat_find(char, count)
        elif char == "/":
            # open the prompt after the key event is fully processed
            QtCore.QTimer.singleShot(0, self._prompt_search)
        elif char == ":":
            QtCore.QTimer.singleShot(0, self._prompt_goto_line)
        elif char == "n":
            self._search_step(1, count)
        elif char == "N":
            self._search_step(-1, count)
        elif char in ("*", "#"):
            self._search_word(1 if char == "*" else -1, count)

        return True

    # ------------------------------------------------------------------ #
    # key synthesis and vertical motions: j k d u
    # ------------------------------------------------------------------ #

    def _send_key(self, widget, key, times=1):
        mods = Qt.KeyboardModifier.NoModifier
        self._synthesizing = True
        try:
            for _ in range(times):
                press = QtGui.QKeyEvent(QEvent.Type.KeyPress, key, mods)
                release = QtGui.QKeyEvent(QEvent.Type.KeyRelease, key, mods)
                QtWidgets.QApplication.sendEvent(widget, press)
                QtWidgets.QApplication.sendEvent(widget, release)
        finally:
            self._synthesizing = False

    def _move_lines(self, count, direction):
        """Move the cursor `count` lines down (direction=1) or up (-1).

        Jumps straight to the computed target: exact line arithmetic in the
        pseudocode view, item heads in the disassembly view (an item that
        renders as several display lines counts as one, which is fine for
        rough vim-style movement).
        """
        result = self._viewer_place()
        if result is None:
            return
        place, x, y = result

        if self._is_pseudocode():
            total = self._pseudocode_size()
            cur = ida_kernwin.place_t_as_simpleline_place_t(place)
            if not total or cur is None:
                return  # decompilation not ready yet
            target = min(max(cur.n + direction * count, 0), total - 1)
            self._jump_pseudocode_line(target, x, y)
            return

        cur = ida_kernwin.place_t_as_idaplace_t(place)
        if cur is None:
            return
        ea = cur.ea
        min_ea = ida_ida.inf_get_min_ea()
        max_ea = ida_ida.inf_get_max_ea()
        # each repeat is an IDA head-walk (unlike the pseudocode branch's
        # O(1) arithmetic above), so a runaway count is capped here
        for _ in range(self._capped_count(count)):
            if direction > 0:
                nxt = ida_bytes.next_head(ea, max_ea)
            else:
                nxt = ida_bytes.prev_head(ea, min_ea)
            if nxt == ida_idaapi.BADADDR:
                break
            ea = nxt
        # plain movement must not pollute the Esc navigation history
        ida_kernwin.jumpto(ea, -1, ida_kernwin.UIJMP_DONTPUSH)

    def _half_page(self, widget):
        try:
            line_height = widget.fontMetrics().height() or 16
        except AttributeError:
            line_height = 16
        return max(1, widget.height() // line_height // 2)

    # ------------------------------------------------------------------ #
    # place helpers
    # ------------------------------------------------------------------ #

    def _viewer_place(self):
        """Returns (place, x, y) or None — _viewer_ctx without the line
        text, for callers that don't need it (the j/k hot path and the
        search entry points skip the curline fetch and tag strip)."""
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return None
        return ida_kernwin.get_custom_viewer_place(viewer, False) or None

    def _viewer_ctx(self):
        """Returns (plain line text, place, x, y) or None."""
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return None
        result = ida_kernwin.get_custom_viewer_place(viewer, False)
        if not result:
            return None
        place, x, y = result
        return _plain_curline(viewer), place, x, y

    def _jump_to_column(self, target_x):
        """Move the caret to target_x within the current line by
        synthesizing native Left/Right key events.

        jumpto for a horizontal move runs IDA's full navigation (item
        highlight and address-sync recomputation) whose cost depends on the
        token under the target column, so identical motions feel fast or
        slow depending on where they land — the same lag that made h/l use
        synthetic keys. Native caret movement is uniformly cheap.

        The caret position is read here, right before moving, so callers
        cannot pass a stale position; both endpoints lie within the current
        line, which also bounds the number of synthesized events."""
        widget = QtWidgets.QApplication.focusWidget()
        if widget is None:
            return
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return
        result = ida_kernwin.get_custom_viewer_place(viewer, False)
        if not result:
            return
        _place, cur_x, _y = result
        dx = target_x - cur_x
        if dx > 0:
            self._send_key(widget, Qt.Key.Key_Right, dx)
        elif dx < 0:
            self._send_key(widget, Qt.Key.Key_Left, -dx)

    def _is_pseudocode(self):
        """True when the current widget is a pseudocode view — regardless of
        whether its decompilation result is available yet."""
        widget = ida_kernwin.get_current_widget()
        return (
            widget is not None
            and ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE
        )

    def _pseudocode_vdui(self):
        """The current pseudocode view's vdui with a ready cfunc, or None
        while the decompilation result is not available (view loading)."""
        if not HAS_HEXRAYS:
            return None
        widget = ida_kernwin.get_current_widget()
        if ida_kernwin.get_widget_type(widget) != ida_kernwin.BWN_PSEUDOCODE:
            return None
        vdui = ida_hexrays.get_widget_vdui(widget)
        if vdui is None or vdui.cfunc is None:
            return None
        return vdui

    def _pseudocode_size(self):
        """Number of pseudocode lines without copying any text — cheap
        enough for per-keypress use (j/k/gg/G clamping)."""
        vdui = self._pseudocode_vdui()
        return vdui.cfunc.get_pseudocode().size() if vdui is not None else 0

    def _pseudocode_lines(self):
        """Plain-text pseudocode lines, or None while unavailable. Builds a
        full tag-stripped copy of the function — do NOT call per keypress;
        use _pseudocode_size() when only the line count is needed."""
        vdui = self._pseudocode_vdui()
        if vdui is None:
            return None
        return [ida_lines.tag_remove(sl.line) for sl in vdui.cfunc.get_pseudocode()]

    def _jump_pseudocode_line(self, lineno, x=0, y=0):
        # simpleline_place_t cannot be constructed directly (abstract in the
        # bindings); clone the viewer's current place and retarget it instead
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return  # deferred prompt paths (:{n}, /) run outside eventFilter's except
        result = ida_kernwin.get_custom_viewer_place(viewer, False)
        if not result:
            return
        place, _x, _y = result
        target = ida_kernwin.place_t_as_simpleline_place_t(place.clone())
        if target is None:
            return
        target.n = lineno
        ida_kernwin.jumpto(viewer, target, x, y)
        self._nudge_sync(lineno)

    def _nudge_sync(self, lineno):
        """Native Down+Up pair (net movement: zero) after a programmatic
        jump: IDA's "Synchronize with" follows only the real key input
        path, so the jump alone leaves synced views unscrolled. The nudge
        replays the input IDA listens to."""
        widget = QtWidgets.QApplication.focusWidget()
        if widget is None:
            return
        total = self._pseudocode_size()
        if total <= 1:
            return
        if lineno >= total - 1:
            keys = (Qt.Key.Key_Up, Qt.Key.Key_Down)
        else:
            keys = (Qt.Key.Key_Down, Qt.Key.Key_Up)
        for key in keys:
            self._send_key(widget, key)

    # ------------------------------------------------------------------ #
    # gg / G
    # ------------------------------------------------------------------ #

    def _goto_top(self):
        if self._is_pseudocode():
            if self._pseudocode_size():
                self._jump_pseudocode_line(0)
            return  # decompilation not ready yet: do nothing
        ida_kernwin.jumpto(ida_ida.inf_get_min_ea())

    def _goto_line(self, lineno, fallback=None):
        """{n}gg / {n}G / :{n}: jump to 1-based line `lineno` in the
        pseudocode view. The disassembly listing has no line numbers, so
        counts fall back to the plain gg/G behavior there."""
        if not self._is_pseudocode():
            if fallback is not None:
                fallback()
            return
        total = self._pseudocode_size()
        if not total:
            return  # decompilation not ready yet
        self._jump_pseudocode_line(min(max(lineno - 1, 0), total - 1))

    def _goto_bottom(self):
        if self._is_pseudocode():
            total = self._pseudocode_size()
            if total:
                self._jump_pseudocode_line(total - 1)
            return  # decompilation not ready yet: do nothing
        last = ida_bytes.prev_head(ida_ida.inf_get_max_ea(), ida_ida.inf_get_min_ea())
        if last != ida_idaapi.BADADDR:
            ida_kernwin.jumpto(last)

    # ------------------------------------------------------------------ #
    # in-line motions: h l f F ; , 0 ^ $ w e b
    # ------------------------------------------------------------------ #

    def _move_in_line(self, dx):
        """h/l: move the caret dx columns, clamped to the current line —
        the same compute-a-column-and-jump shape as every other in-line
        motion, so no count cap is needed (the burst is line-bounded)."""
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        text, _place, x, _y = ctx
        end = max(0, len(text.rstrip()) - 1)
        self._jump_to_column(min(max(x + dx, 0), end))

    def _find_in_line(self, cmd, char, count):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        text, _place, x, _y = ctx

        pos = x
        for _ in range(count):
            if cmd == "f":
                pos = text.find(char, pos + 1)
            else:
                pos = text.rfind(char, 0, max(0, pos))
            if pos < 0:
                ida_kernwin.msg(f"[idavim] {char!r} not found in line\n")
                return
        self._jump_to_column(pos)

    def _repeat_find(self, char, count):
        # last_find is never None here: _wants leaves ;/, with IDA until
        # f/F has set it, and nothing resets it back to None
        cmd, target = self.last_find
        if char == ",":
            cmd = "F" if cmd == "f" else "f"
        self._find_in_line(cmd, target, count)

    def _goto_line_start(self):
        self._jump_to_column(0)

    def _goto_line_end(self):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        text, _place, _x, _y = ctx
        self._jump_to_column(max(0, len(text.rstrip()) - 1))

    def _goto_first_nonblank(self):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        text, _place, _x, _y = ctx
        stripped = text.lstrip()
        self._jump_to_column(len(text) - len(stripped) if stripped else 0)

    def _word_motion(self, widget, count, pos, forward):
        """Shared w/e/b loop: step to the next/previous word position on
        the current line, wrapping one line (Down+Home / Up+End) and
        rescanning when the line runs out. `pos` extracts the column of a
        WORD_RE match (start for w/b, end-1 for e)."""
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        text, _place, x, _y = ctx

        for _ in range(count):
            cands = [p for p in (pos(m) for m in WORD_RE.finditer(text))
                     if (p > x if forward else p < x)]
            if not cands:
                # wrap to the first word of the next line / the last word
                # of the previous line
                if forward:
                    self._send_key(widget, Qt.Key.Key_Down)
                    self._send_key(widget, Qt.Key.Key_Home)
                else:
                    self._send_key(widget, Qt.Key.Key_Up)
                    self._send_key(widget, Qt.Key.Key_End)
                ctx = self._viewer_ctx()
                if ctx is None:
                    return
                text, _place, x, _y = ctx
                cands = [pos(m) for m in WORD_RE.finditer(text)]
                if not cands:
                    continue  # blank line: keep the fresh caret column
            x = cands[0] if forward else cands[-1]
        self._jump_to_column(x)

    def _word_forward(self, widget, count):
        self._word_motion(widget, count, lambda m: m.start(), forward=True)

    def _word_end(self, widget, count):
        self._word_motion(widget, count, lambda m: m.end() - 1, forward=True)

    def _word_backward(self, widget, count):
        self._word_motion(widget, count, lambda m: m.start(), forward=False)

    # ------------------------------------------------------------------ #
    # deferred prompts and actions: cw : yy
    # ------------------------------------------------------------------ #

    def _yank_address(self):
        """yy: copy the current line's address to the clipboard —
        vim's "yank line" mapped onto what a listing line is to an
        analyst, its ea."""
        result = self._viewer_place()
        if result is None:
            return
        ea = result[0].toea()
        if ea == ida_idaapi.BADADDR:
            ida_kernwin.msg("[idavim] no address here to copy\n")
            return
        QtWidgets.QApplication.clipboard().setText(f"{ea:#x}")
        ida_kernwin.msg(f"[idavim] copied {ea:#x}\n")

    def _rename_under_cursor(self):
        action = (
            HX_RENAME_ACTION if self._is_pseudocode() else DISASM_RENAME_ACTION
        )
        if not ida_kernwin.process_ui_action(action):
            ida_kernwin.msg("[idavim] nothing to rename here\n")

    def _prompt_goto_line(self):
        value = ida_kernwin.ask_long(1, "idavim: go to line")
        if value is not None and value > 0:
            self._goto_line(value)

    # ------------------------------------------------------------------ #
    # marks: m{a-z} `{a-z}
    # ------------------------------------------------------------------ #

    def _find_mark(self, viewer, char):
        """(index, lochist_entry_t) of the bookmark holding mark `char`,
        or None. Always a fresh description scan: bookmark indices are
        renumbered when one is deleted in the Bookmarks widget, so a
        stored index cannot be trusted."""
        target = f"idavim: {char}"
        for index, (entry, desc) in enumerate(ida_moves.bookmarks_t(viewer)):
            if desc == target:
                return index, entry
        return None

    def _set_mark(self, char):
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return
        loc = ida_moves.lochist_entry_t()
        if not ida_kernwin.get_custom_viewer_location(loc, viewer):
            return
        ea = loc.place().toea()
        if ea == ida_idaapi.BADADDR:
            ida_kernwin.msg(f"[idavim] cannot set mark {char!r}: no address here\n")
            return

        # normalize the place to idaplace_t: bookmark storage is split per
        # place class, and marking the pseudocode's own place "succeeds"
        # into a storage the Bookmarks widget and bookmarks_t(viewer) never
        # read. idaplace_t is abstract (and the class template must not be
        # mutated in place), so clone the template and retarget the clone.
        template = ida_kernwin.get_place_class_template(
            ida_kernwin.get_place_class_id("idaplace_t")
        )
        ida_place = ida_kernwin.place_t_as_idaplace_t(template.clone())
        ida_place.ea = ea
        ida_place.lnnum = 0
        loc.set_place(ida_place)

        found = self._find_mark(viewer, char)
        if found is not None:
            index = found[0]  # remarking a letter overwrites its slot
        else:
            index = len(ida_moves.bookmarks_t(viewer))
        if (
            ida_moves.bookmarks_t.mark(loc, index, None, f"idavim: {char}", None)
            == BOOKMARKS_BAD_INDEX
        ):
            ida_kernwin.msg(f"[idavim] could not set mark {char!r}\n")
            return
        ida_kernwin.msg(f"[idavim] mark {char!r} set at {ea:#x}\n")

    def _jump_mark(self, char):
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return
        found = self._find_mark(viewer, char)
        if found is None:
            ida_kernwin.msg(f"[idavim] mark {char!r} not set\n")
            return
        # a mark is just its ea: one jumpto resolves it in whichever view
        # is current, landing on the line mapped to the address
        if not ida_kernwin.jumpto(found[1].place().toea()):
            return
        if self._is_pseudocode():
            # jumpto is programmatic, so nudge synced views to follow
            result = self._viewer_place()
            if result is None:
                return
            line_place = ida_kernwin.place_t_as_simpleline_place_t(result[0])
            if line_place is not None:
                self._nudge_sync(line_place.n)

    # ------------------------------------------------------------------ #
    # search: / n N * #
    # ------------------------------------------------------------------ #

    def _set_search(self, pattern, whole_word=False):
        """Set the pattern n/N step through: a case-insensitive substring
        for /, whole-word for */# (vim's \\<word\\> — `v1` must not stop
        on `v12`)."""
        self.search = pattern
        escaped = re.escape(pattern)
        if whole_word:
            escaped = r"\b" + escaped + r"\b"
        self.search_re = re.compile(escaped, re.IGNORECASE)
        # \b/IGNORECASE regexes lose sre's fast literal skip (measured 4-8x
        # slower over big functions), so the per-press pseudocode scan first
        # rejects lines with a casefolded substring test — a strict superset
        # of the regex's matches (casefold, not lower: ς/σ, µ/μ)
        self.search_key = pattern.casefold()

    def _prompt_search(self):
        pattern = ida_kernwin.ask_str(self.search, ida_kernwin.HIST_SRCH, "idavim search")
        if pattern:
            self._set_search(pattern)
            self._search_step(1, 1)

    def _search_word(self, direction, count):
        """*/#: search forward/backward for the identifier under the
        cursor. On whitespace/punctuation, the nearest identifier to the
        right on the current line is used (vim behavior)."""
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        word = None
        text, _place, x, _y = ctx
        for m in IDENT_RE.finditer(text):
            if m.end() > x:
                word = m
                break
        if word is None:
            ida_kernwin.msg("[idavim] no identifier under cursor\n")
            return
        self._set_search(word.group(), whole_word=True)
        # anchor at the identifier's start, not the caret: vim's # from
        # mid-word goes to the PREVIOUS occurrence (not the word's start),
        # and * from the whitespace left of a word skips the adjacent word
        self._search_step(direction, count, anchor_x=word.start())

    def _search_step(self, direction, count, anchor_x=None):
        if not self.search:
            ida_kernwin.msg("[idavim] no previous search (use /)\n")
            return
        if self._is_pseudocode():
            self._search_pseudocode(direction, count, anchor_x)
        else:
            self._search_disasm(direction, count)

    def _search_pseudocode(self, direction, count, anchor_x=None):
        """{count}n/N in the pseudocode view: collect every match position
        once, then step into the sorted list with modular index arithmetic.
        Any count is exact and costs one scan of the function plus a single
        jumpto, so counts need no cap here. anchor_x replaces the caret
        column as the stepping origin (*/# anchor at the identifier)."""
        lines = self._pseudocode_lines()
        if not lines:
            return  # decompilation not ready yet

        matches = []  # (line, column) of every occurrence, in text order
        for n, line in enumerate(lines):
            if self.search_key in line.casefold():
                matches.extend((n, m.start()) for m in self.search_re.finditer(line))
        if not matches:
            ida_kernwin.msg(f"[idavim] pattern not found: {self.search}\n")
            return

        result = self._viewer_place()
        if result is None:
            return
        place, x, y = result
        line_place = ida_kernwin.place_t_as_simpleline_place_t(place)
        if line_place is None:
            return
        # the count-th match strictly beyond the anchor, wrapping vim-style
        cur = (line_place.n, x if anchor_x is None else anchor_x)
        if direction > 0:
            idx = bisect.bisect_right(matches, cur) + count - 1
        else:
            idx = bisect.bisect_left(matches, cur) - count
        n, col = matches[idx % len(matches)]
        self._jump_pseudocode_line(n, col, y)

    def _search_disasm(self, direction, count):
        """{count}n/N in the disassembly view: walk item heads up to the
        count-th match, spending one shared DISASM_SEARCH_LIMIT budget for
        the whole command, and jumpto only the final landing spot."""
        result = self._viewer_place()
        if result is None:
            return
        place, _x, _y = result
        addr_place = ida_kernwin.place_t_as_idaplace_t(place)
        if addr_place is None:
            return
        ea = addr_place.ea
        min_ea = ida_ida.inf_get_min_ea()
        max_ea = ida_ida.inf_get_max_ea()

        target = None
        found = 0
        budget = DISASM_SEARCH_LIMIT
        while budget > 0 and found < count:
            budget -= 1
            if direction > 0:
                ea = ida_bytes.next_head(ea, max_ea)
            else:
                ea = ida_bytes.prev_head(ea, min_ea)
            if ea == ida_idaapi.BADADDR:
                break  # hit the edge of the listing (no wrap here)

            line = ida_lines.generate_disasm_line(ea, ida_lines.GENDSM_REMOVE_TAGS) or ""
            # the name is only consulted when the line itself doesn't match
            if self.search_re.search(line) or self.search_re.search(ida_name.get_name(ea) or ""):
                target = ea
                found += 1

        if target is not None:
            ida_kernwin.jumpto(target)
        if found >= count:
            return
        if budget <= 0:
            ida_kernwin.msg(
                f"[idavim] search stopped after {DISASM_SEARCH_LIMIT} items"
                f" (match {found} of {count})\n"
            )
        elif found:
            ida_kernwin.msg(
                f"[idavim] only {found} of {count} matches before the end of the listing\n"
            )
        else:
            ida_kernwin.msg(f"[idavim] pattern not found: {self.search}\n")


# The event filter and the toggle action are process-wide singletons. IDA may
# create a new plugmod per database without the previous one being torn down
# first; per-plugmod ownership would let a stale plugmod's teardown remove the
# filter or the action the live plugmod still uses. Reference counting keeps
# install/remove balanced for both.
ACTION_TOGGLE = "idavim:toggle"

_filter = None
_filter_refs = 0


def acquire_filter():
    global _filter, _filter_refs
    if _filter is None:
        _filter = VimEventFilter()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(_filter)
            app.focusChanged.connect(_filter._on_focus_changed)

        ida_kernwin.unregister_action(ACTION_TOGGLE)
        ida_kernwin.register_action(
            ida_kernwin.action_desc_t(
                ACTION_TOGGLE,
                "idavim: toggle",
                toggle_action_handler_t(_filter),
                "Shift-Esc",
                "Enable or disable idavim keyboard handling",
                -1,
            )
        )
    _filter_refs += 1
    return _filter


def release_filter():
    global _filter, _filter_refs
    _filter_refs -= 1
    if _filter_refs <= 0 and _filter is not None:
        ida_kernwin.unregister_action(ACTION_TOGGLE)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(_filter._on_focus_changed)
            except (RuntimeError, TypeError):
                pass  # already disconnected during teardown
            app.removeEventFilter(_filter)
        _filter = None
        _filter_refs = 0


class toggle_action_handler_t(ida_kernwin.action_handler_t):
    def __init__(self, event_filter, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_filter = event_filter

    def activate(self, ctx):
        self.event_filter.toggle()
        return 1

    def update(self, ctx):
        return ida_kernwin.AST_ENABLE_ALWAYS


class idavim_plugmod_t(ida_idaapi.plugmod_t):
    def __init__(self):
        self.event_filter = acquire_filter()
        logger.info("idavim loaded and enabled (Shift+Esc: toggle)")

    def run(self, arg):
        if self.event_filter is not None:
            self.event_filter.toggle()

    def __del__(self):
        try:
            if self.event_filter is not None:
                self.event_filter = None
                release_filter()
        except Exception:
            logger.exception("idavim teardown failed")


class idavim_plugin_t(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_MULTI
    comment = "vim-style navigation for disassembly and pseudocode views"
    help = "hjkl/u/d/gg/G/f/n vim navigation; Shift+Esc to toggle"
    wanted_name = "idavim"
    wanted_hotkey = ""

    def init(self):
        return idavim_plugmod_t()
