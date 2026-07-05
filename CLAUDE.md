# idavim — development notes

Vim-style keyboard navigation plugin for IDA Pro (9.0+, GUI only). See
`README.md` / `README_ko.md` for the user-facing key reference — keep both in
sync when keys change, and update the description in `ida-plugin.json` too.

## Layout

- `idavim_entry.py` — gated entry point; loads the real plugin only in an
  interactive GUI IDA (9.0+), otherwise returns a hidden no-op plugin
- `idavim.py` — everything else: event filter, motions, plugin registration
- `ida-plugin.json` — Plugin Manager manifest (name-based `hcli` install is
  not live yet; the plugin is not published to plugins.hex-rays.com)

## Dev workflow

- Installed via symlink: `~/.idapro/plugins/idavim -> this repo`
- **No hot reload** — every code change requires a full IDA restart
- No automated tests; verify by driving IDA manually. Mode transitions and
  errors are printed to the Output window prefixed with `[idavim]`
- Per-key debug tracing: run in IDA's Python CLI:
  `import logging; logging.basicConfig(); logging.getLogger('idavim').setLevel(logging.DEBUG)`
- Lint/package: `uv run --with=ida-hcli hcli plugin lint <zip>` (the
  ida-plugin-development skill's `hcli-package.py` script builds the zip)

## Design decisions (hard-won, don't regress)

- **Key interception**: application-level Qt event filter. Accepting the
  `ShortcutOverride` event suppresses IDA's own shortcut for that key and the
  key is redelivered as a `KeyPress`, which we consume. Both event types must
  pass the same `_wants()` predicate.
- **Enable/disable, not vim modes**: a single boolean, deliberately not
  NORMAL/INSERT (two layers of state confused users). The toggle is a
  registered IDA action (`idavim:toggle`, hotkey `Shift-Esc`, remappable in
  Options → Shortcuts) — the event filter itself intercepts nothing while
  disabled. While disabled every key (IDA's native n/d/u/g/...) goes to IDA.
  `i` was tried as the disable chord and removed — it is IDA's
  MakeExtraLineA (insert comment line) in the disassembly view. `Ctrl+[`
  was removed because macOS turns it into ESC (conflicts with IDA's Esc =
  navigate back) and `Cmd+[` is IDA's own back-navigation.
- **Singleton filter AND action**: the filter and the `idavim:toggle` action
  registration are one refcounted module-level singleton (both live in
  acquire_filter/release_filter). IDA can create a new plugmod per database
  without tearing down the old one first; per-plugmod ownership let a stale
  plugmod's `__del__` unregister the action the live plugmod had just
  re-registered (Shift+Esc dead while keys were still intercepted).
- **Qt6 quirks**: key events may be delivered to the top-level `QWindow`
  (no `fontMetrics`), so the handler resolves `QApplication.focusWidget()`
  itself. On macOS Qt maps physical Ctrl to `MetaModifier` and Cmd to
  `ControlModifier`.
- **Places**: `simpleline_place_t` / `idaplace_t` are abstract in the IDA 9.3
  Python bindings — never construct them; `clone()` the viewer's current
  place and retarget it.
- **Motions**: vertical movement (`j/k/d/u`) computes the target and calls
  `jumpto` once (clamped to the listing bounds; disassembly moves by item
  heads, which is deliberately rough). Disassembly jumps use
  `UIJMP_DONTPUSH` so plain movement does not pollute the Esc history.
  Horizontal movement (`h/l`) synthesizes native Left/Right key events —
  `jumpto` per keypress caused visible lag there. `0`/`$` jump to a
  computed column instead of sending Home/End — IDA's own Home/End keep
  moving the cursor on repeated presses.
- Never intercept keys when a modal widget is active, focus is in a text
  input, or the focus window is a QDialog; only act in `BWN_DISASM` /
  `BWN_PSEUDOCODE`, and in `BWN_DISASM` only when the renderer is
  `TCCRT_FLAT` (graph mode is left entirely to IDA).
- Half-typed command state (pending f/F/g target, count prefix) is abandoned
  on any focus change (`QApplication.focusChanged`), so a stale prefix never
  hijacks a key pressed after the user worked elsewhere. Completed-command
  state (last_find for `;`/`,`, the `/` search pattern) survives.

## Environment facts

- Verified against IDA Professional 9.3 on macOS: Qt 6.8.2, native binding
  is **PySide6** (the bundled PyQt5 directory is a compatibility shim), so
  import PySide6 first and fall back to PyQt5 for older IDA versions.
- IDA default shortcuts live in `<IDA.app>/Contents/MacOS/cfg/idagui.cfg`
  — check there before claiming a chord is free.
