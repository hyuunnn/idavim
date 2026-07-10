# idavim

<p align="center">
  <img src="ida-plugin.png" alt="idavim" width="480">
</p>

**English** | [한국어](README_ko.md)

Vim-style keyboard navigation for IDA Pro, inspired by [Vimium](https://github.com/philc/vimium) and [IdeaVim](https://github.com/JetBrains/ideavim).

Works in both the **disassembly view** and the **Hex-Rays pseudocode view**.

## How it works

idavim is a simple on/off switch:

- **Enabled** (default): the keys below are intercepted for navigation, taking priority over IDA's single-key shortcuts (`n` rename, `d` data, `u` undefine, `g` jump, ...).
- **Disabled**: every key goes to IDA, so all native IDA shortcuts work as usual.

| Action | Key |
|---|---|
| Toggle enabled ↔ disabled | `Shift+Esc` (plain `Esc` stays with IDA's "navigate back"; also Edit → Plugins → idavim) |

The toggle is a regular IDA action (`idavim: toggle`), so the key can be remapped in Options → Shortcuts.

State changes are printed to the Output window (`[idavim] enabled` / `[idavim] disabled`), so check there when unsure.

Keys are intercepted with an application-level Qt event filter, so nothing is stolen from dialogs, the CLI input, or any other text field — only the listing views are affected. Graph-mode disassembly is also left entirely to IDA (line-oriented motions make no sense there).

With "Synchronize with" enabled, views linked to the pseudocode follow idavim motions just like they follow the arrow keys. (IDA's sync only reacts to real key input, so after each jump idavim taps a quick Down+Up — net movement zero — and lets IDA do the following.)

## Keys (while enabled)

### Movement

| Key | Action |
|---|---|
| `h` `j` `k` `l` | left / down / up / right |
| `d` / `u` | half page down / up (Vimium style) |
| `gg` / `G` | top / bottom of listing (start / end of database in disassembly) |
| `{n}gg` / `{n}G` | jump to line n — pseudocode view only (e.g. `3G` → line 3) |
| `:` | prompt for a line number and jump to it (`:30`) — pseudocode view only; in the disassembly view `:` stays IDA's "enter comment" |
| `0` / `^` / `$` | start of line / first non-blank / end of line |
| `w` / `e` / `b` | next word start / word end / previous word start |
| `1`–`9` | count prefix, e.g. `12j`, `3w`, `2d` |

### Find & search

| Key | Action |
|---|---|
| `f{char}` / `F{char}` | find character forward / backward in the current line |
| `;` / `,` | repeat last `f`/`F` (same / opposite direction) |
| `/` | search (prompts for a pattern, case-insensitive substring) |
| `n` / `N` | next / previous search match |

In the pseudocode view, `/` searches all lines of the current function and wraps around.
In the disassembly view it walks item heads (disassembly text and names) from the cursor.

### Actions

| Key | Action |
|---|---|
| `cw` | rename the identifier under the cursor (opens IDA's rename dialog) — pseudocode view only; in the disassembly view `c` stays IDA's "make code" |

Everything else (`Esc`, `Enter`, `x`, `Space`, arrows, ...) is passed through to IDA even while idavim is enabled. The only exception is the single key typed right after `f`/`F`/`g`/`c`: a printable key completes that command, and a plain `Esc` cancels it (vim-style) without triggering IDA's "navigate back".

### Command composition

idavim is a navigation aid, not a vim emulator — only single commands (optionally with a count prefix) are supported:

- **Supported**: `count + motion` (`12j`, `3w`, `2d`), `count + gg/G` (`30G`, pseudocode only), `f{char}` then `;`/`,`
- **Not supported**: `count + f/F` (`3fx` finds the 1st match — use `f` + `;;`), operators and text objects (`dw`, `yy`, `ci(`, ...), marks, registers, macros

An interrupted two-key sequence behaves like vim: an invalid follow-up key cancels the command (`30g` then `3` throws away both), and switching focus to another widget or clicking the mouse abandons any half-typed command.

## Requirements

- IDA Pro 9.0+ with a GUI (Qt via PySide6 or PyQt5)
- No external Python dependencies

## Installation

Clone the repository and symlink it into your IDA plugins directory:

```sh
git clone https://github.com/hyuunnn/idavim.git
ln -s "$(pwd)/idavim" ~/.idapro/plugins/idavim
```

Then restart IDA.

## License

MIT
