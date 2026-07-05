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
- **Cap the work, not the count** (`{n}n`/`{n}N`): pseudocode collects all
  match positions once per command and picks modularly (any count exact —
  one scan, one jumpto); disassembly spends one `DISASM_SEARCH_LIMIT` item
  budget per command, jumps to the last match reached, reports partial
  progress. (The old `min(count, 32)` cap silently truncated counts.)
  Backward in-line matching is match-start based (start < caret, like vim).
- **Pseudocode-only keys** (`:` line prompt, `cw` rename via
  `process_ui_action("hx:Rename")`): in the disassembly view `:` is IDA's
  "enter comment" and `c` is MakeCode, so both stay with IDA there. The
  gate (PSEUDOCODE_ONLY_KEYS) lives in `_in_vim_context`, NOT `_wants`
  (ordering invariant above), and is SKIPPED while a prefix is pending: a
  pending f/F target must always be consumed — `fc` leaking to IDA ran
  MakeCode, a destructive DB edit. Don't simplify the `not self.pending`
  condition away. `:` uses ask_long (ask_str + HIST_IDENT rejects digits).
- Never intercept when a modal widget is active, focus is in a text input,
  or the focus window is a QDialog; act only in `BWN_DISASM` /
  `BWN_PSEUDOCODE`, and in `BWN_DISASM` only with a `TCCRT_FLAT` renderer
  (graph mode is left entirely to IDA).
- Half-typed state (pending f/F/g/c target, count prefix) is abandoned on
  any focus change AND on any text-less key (bare modifiers excepted —
  Shift is held while typing an uppercase f-target). Completed-command
  state (last_find, the `/` pattern) survives.

### Known limitations — reviewed, deliberately NOT fixed

- Counts don't compose with `f`/`F` (`3fx` finds the 1st `x`): this is a
  navigation aid, not a vim emulator; `f` + `;;` covers it. `{n}gg`/`{n}G`
  ARE supported in pseudocode; the disassembly listing has no line numbers,
  so counts fall back to plain gg/G there.
- A bare count is not cancelled by Esc (Esc = IDA's navigate back); it is
  cleared by any motion, focus change, or toggle.
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
