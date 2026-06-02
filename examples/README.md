# Launcher examples

claude-sentry can auto-link to a Claude session and re-link on `/resume`
without any manual `l`-paste step — you just need a launcher that wires
two things up. Each subdirectory here is a reference implementation of
the same pattern for a different terminal.

| Terminal | Launcher | Status |
|---|---|---|
| Kitty (macOS / Linux) | [`kitty/claude-with-sentry`](kitty/claude-with-sentry) | tested |
| iTerm2 (macOS) | [`iterm2/claude-with-sentry`](iterm2/claude-with-sentry) | tested |

---

## The contract

A compatible launcher must do two things:

1. **Generate a fresh UUID** and export it as `CLAUDE_SENTRY_LINK_ID` in the
   environment of **both** the Claude pane and the claude-sentry pane before
   either process starts.
2. **On each `SessionStart` hook** (fires when Claude creates or resumes a
   session), write the new `session_id` to
   `~/.claude/state/sentry-links/$CLAUDE_SENTRY_LINK_ID.json`.

claude-sentry handles the rest — it polls that file on its 2-second refresh
tick and re-scopes whenever the `session_id` changes.

That's the whole API. No signals, no PID lifecycle, no special permissions.
Multiple terminal windows are naturally isolated because each gets its own
UUID and therefore its own state file.

---

## The shared hook snippet

The hook code is **identical across terminals** because it just writes a file.
A drop-in snippet you can `source` from your existing
`~/.claude/hooks/session-start-hook.sh` lives at:

  [`kitty/session-start-hook-snippet.sh`](kitty/session-start-hook-snippet.sh)

(It's filed under `kitty/` for historical reasons — it works with any launcher
that follows the contract above. If you're only using iTerm2, you can copy or
symlink it into your own hooks directory; you don't need to keep the
`kitty/` folder around.)

To install it, ensure your `SessionStart` hook reads stdin into `_HOOK_INPUT`
once, then `source`s the snippet:

```bash
# ~/.claude/hooks/session-start-hook.sh
_HOOK_INPUT=$(cat)
source ~/path/to/session-start-hook-snippet.sh
# ... rest of your hook
```

---

## Using a launcher

Drop the launcher script into `~/.local/bin/` (or anywhere on your `PATH`)
and run it. It opens a new terminal window with two panes:

- **Left pane:** `claude --verbose`
- **Right pane:** waits ~1 s for the SessionStart hook to write the state
  file, then launches `claude-sentry --session <id>`. Falls back to
  `--all-sessions` after 15 s if nothing arrives.

You can wire it to your OS so a launcher icon (Dock, Spotlight, Start menu)
opens the whole setup in one click — for macOS, a minimal `.app` bundle whose
`Contents/MacOS/Claude` shell script just calls the launcher works well.

---

## Per-terminal notes

### Kitty

Requires the **splits layout** to be enabled in `~/.config/kitty/kitty.conf`:

```
enabled_layouts splits,stack
```

See the [Kitty setup section of the main README](../README.md#kitty-setup) for
recommended keybinds to widen/narrow the sidebar pane.

### iTerm2

The launcher drives iTerm2 via AppleScript (`osascript`). First run will pop
a macOS permission dialog asking whether your shell (Terminal.app, Kitty,
etc.) may control iTerm — grant it under **System Settings → Privacy &
Security → Automation**.

iTerm2 AppleScript splits 50/50 by default; drag the divider or use
`cmd+shift+]` / `cmd+shift+[` to nudge the sentry pane to your preferred
width. The launcher could add a `set columns of sentrySession to N` line if
you want a fixed start width — left out by default since the right ratio
depends on your font size and screen width.

---

## Writing your own launcher

The two-bullet contract is small enough that adapting it to a new terminal
(WezTerm, Alacritty, tmux, Windows Terminal as a custom launcher, …) is
mostly a copy-paste exercise:

1. Copy either reference launcher.
2. Replace the terminal-control block (the `kitty @ launch …` / `osascript …`
   section) with whatever your terminal exposes for opening a window and
   splitting it.
3. Keep everything else — the UUID generation, env-file snapshot, sentry
   wrapper that polls the state file — unchanged.

PRs welcome.
