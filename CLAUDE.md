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
  pass the same `_wants()` predicate. Order matters for performance: the
  filter evaluates `_wants(event) and _in_vim_context(event)` — `_wants`
  makes NO IDA API calls (it reads the enabled flag, the Qt event, the
  pending state — which it clears for keys that cannot complete a pending
  command — and last_find, which keeps `;`/`,` with IDA until f/F has been
  used), so the IDA API probe (`_in_vim_context`) runs only for keys
  idavim might claim, and a disabled idavim costs nothing per keystroke.
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
  ALL in-line horizontal motions (`h/l`, `f/F/;/,`, `0/^/$`, and the word
  motions' landing step) move the caret by synthesizing native Left/Right
  key events — `jumpto` for a horizontal move runs IDA's full navigation
  (item highlight + address-sync recompute) whose cost varies with the
  token under the target column, so identical motions felt fast or slow
  depending on where they landed (first seen with h/l, again with `;`/`,`).
  `0`/`$` compute the target column and arrow-key to it instead of sending
  Home/End — IDA's own Home/End keep moving the cursor on repeated presses.
  `_jump_to_column` reads the caret itself right before moving (callers
  cannot pass a stale position) and the move is deliberately uncapped: a
  cap silently landed long-line motions short, and both endpoints lie
  within the current line, which bounds the synthetic-key burst anyway.
- **`{n}n`/`{n}N` cap the work, not the count**: the pseudocode view
  collects every match position once per command and picks the target with
  modular index arithmetic (any count is exact — one scan, one jumpto); the
  disassembly view spends a single `DISASM_SEARCH_LIMIT` item budget across
  the whole command and jumps once to the last match reached, reporting
  partial progress. The old `min(count, 32)` repeat cap silently truncated
  counts and re-ran `jumpto` per repeat. Backward in-line matching is
  match-start based (start < caret, like vim), not whole-match-left-of-caret.
- Never intercept keys when a modal widget is active, focus is in a text
  input, or the focus window is a QDialog; only act in `BWN_DISASM` /
  `BWN_PSEUDOCODE`, and in `BWN_DISASM` only when the renderer is
  `TCCRT_FLAT` (graph mode is left entirely to IDA).
- Half-typed command state (pending f/F/g/c target, count prefix) is
  abandoned on any focus change (`QApplication.focusChanged`) AND on any
  text-less key (arrows, PgUp/PgDn — bare modifier presses excepted, since
  Shift is held while typing an uppercase f-target), so a stale prefix never
  hijacks a later key. Completed-command state (last_find for `;`/`,`, the
  `/` search pattern) survives.
- **Known limitation, deliberately NOT fixed**: counts do not compose with
  `f`/`F` — `3fx` finds the 1st `x` (the count is consumed when `f` sets the
  pending state and is not carried to the target key). The plugin is a
  navigation aid for analysis, not a vim emulator; `f` + `;;` covers the use
  case. `{n}gg`/`{n}G` ARE supported (pseudocode only, via pending_count /
  has_count); the disassembly listing has no line numbers so counts fall
  back to plain gg/G there. `:` prompts for a line number (`:30`, via
  ask_long — ask_str with HIST_IDENT rejects digits as "not a valid
  identifier") and is intercepted in the pseudocode view ONLY — in the
  disassembly view `:` is IDA's "enter comment" key and must stay with IDA.
  `cw` (rename under cursor, `process_ui_action("hx:Rename")`) is likewise
  pseudocode-only: in the disassembly view `c` is IDA's "make code". The
  pseudocode-only gating lives in `_in_vim_context` (PSEUDOCODE_ONLY_KEYS,
  checked against the already-resolved widget type), NOT in `_wants` — that
  keeps `_wants` free of IDA API calls per the ordering invariant above.
  The gate is SKIPPED while a prefix is pending: a pending f/F target must
  always be consumed — `fc` in the disassembly view finds `c`; letting it
  leak to IDA ran MakeCode (a destructive DB edit). Don't "simplify" the
  `not self.pending` condition away.
- **Known limitation, deliberately NOT fixed**: a bare count (digits typed
  with no pending prefix) is not cancelled by Esc — Esc passes to IDA
  (navigate back) and the count stays armed for the next motion. Reviewed
  and accepted: the count is cleared by any motion, focus change, or toggle.
- **Known limitation, deliberately NOT fixed**: acquire_filter is not
  atomic (an exception between filter creation and refcount increment could
  leave a half-initialized singleton) and the j/k hot path re-resolves the
  widget/viewer 2-3x per press with a redundant place clone. Reviewed;
  no realistic trigger / microsecond-scale cost — not worth the churn.
- **Known limitation, deliberately NOT fixed**: w/e/b wrapping at the first/
  last line of the listing moves the cursor to the wrong word instead of
  staying put like vim. A fix based on `place.clone()` + `place.compare()`
  to detect "the line did not change" broke w/b outright in live IDA and was
  rolled back; the real-world impact (last pseudocode line is just `}`) does
  not justify the risk. If ever retried, compare explicit line identity per
  view (simpleline `.n` / idaplace `.ea`), not `place_t.compare()`.

## Environment facts

- Verified against IDA Professional 9.3 on macOS: Qt 6.8.2, native binding
  is **PySide6** (the bundled PyQt5 directory is a compatibility shim), so
  import PySide6 first and fall back to PyQt5 for older IDA versions.
- IDA default shortcuts live in `<IDA.app>/Contents/MacOS/cfg/idagui.cfg`
  — check there before claiming a chord is free.
