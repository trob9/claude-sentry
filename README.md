# claude-sentry

A sidebar TUI that watches a Claude Code session and shows you, live, what it's
touching — files edited, skills and agents invoked, and tool calls — without you
having to scroll back through the transcript.

```
┌─ ▼ Activity (this session) ──────────────────────┐
│ act  file                       −     +    when  │
│ edt  …sentry/app.py            -116  +174   1m   │
│ del  …sentry/aliases.json                  50m   │   ← deleted file in red
│ cre  …sentry/hook.py                 +42   1h   │
├──────────── drag or +/- to resize ───────────────┤
│ Skills │ Agents │ Tools                          │
│ name                      ses   7D   all         │
│ ▸ View all installed…                            │
│ verify (native)            2    3    3           │
└──────────────────────────────────────────────────┘
 + Taller   − Shorter   o Open   r Reveal   c Copy
 m Menu   v View all   l Link   s Settings   q Quit
        left-click: select    right-click: menu
```

- **Activity** (top): files this session edited/created/deleted, with line
  add/remove counts and how long ago. Deleted or missing files show in red.
- **Skills / Agents / Tools** (bottom tabs): what the session invoked. Skills and
  Agents show per-session, 7-day, and all-time counts; Tools is per-session.

Left-click a row to select it. Right-click (or press `m`) opens a context menu —
**Open file**, **Show in file browser**, **Copy path**. Press `v` (or click
*View all installed*) to open a separate window listing every skill and agent on
the machine with global usage counts.

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

### 1. Install the package

```bash
pipx install claude-sentry
```

This puts four commands on your `PATH`:

| Command | What it is |
|---|---|
| `claude-sentry` | the sidebar TUI you run |
| `claude-sentry-hook` | the logging hook Claude runs (you don't run this yourself) |
| `claude-sentry-launch` | the auto-dock hook (Windows Terminal only) |
| `claude-sentry-install` | wires the hooks into your Claude settings |

`pipx` is recommended because it isolates the app and guarantees the four
commands land on your `PATH`. Plain `pip install claude-sentry` also works if you
manage your own environment.

### 2. Register the hooks

```bash
claude-sentry-install
```

This edits `~/.claude/settings.json` and adds:

- **`PostToolUse`** hook (matcher `*`) → runs `claude-sentry-hook` after every
  tool call. This is what records edits, deletions, skill/agent/tool usage. It is
  **required** on every platform.
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
  events.jsonl     # one JSON line per tool call, append-only
  config.json      # divider position + chosen theme
  win-session/     # records which Claude session is active per WT window
  locks/           # per-window guards so the pane only auto-launches once
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
git clone <repo> && cd claude-sentry
python -m venv .venv && .venv/bin/pip install -e .   # Windows: .venv\Scripts\pip
claude-sentry-install
```

With an editable install (`-e`), edits to `claude_sentry/` take effect on the
next launch. The app is a single module, `claude_sentry/app.py`; the hooks are
`hook.py` and `launch_hook.py`; the settings wiring is `install.py`.
