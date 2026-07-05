"""idavim - vim-style keyboard navigation for IDA Pro.

Provides modal vim-like navigation (hjkl, f, /, n, u/d half-page scrolling,
gg/G, counts, word motions) in the disassembly and Hex-Rays pseudocode views,
similar to Vimium / IdeaVim.

Keys are intercepted with an application-level Qt event filter so they take
priority over IDA's own single-key shortcuts (n, d, u, g, ...) while idavim
is enabled. Press `i` to disable idavim and use IDA's native keys; press
Shift+Esc to re-enable it.
"""

import logging
import re

import ida_bytes
import ida_ida
import ida_idaapi
import ida_kernwin
import ida_lines
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

# maximum number of item heads walked per `/` search step in the
# disassembly view (the pseudocode view searches all lines and wraps)
DISASM_SEARCH_LIMIT = 50000

# characters handled while idavim is enabled (without a pending prefix key)
VIM_KEYS = set("hjklGgudfFwbe0$^;,/nNi123456789")

WORD_RE = re.compile(r"\w+|[^\w\s]+")


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
        self.pending = ""  # "f", "F" (find char) or "g" (gg)
        self.count = ""  # accumulated count prefix, e.g. "12" for 12j
        self.last_find = None  # (cmd, char) for ; and ,
        self.search = ""  # last / pattern for n and N
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
        self.count = ""

    def _take_count(self):
        try:
            count = max(1, int(self.count))
        except ValueError:
            count = 1
        self.count = ""
        return count

    # ------------------------------------------------------------------ #
    # event filtering
    # ------------------------------------------------------------------ #

    def eventFilter(self, obj, event):
        if self._synthesizing:
            return False

        etype = event.type()
        if etype not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False

        try:
            in_context = self._in_vim_context()
            wanted = in_context and self._wants(event)

            if logger.isEnabledFor(logging.DEBUG) and etype == QEvent.Type.KeyPress:
                logger.debug(
                    "key=0x%x text=%r enabled=%s pending=%r in_context=%s wanted=%s",
                    event.key(), event.text(), self.enabled, self.pending,
                    in_context, wanted,
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
            return False

    def _in_vim_context(self):
        app = QtWidgets.QApplication
        if app.activeModalWidget() is not None:
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
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        meta = bool(mods & Qt.KeyboardModifier.MetaModifier)

        if not self.enabled:
            # only the "re-enable" chord is intercepted; a plain Esc stays
            # with IDA (navigate back) and Shift+Esc is unused by IDA
            if event.key() == Qt.Key.Key_Escape and bool(
                mods & Qt.KeyboardModifier.ShiftModifier
            ):
                return True
            return False

        if ctrl or alt or meta:
            return False

        text = event.text()
        if not text:
            return False

        if self.pending:
            # the next key always resolves (or cancels) the pending command
            return True

        if text in ";," and self.last_find is None:
            return False  # keep IDA's `;` (comment) until f/F has been used

        return text in VIM_KEYS

    # ------------------------------------------------------------------ #
    # key dispatch
    # ------------------------------------------------------------------ #

    def _handle(self, event):
        # in Qt6 key events can be delivered to the top-level QWindow rather
        # than the widget, so always resolve the real focus widget ourselves
        widget = QtWidgets.QApplication.focusWidget()
        if widget is None:
            return False

        if not self.enabled:
            self._set_enabled(True)
            return True

        char = event.text()

        if self.pending:
            cmd, self.pending = self.pending, ""
            count = self._take_count()
            if cmd in ("f", "F") and char.isprintable():
                self.last_find = (cmd, char)
                self._find_in_line(cmd, char, count)
            elif cmd == "g" and char == "g":
                self._goto_top()
            # anything else cancels the pending command (vim-like)
            return True

        if char.isdigit() and (char != "0" or self.count):
            self.count += char
            return True

        count = self._take_count()

        if char == "h":
            self._send_key(widget, Qt.Key.Key_Left, min(count, 128))
        elif char == "j":
            self._move_lines(count, 1)
        elif char == "k":
            self._move_lines(count, -1)
        elif char == "l":
            self._send_key(widget, Qt.Key.Key_Right, min(count, 128))
        elif char == "d":
            self._move_lines(count * self._half_page(widget), 1)
        elif char == "u":
            self._move_lines(count * self._half_page(widget), -1)
        elif char == "g":
            self.pending = "g"
        elif char == "G":
            self._goto_bottom()
        elif char == "0":
            self._goto_line_start()
        elif char == "$":
            self._goto_line_end()
        elif char == "^":
            self._goto_first_nonblank()
        elif char == "w":
            self._word_forward(widget, count)
        elif char == "e":
            self._word_end(widget, count)
        elif char == "b":
            self._word_backward(widget, count)
        elif char in ("f", "F"):
            self.pending = char
        elif char in (";", ","):
            self._repeat_find(char, count)
        elif char == "/":
            # open the prompt after the key event is fully processed
            QtCore.QTimer.singleShot(0, self._prompt_search)
        elif char == "n":
            self._search_step(1, count)
        elif char == "N":
            self._search_step(-1, count)
        elif char == "i":
            self._set_enabled(False)
        else:
            return False

        return True

    # ------------------------------------------------------------------ #
    # synthetic key motions
    # ------------------------------------------------------------------ #

    def _send_key(self, widget, key, times=1, mods=None):
        if mods is None:
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
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        _viewer, _text, place, x, y = ctx

        widget = ida_kernwin.get_current_widget()
        if ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE:
            lines = self._pseudocode_lines()
            if not lines:
                return
            cur = ida_kernwin.place_t_as_simpleline_place_t(place).n
            target = min(max(cur + direction * count, 0), len(lines) - 1)
            self._jump_pseudocode_line(target, x, y)
            return

        ea = ida_kernwin.place_t_as_idaplace_t(place).ea
        min_ea = ida_ida.inf_get_min_ea()
        max_ea = ida_ida.inf_get_max_ea()
        for _ in range(count):
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

    def _viewer_ctx(self):
        """Returns (viewer, plain line text, place, x, y) or None."""
        viewer = ida_kernwin.get_current_viewer()
        if viewer is None:
            return None
        result = ida_kernwin.get_custom_viewer_place(viewer, False)
        if not result:
            return None
        place, x, y = result
        return viewer, _plain_curline(viewer), place, x, y

    def _jump_to_column(self, viewer, place, x, y):
        ida_kernwin.jumpto(viewer, place, x, y)

    def _pseudocode_lines(self):
        """Plain-text pseudocode lines of the current vdui, or None."""
        if not HAS_HEXRAYS:
            return None
        widget = ida_kernwin.get_current_widget()
        if ida_kernwin.get_widget_type(widget) != ida_kernwin.BWN_PSEUDOCODE:
            return None
        vdui = ida_hexrays.get_widget_vdui(widget)
        if vdui is None or vdui.cfunc is None:
            return None
        return [ida_lines.tag_remove(sl.line) for sl in vdui.cfunc.get_pseudocode()]

    def _jump_pseudocode_line(self, lineno, x=0, y=0):
        # simpleline_place_t cannot be constructed directly (abstract in the
        # bindings); clone the viewer's current place and retarget it instead
        viewer = ida_kernwin.get_current_viewer()
        result = ida_kernwin.get_custom_viewer_place(viewer, False)
        if not result:
            return
        place, _x, _y = result
        target = ida_kernwin.place_t_as_simpleline_place_t(place.clone())
        if target is None:
            return
        target.n = lineno
        ida_kernwin.jumpto(viewer, target, x, y)

    # ------------------------------------------------------------------ #
    # gg / G
    # ------------------------------------------------------------------ #

    def _goto_top(self):
        lines = self._pseudocode_lines()
        if lines is not None:
            self._jump_pseudocode_line(0)
        else:
            ida_kernwin.jumpto(ida_ida.inf_get_min_ea())

    def _goto_bottom(self):
        lines = self._pseudocode_lines()
        if lines is not None:
            self._jump_pseudocode_line(max(0, len(lines) - 1))
        else:
            last = ida_bytes.prev_head(ida_ida.inf_get_max_ea(), ida_ida.inf_get_min_ea())
            if last != ida_idaapi.BADADDR:
                ida_kernwin.jumpto(last)

    # ------------------------------------------------------------------ #
    # in-line motions: f F ; , ^ w b
    # ------------------------------------------------------------------ #

    def _find_in_line(self, cmd, char, count):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, x, y = ctx

        pos = x
        for _ in range(count):
            if cmd == "f":
                pos = text.find(char, pos + 1)
            else:
                pos = text.rfind(char, 0, max(0, pos))
            if pos < 0:
                ida_kernwin.msg(f"[idavim] {char!r} not found in line\n")
                return
        self._jump_to_column(viewer, place, pos, y)

    def _repeat_find(self, char, count):
        if self.last_find is None:
            return
        cmd, target = self.last_find
        if char == ",":
            cmd = "F" if cmd == "f" else "f"
        self._find_in_line(cmd, target, count)

    def _goto_line_start(self):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, _text, place, _x, y = ctx
        self._jump_to_column(viewer, place, 0, y)

    def _goto_line_end(self):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, _x, y = ctx
        pos = max(0, len(text.rstrip()) - 1)
        self._jump_to_column(viewer, place, pos, y)

    def _goto_first_nonblank(self):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, _x, y = ctx
        stripped = text.lstrip()
        pos = len(text) - len(stripped) if stripped else 0
        self._jump_to_column(viewer, place, pos, y)

    def _word_forward(self, widget, count):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, x, y = ctx

        for _ in range(count):
            starts = [m.start() for m in WORD_RE.finditer(text) if m.start() > x]
            if starts:
                x = starts[0]
            else:
                # wrap to the first word of the next line
                self._send_key(widget, Qt.Key.Key_Down)
                self._send_key(widget, Qt.Key.Key_Home)
                ctx = self._viewer_ctx()
                if ctx is None:
                    return
                viewer, text, place, x, y = ctx
                starts = [m.start() for m in WORD_RE.finditer(text)]
                if starts:
                    x = starts[0]
        self._jump_to_column(viewer, place, x, y)

    def _word_end(self, widget, count):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, x, y = ctx

        for _ in range(count):
            ends = [m.end() - 1 for m in WORD_RE.finditer(text) if m.end() - 1 > x]
            if ends:
                x = ends[0]
            else:
                # wrap to the end of the first word of the next line
                self._send_key(widget, Qt.Key.Key_Down)
                self._send_key(widget, Qt.Key.Key_Home)
                ctx = self._viewer_ctx()
                if ctx is None:
                    return
                viewer, text, place, x, y = ctx
                ends = [m.end() - 1 for m in WORD_RE.finditer(text)]
                if ends:
                    x = ends[0]
        self._jump_to_column(viewer, place, x, y)

    def _word_backward(self, widget, count):
        ctx = self._viewer_ctx()
        if ctx is None:
            return
        viewer, text, place, x, y = ctx

        for _ in range(count):
            starts = [m.start() for m in WORD_RE.finditer(text) if m.start() < x]
            if starts:
                x = starts[-1]
            else:
                # wrap to the last word of the previous line
                self._send_key(widget, Qt.Key.Key_Up)
                self._send_key(widget, Qt.Key.Key_End)
                ctx = self._viewer_ctx()
                if ctx is None:
                    return
                viewer, text, place, x, y = ctx
                starts = [m.start() for m in WORD_RE.finditer(text)]
                if starts:
                    x = starts[-1]
        self._jump_to_column(viewer, place, x, y)

    # ------------------------------------------------------------------ #
    # search: / n N
    # ------------------------------------------------------------------ #

    def _prompt_search(self):
        pattern = ida_kernwin.ask_str(self.search, ida_kernwin.HIST_SRCH, "idavim search")
        if pattern:
            self.search = pattern
            self._search_step(1, 1)

    def _search_step(self, direction, count):
        if not self.search:
            ida_kernwin.msg("[idavim] no previous search (use /)\n")
            return
        # huge counts would rescan the listing over and over (the pseudocode
        # search wraps around), so cap the number of repeats
        for _ in range(min(count, 32)):
            lines = self._pseudocode_lines()
            if lines is not None:
                found = self._search_pseudocode(lines, direction)
            else:
                found = self._search_disasm(direction)
            if not found:
                ida_kernwin.msg(f"[idavim] pattern not found: {self.search}\n")
                return

    def _search_pseudocode(self, lines, direction):
        ctx = self._viewer_ctx()
        if ctx is None:
            return False
        viewer, _text, place, x, y = ctx
        lineno = ida_kernwin.place_t_as_simpleline_place_t(place).n
        pattern = self.search.lower()
        total = len(lines)

        # scan every line once, wrapping around, starting next to the cursor
        for step in range(total + 1):
            n = (lineno + direction * step) % total
            line = lines[n].lower()
            if step == 0:
                # within the current line, only look beyond the cursor
                if direction > 0:
                    pos = line.find(pattern, x + 1)
                else:
                    pos = line.rfind(pattern, 0, max(0, x))
            elif direction > 0:
                pos = line.find(pattern)
            else:
                pos = line.rfind(pattern)
            if pos >= 0:
                self._jump_pseudocode_line(n, pos, y)
                return True
        return False

    def _search_disasm(self, direction):
        ctx = self._viewer_ctx()
        if ctx is None:
            return False
        _viewer, _text, place, _x, _y = ctx
        ea = ida_kernwin.place_t_as_idaplace_t(place).ea
        pattern = self.search.lower()
        min_ea = ida_ida.inf_get_min_ea()
        max_ea = ida_ida.inf_get_max_ea()

        for _ in range(DISASM_SEARCH_LIMIT):
            if direction > 0:
                ea = ida_bytes.next_head(ea, max_ea)
            else:
                ea = ida_bytes.prev_head(ea, min_ea)
            if ea == ida_idaapi.BADADDR:
                return False

            line = ida_lines.generate_disasm_line(ea, ida_lines.GENDSM_REMOVE_TAGS) or ""
            name = ida_name.get_name(ea) or ""
            if pattern in line.lower() or pattern in name.lower():
                ida_kernwin.jumpto(ea)
                return True

        ida_kernwin.msg(
            f"[idavim] search stopped after {DISASM_SEARCH_LIMIT} items\n"
        )
        return False


# The event filter must be a process-wide singleton. IDA may create a new
# plugmod per database without the previous one being torn down first; if two
# filters were installed at once, the stale one would keep intercepting keys
# with its own (stale) mode. Reference counting keeps install/remove balanced.
_filter = None
_filter_refs = 0


def acquire_filter():
    global _filter, _filter_refs
    if _filter is None:
        _filter = VimEventFilter()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(_filter)
    _filter_refs += 1
    return _filter


def release_filter():
    global _filter, _filter_refs
    _filter_refs -= 1
    if _filter_refs <= 0 and _filter is not None:
        app = QtWidgets.QApplication.instance()
        if app is not None:
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
    ACTION_TOGGLE = "idavim:toggle"

    def __init__(self):
        self.event_filter = None
        self.init()

    def init(self):
        self.event_filter = acquire_filter()
        self.register_actions()
        logger.info("idavim loaded and enabled (i: hand keys to IDA, Shift+Esc: re-enable, Ctrl-Shift-V: toggle)")

    def register_actions(self):
        ida_kernwin.unregister_action(self.ACTION_TOGGLE)
        ida_kernwin.register_action(
            ida_kernwin.action_desc_t(
                self.ACTION_TOGGLE,
                "idavim: toggle vim mode",
                toggle_action_handler_t(self.event_filter),
                "Ctrl-Shift-V",
                "Enable or disable idavim keyboard handling",
                -1,
            )
        )

    def unregister_actions(self):
        ida_kernwin.unregister_action(self.ACTION_TOGGLE)

    def run(self, arg):
        if self.event_filter is not None:
            self.event_filter.toggle()

    def __del__(self):
        try:
            self.unregister_actions()
            if self.event_filter is not None:
                self.event_filter = None
                release_filter()
        except Exception:
            logger.exception("idavim teardown failed")


class idavim_plugin_t(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_MULTI
    comment = "vim-style navigation for disassembly and pseudocode views"
    help = "hjkl/u/d/gg/G/f/n vim navigation; i to hand keys to IDA, Shift+Esc or Ctrl-Shift-V to re-enable"
    wanted_name = "idavim"
    wanted_hotkey = ""

    def init(self):
        return idavim_plugmod_t()
