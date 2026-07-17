# idavim — development notes

Vim-style keyboard navigation plugin for IDA Pro (9.0+, GUI only). The
user-facing key reference lives in `README.md`/`README_ko.md` — keep both
and the `ida-plugin.json` description in sync when keys change.

## Layout

- `idavim_entry.py` — gated entry point: loads the real plugin only in an
  interactive GUI IDA (9.0+), otherwise returns a hidden no-op plugin
- `idavim.py` — everything else: event filter, motions, plugin registration
- `ida-plugin.json` — Plugin Manager manifest (not yet published to
  plugins.hex-rays.com)

## Dev workflow

- Installed via symlink: `~/.idapro/plugins/idavim -> this repo`.
  **No hot reload** — every code change requires a full IDA restart
- No automated tests; verify by driving IDA manually. Messages go to the
  Output window prefixed with `[idavim]`. Per-key tracing (IDA Python CLI):
  `import logging; logging.basicConfig(); logging.getLogger('idavim').setLevel(logging.DEBUG)`
- Lint/package: `uv run --with=ida-hcli hcli plugin lint <zip>` (the
  ida-plugin-development skill's `hcli-package.py` builds the zip)

## Design decisions (hard-won, don't regress)

- **Key interception**: application-level Qt event filter. Accepting
  `ShortcutOverride` suppresses IDA's shortcut for that key; it redelivers
  as a `KeyPress`, which we consume. Both event types must pass the same
  `_wants()` predicate. Ordering invariant: `_wants` makes NO IDA API calls
  (enabled flag, Qt event, pending state, last_find — `;`/`,` stay with IDA
  until f/F has been used), so the IDA probe (`_in_vim_context`) runs only
  for keys idavim might claim and a disabled idavim costs nothing per key.
- **Enable/disable, not vim modes**: one boolean (NORMAL/INSERT confused
  users). Toggle is IDA action `idavim:toggle` (Shift-Esc, remappable);
  while disabled every key goes to IDA. Rejected toggle chords: `i` (IDA's
  MakeExtraLineA), `Ctrl+[` (macOS turns it into Esc = IDA's back), `Cmd+[`
  (IDA's back).
- **Singleton filter AND action**, refcounted at module level
  (acquire_filter/release_filter). IDA can create a new plugmod per database
  without tearing down the old one; per-plugmod ownership let a stale
  `__del__` unregister the live action (Shift+Esc dead, keys intercepted).
- **Qt6 quirks**: key events may arrive at the top-level `QWindow`, so
  resolve `QApplication.focusWidget()` yourself. On macOS Qt maps physical
  Ctrl to `MetaModifier` and Cmd to `ControlModifier`.
- **Places**: `simpleline_place_t`/`idaplace_t` are abstract in the IDA 9.3
  bindings — never construct them; `clone()` the current place and retarget.
- **Motions**: vertical (`j/k/d/u`) computes the target and calls `jumpto`
  once — with `UIJMP_DONTPUSH` in disassembly so movement doesn't pollute
  the Esc history (item-head granularity is deliberately rough). ALL in-line
  horizontal motions synthesize native Left/Right key events: `jumpto`'s
  cost varies with the token under the landing column (identical motions
  felt fast or slow), and IDA's own Home/End keep moving on repeated
  presses, so `0`/`$` compute the column and arrow-key to it. `h`/`l`
  clamp `caret ± count` to the line and jump the same way (replacing an
  old `min(count, 128)` cap that truncated silently).
  `_jump_to_column` reads the caret itself right before moving and is
  deliberately uncapped — a cap silently landed long-line motions short,
  and both endpoints lie in the current line, bounding the burst. Motions
  doing real work per repeat (`w/e/b` iterations, the disassembly head-walk
  of `j/k/d/u`) clamp the count at `MOTION_LIMIT` WITH a message so a
  runaway count can't freeze the UI; pseudocode `j/k` is exact arithmetic,
  uncapped.
- **Synced-view follow**: IDA's "Synchronize with" reacts to real key
  input ONLY — programmatic jumps (`jumpto`, `custom_viewer_jump`,
  `refresh_cpos`) repaint the partner's highlight but never scroll it,
  and driving the partner view directly via the API feeds back (its jump
  reciprocally yanks this view's caret to the address's first line, then
  a queued counter-move clobbers the drive; measure with the partner's
  scrollbar/caret, never the highlight). So every pseudocode jump ends
  with `_nudge_sync`: a native Down+Up pair, net movement zero, order
  flipped on the last line — replaying the one input IDA listens to.
  Sync on/off is honored automatically because IDA itself decides.
- **Cap the work, not the count** (`{n}n`/`{n}N`): pseudocode collects all
  match positions once per command and picks modularly (any count exact —
  one scan, one jumpto); disassembly spends one `DISASM_SEARCH_LIMIT` item
  budget per command, jumps to the last match reached, reports partial
  progress. (The old `min(count, 32)` cap silently truncated counts.)
  Backward in-line matching is match-start based (start < caret, like vim).
- **`*`/`#` reuse the `/`→`n`/`N` machinery** with a whole-word regex
  (`\b`), so `v1` never stops on `v12`; `/` stays substring. The identifier
  is taken from a `\w`-run scan at the caret column (falling right to the
  next identifier on the line, vim-style) — NOT `get_highlight`, which can
  be stale/locked from a past click and needn't match the caret. `*`
  shadows IDA's MakeArray and `#` OpNumber (same trade as n/d/u/g/c).
  `*`/`#` anchor the bisect step at the identifier's START (anchor_x), not
  the caret — vim's `#` from mid-word goes to the previous occurrence.
  The pseudocode scan prefilters each line with a casefolded substring
  test before the regex: a `\b`/IGNORECASE pattern loses sre's literal
  fast path (measured 34ms→7ms per press on a 40k-line function).
- **`cw` renames in BOTH views**, dispatching per view
  (`process_ui_action`: `hx:Rename` / `MakeName`). `c` therefore shadows
  IDA's MakeCode while enabled — the same trade as n/d/u/g, toggle off to
  use them. (Originally `c` stayed with IDA in the disassembly view; that
  exception was dropped as inconsistent with the toggle model.)
- **Marks are IDA bookmarks, ea-only** (`m{a-z}`/`` `{a-z} ``): a mark
  remembers just an address, so `` ` `` is ONE `jumpto` — cross-view for
  free, one Esc-history entry. (Line/column restore was built and dropped:
  it needs a second jump, whose extra history push made Esc land on the
  intermediate spot.) Three silent bookmark traps, measured on IDA 9.3:
  (1) storage is split per place class — `mark()`ing the pseudocode's own
  place returns success into a storage the Bookmarks widget and
  `bookmarks_t(viewer)` never read, so the place is normalized to an
  idaplace_t carrying only the ea (clone the class template; idaplace_t
  is abstract, and don't mutate the template itself). (2) passing
  BOOKMARKS_BAD_INDEX (0xFFFFFFFF, not exposed by ida_moves) as the index
  is a silent no-op, NOT an append — append with
  `index = len(bookmarks_t(viewer))`; an existing index overwrites (used
  to remark a letter). (3) never trust a stored index — widget deletion
  renumbers, so every lookup rescans by description (`"idavim: a"`, exact
  string match; the letter lives there because bookmarks have no letter
  field). `m` shadows IDA's OpEnum, `y` (yy = copy the line's ea)
  SetType; the backtick is unbound.
- **Pseudocode-only key** `:` (line prompt): in the disassembly view `:`
  is IDA's "enter comment" and there are no line numbers to jump to. The
  gate (PSEUDOCODE_ONLY_KEYS) lives in `_in_vim_context`, NOT `_wants`
  (ordering invariant above), and is SKIPPED while a prefix is pending: a
  pending f/F target must always be consumed, never leaked to IDA (this
  gate once covered `c`, where an `fc` leak ran MakeCode — a destructive
  DB edit). Don't simplify the `not self.pending` condition away. For the
  same reason eventFilter's except fails CLOSED while a prefix is pending
  (swallow one key, reset) — an exception in the probe would otherwise
  leave the override unaccepted and fire IDA's shortcut for the target
  key. `:` uses ask_long (ask_str + HIST_IDENT rejects digits).
- Never intercept when a modal widget is active, focus is in a text input,
  or the focus window is a QDialog; act only in `BWN_DISASM` /
  `BWN_PSEUDOCODE`, and in `BWN_DISASM` only with a `TCCRT_FLAT` renderer
  (graph mode is left entirely to IDA).
- Half-typed state (pending f/F/g/c target, count prefix) is abandoned on
  any focus change, any text-less key, any modifier chord (bare modifiers
  excepted — Shift is held while typing an uppercase f-target), AND any
  mouse press. The mouse-press reset lives in eventFilter, not `_wants`:
  a click can open a context menu (popups take no focus, so no
  focusChanged fires) or just move the caret, and neither produces a key
  event — without it a pending prefix survived the popup and hijacked a
  key pressed much later. Completed-command state (last_find, the `/`
  pattern) survives.
- Never intercept while a Qt popup (context menu) is open: popups are
  neither modal nor focus-taking, so only `activePopupWidget()` detects
  them — without that check menu type-ahead keys would run vim motions.

### Known limitations — reviewed, deliberately NOT fixed

- Counts don't compose with `f`/`F` (`3fx` finds the 1st `x`): this is a
  navigation aid, not a vim emulator; `f` + `;;` covers it. `{n}gg`/`{n}G`
  ARE supported in pseudocode; the disassembly listing has no line numbers,
  so counts fall back to plain gg/G there.
- A bare count is not cancelled by Esc (Esc = IDA's navigate back), nor by
  other unclaimed keys that carry Qt text (Enter, Tab, Backspace — same
  mechanism: non-empty `text()` skips the text-less reset); it is cleared
  by any motion, text-less key, modifier chord, focus change, or toggle.
- acquire_filter is not atomic and the j/k hot path re-resolves the
  widget/viewer 2-3x per press — no realistic trigger / microsecond cost.
- w/e/b wrapping at the listing's first/last line lands on the wrong word
  instead of staying put. A `place_t.compare()`-based fix broke w/b in live
  IDA and was rolled back; if ever retried, compare per-view line identity
  (simpleline `.n` / idaplace `.ea`), never `place_t.compare()`.

## Environment facts

- Verified against IDA Professional 9.3 on macOS: Qt 6.8.2, native binding
  is **PySide6** (the bundled PyQt5 directory is a shim) — import PySide6
  first, fall back to PyQt5 for older IDA versions.
- IDA default shortcuts live in `<IDA.app>/Contents/MacOS/cfg/idagui.cfg`
  — check there before claiming a chord is free.
