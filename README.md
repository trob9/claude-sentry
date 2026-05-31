# claude-sentry

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Built with Textual](https://img.shields.io/badge/built%20with-Textual-5a2ca0.svg)

**See what Claude Code is touching — live.** A sidebar that shows every file it
edits, skill and agent it runs, and tool it calls, so you never scroll back
through the transcript to find out what changed.

```bash
pipx install git+https://github.com/trob9/claude-sentry.git && claude-sentry-install
```

```
┌─ ▼ Activity (this session) ──────────────────────┐
│ act  file                       −     +    when  │
│ edt  …sentry/app.py            -116  +174   1m   │
│ del  …sentry/aliases.json                  50m   │   ← deleted file in red
│ cre  …sentry/hook.py                 +42   1h   │
├──────────── drag or +/- to resize ───────────────┤
│ Skills │ Agents │ Tools │ Unconfirmed (1)         │
│ name                  session  week  all         │
│ ▸ View all installed…                            │
│ verify (native)          2      3    3           │
└──────────────────────────────────────────────────┘
 + Taller   − Shorter   o Open   r Reveal   c Copy
 m Menu   v View all   l Link   s Settings   q Quit
   left-click: select    right-click: options
   alt+shift+←→: widen / narrow   (Windows Terminal)
```

- **Activity** (top): files this session edited/created/deleted, with line
  add/remove counts and how long ago. Deleted or missing files show in red.
- **Skills / Agents / Tools** (bottom tabs): what the session invoked. Skills and
  Agents show per-session, 7-day, and all-time counts; Tools is per-session.
- **Unconfirmed** (bottom tab): a review queue. Anything that *looks* like a
  skill or agent but isn't installed on disk and isn't a known Claude built-in —
  a typo, or a brand-new command — lands here instead of cluttering the real
  lists. Click the green ✓ to confirm it (it moves into Skills/Agents) or the red
  ✗ to dismiss it for good; or use **✓ Confirm all** / **✗ Deny all** at the top
  to clear the list in one click. Your decisions are saved permanently.

Claude's **built-in** skills and agents (`verify`, `code-review`,
`general-purpose`, …) are recognised out of the box, shown with a `(native)`
tag. Clicking one opens a small menu with a deep link to that command's entry in
the [Claude docs](https://code.claude.com/docs/en/commands).

Left-click a row to select it. Right-click (or press `m`) opens a context menu —
**Open file**, **Show in file browser**, **Copy path**. Press `v` (or click
*View all installed*) to open a separate window listing every skill and agent on
the machine with global usage counts.

**Path display:** open Settings (`s` or `Ctrl+P`) and pick how Activity shows
paths — *filename only* (`app.py`), *filename + 1 folder* (`claude-sentry/app.py`),
or *full path* (`~/Projects/claude-sentry/app.py`). The choice persists. Rows are
always de-duplicated by the full path, so changing the display never splits or
merges files.

---

## Why not just scroll the transcript?

You can — but the transcript interleaves Claude's prose with its tool calls, so
finding "which files actually changed" means reading past everything else, and
the answer scrolls away as the session grows. claude-sentry pulls just the
**signal** into a fixed pane: a deduplicated list of touched files (newest
first, deletions in red, with line counts), and a running tally of which skills,
agents, and tools the session leans on. It updates live, so it's a glance, not a
search. Nothing to query, nothing to scroll.

---

## What it needs to work

claude-sentry has two moving parts:

1. **The TUI** — a Python/[Textual](https://textual.textualize.io/) app you run
   in a terminal. This part works on Windows, macOS, and Linux.
2. **Hooks** — small commands Claude Code runs automatically on each tool call.
   They write one line per event to a log file the TUI reads. Without the hooks,
   the TUI runs but shows nothing, because nothing is feeding it data.

> **Why hooks?** Claude Code can't push live data into another window on its own.
> But it can run a command after every tool it uses (a "hook"). We register a
> hook that appends each Edit/Write/Bash/Skill/Task event to
> `~/.claude/sentry/events.jsonl`. The TUI tails that file. So the hook is the
> bridge between Claude and the sidebar — that's the one piece of setup you
> can't skip.

---

## Setup (2 steps)

**Prerequisite:** Python ≥ 3.10. ([pipx](https://pipx.pypa.io/) recommended — it
isolates the app and guarantees the commands land on your `PATH`.)

### 1. Install the package

```bash
pipx install git+https://github.com/trob9/claude-sentry.git
```

> Not on PyPI yet — installing straight from GitHub works today. Once published,
> this becomes `pipx install claude-sentry`.

This puts four commands on your `PATH`:

| Command | What it is |
|---|---|
| `claude-sentry` | the sidebar TUI you run |
| `claude-sentry-hook` | the logging hook Claude runs (you don't run this yourself) |
| `claude-sentry-launch` | the auto-dock hook (Windows Terminal only) |
| `claude-sentry-install` | wires the hooks into your Claude settings |

(Plain `pip install git+https://github.com/trob9/claude-sentry.git` also works
if you manage your own environment — just make sure the four commands land on
your `PATH`.)

### 2. Register the hooks

```bash
claude-sentry-install
```

This edits `~/.claude/settings.json` and adds:

- **`PostToolUse`** hook (matcher `*`) → runs `claude-sentry-hook` after every
  tool call. This is what records edits, deletions, skill/agent/tool usage. It is
  **required** on every platform.
- **`UserPromptSubmit`** hook (matcher `*`) → runs `claude-sentry-hook` on each
  prompt you send, so a `/slash-command` you type is tracked too (those never go
  through the Skill tool, so `PostToolUse` alone can't see them). Every leading
  `/command` is logged; ones that aren't installed or a known built-in land in
  the **Unconfirmed** tab for you to confirm or dismiss, so a typo like `/tset`
  never silently becomes a tracked skill.
- **`SessionStart`** hook (matcher `*`) → runs `claude-sentry-launch`.
  **Windows Terminal only.** It splits a 25%-wide sidebar pane on the right of
  your terminal automatically when a Claude session starts, already linked to
  that session. Pass `--no-launcher` to skip it:

  ```bash
  claude-sentry-install --no-launcher
  ```

The installer is idempotent — running it twice does nothing the second time.

**Restart your Claude session** (or start a new one) so the hooks take effect.

### Uninstall

```bash
claude-sentry-install --uninstall   # removes the hooks
pipx uninstall claude-sentry        # removes the commands
```

---

## Running it

```bash
claude-sentry                       # global mode — activity across ALL sessions
claude-sentry --session <uuid>      # scope to one Claude session
claude-sentry --inventory           # full-window catalogue of installed skills/agents
```

### Linking to your current chat

The sidebar filters its Activity pane to a single Claude session. There are three
ways it learns which one:

- **Windows Terminal, auto-launched:** the `SessionStart` hook passes the session
  ID for you. Nothing to do.
- **Manual start in the same WT window:** `claude-sentry` re-reads the session the
  hook recorded for that window, so a restart re-attaches automatically.
- **Anywhere else (global mode):** when you run bare `claude-sentry` with no
  session, it opens in **global mode** and pops up a Link dialog. To link it:
  run **`/status`** in your Claude session to see its session ID, then paste the
  UUID into the dialog. You can re-link any time with the **`l`** key.

---

## Keys

| Key | Action |
|---|---|
| `+` / `−` | resize the divider — grows/shrinks the focused pane |
| `o` | open the selected file with the OS default app |
| `r` | reveal the selected file in your file manager |
| `c` | copy the selected file's path to the clipboard |
| `m` | open the right-click context menu for the selected row |
| `v` | open the full inventory window |
| `l` | link this sidebar to a Claude session (paste a UUID) |
| `s` / `Ctrl+P` | open Settings (the command palette — themes, etc.) |
| `q` | quit |
| `Enter` | activate the selected row / confirm a dialog |
| `Esc` | close a dialog or the command palette |

Theme changes (Settings → "Change theme") persist across launches.

---

## How it stores data

```
~/.claude/sentry/
  events.jsonl        # one JSON line per tool call, append-only
  config.json         # divider position, theme, path-display mode
  confirmations.json  # your confirm/deny decisions for the Unconfirmed tab
  win-session/        # records which Claude session is active per WT window
  locks/              # per-window guards so the pane only auto-launches once
```

`events.jsonl` is plain JSON-lines — safe to read, archive, or delete. Deleting
it resets all counts; the hooks repopulate it as you keep working.

---

## Platform notes

| Platform | TUI | Logging hook | Auto-dock sidebar |
|---|---|---|---|
| **Windows Terminal** | ✅ | ✅ | ✅ (splits a pane for you) |
| **macOS** (Terminal/iTerm2) | ✅ | ✅ | ➖ run `claude-sentry` yourself |
| **Linux** (gnome-terminal, kitty, etc.) | ✅ | ✅ | ➖ run `claude-sentry` yourself |

Open-with-default and reveal-in-file-manager are wired per OS: `os.startfile` on
Windows, `open` / `open -R` on macOS, `xdg-open` on Linux. The inventory window
spawns via Windows Terminal, `osascript` on macOS, or a detected terminal on
Linux.

**Best layout:** dock claude-sentry as a narrow (~25%) pane on the right of your
terminal. On Windows Terminal the auto-dock hook does this; elsewhere, split your
terminal manually and run `claude-sentry` in the new pane.

---

## Development

```bash
git clone https://github.com/trob9/claude-sentry.git && cd claude-sentry
python -m venv .venv && .venv/bin/pip install -e .   # Windows: .venv\Scripts\pip
claude-sentry-install
```

With an editable install (`-e`), edits to `claude_sentry/` take effect on the
next launch. The app is a single module, `claude_sentry/app.py`; the hooks are
`hook.py` and `launch_hook.py`; the settings wiring is `install.py`.

---

Found a bug or have an idea? [Open an issue](https://github.com/trob9/claude-sentry/issues).
If claude-sentry saves you a scroll, a ⭐ helps other people find it.
