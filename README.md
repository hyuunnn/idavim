# idavim

**English** | [한국어](README.ko.md)

Vim-style keyboard navigation for IDA Pro, inspired by [Vimium](https://github.com/philc/vimium) and [IdeaVim](https://github.com/JetBrains/ideavim).

Works in both the **disassembly view** and the **Hex-Rays pseudocode view**.

## How it works

idavim is modal, like vim:

- **NORMAL mode** (default): the keys below are intercepted for navigation, taking priority over IDA's single-key shortcuts (`n` rename, `d` data, `u` undefine, `g` jump, ...).
- **INSERT mode**: everything is passed through to IDA, so all native IDA shortcuts work as usual.

| Action | Key |
|---|---|
| Enter INSERT (passthrough) mode | `i` |
| Back to NORMAL mode | `Ctrl+[` or `Shift+Esc` (macOS: `⌃[` or `⌘[` both work) |
| Enable/disable idavim entirely | `Ctrl+Shift+V` (or Edit → Plugins → idavim) |

Keys are intercepted with an application-level Qt event filter, so nothing is stolen from dialogs, the CLI input, or any other text field — only the listing views are affected.

## Keys (NORMAL mode)

### Movement

| Key | Action |
|---|---|
| `h` `j` `k` `l` | left / down / up / right |
| `d` / `u` | half page down / up (Vimium style) |
| `gg` / `G` | top / bottom of listing (start / end of database in disassembly) |
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

Everything else (`Esc`, `Enter`, `x`, `Space`, arrows, ...) is passed through to IDA even in NORMAL mode.

## Requirements

- IDA Pro 9.0+ with a GUI (Qt via PySide6 or PyQt5)
- No external Python dependencies

## Installation

Via the [IDA Plugin Manager](https://plugins.hex-rays.com/):

```sh
hcli plugin install idavim
```

Or manually: copy `idavim_entry.py`, `idavim.py` and `ida-plugin.json` into a `idavim/` directory inside your IDA plugins directory (e.g. `~/.idapro/plugins/idavim/`).

For development, a symlink to the repository works too:

```sh
ln -sfn /path/to/idavim ~/.idapro/plugins/idavim
```

## License

MIT
