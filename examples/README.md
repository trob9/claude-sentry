# Launcher examples

claude-sentry can auto-link to a Claude session and re-link on `/resume`
without any manual `l`-paste step — you just need a launcher that wires
two things up. Working reference implementations live in this folder:

| Terminal | Launcher | Status |
|---|---|---|
| Kitty (macOS / Linux) | [`kitty/claude-with-sentry`](kitty/claude-with-sentry) — 102 lines | tested |
| iTerm2 (macOS)        | [`iterm2/claude-with-sentry`](iterm2/claude-with-sentry) — 108 lines | tested |

Each is a complete, self-contained bash script you can drop into `~/.local/bin/`
and run. They're not abstract templates — they handle UUID generation, env-var
snapshotting (proxies, CA bundles, etc.), pane creation, the polling sentry
wrapper, and cleanup. Read them; they're short.

---

## Quickstart

```bash
# 1. Copy the launcher for your terminal
cp examples/kitty/claude-with-sentry   ~/.local/bin/   # or iterm2/

# 2. Install the SessionStart hook snippet (next section explains what it does)
#    — paste the snippet into your existing ~/.claude/hooks/session-start-hook.sh,
#    or symlink the file in if you don't have a hook yet.

# 3. Run it
claude-with-sentry
```

A new window opens with `claude --verbose` on the left and `claude-sentry`
docked on the right, linked to the new session. `/resume` to another session
inside Claude → the sentry pane swaps within ~2 seconds.

---

## The contract (what every launcher must do)

1. **Generate a fresh UUID** and export it as `CLAUDE_SENTRY_LINK_ID` in the
   environment of **both** the Claude pane and the claude-sentry pane before
   either process starts.
2. **On each `SessionStart` hook** (fires when Claude creates or resumes a
   session), write the new `session_id` to
   `~/.claude/state/sentry-links/$CLAUDE_SENTRY_LINK_ID.json`.

claude-sentry handles the rest — it polls that file on its 2-second refresh
tick and re-scopes whenever the `session_id` changes. No signals, no PID
lifecycle, no special permissions. Multiple terminal windows are naturally
isolated because each gets its own UUID and therefore its own state file.

---

## The hook snippet (#2 of the contract)

The hook code is **identical across terminals** because it just writes a file.
This is the entire snippet — paste it into your existing
`~/.claude/hooks/session-start-hook.sh`, or symlink the file if your hook
doesn't exist yet:

```bash
#!/usr/bin/env bash
# Read hook JSON into _HOOK_INPUT *once* per hook file. If your hook already
# does this (or reads stdin via $(cat)), skip this line.
_HOOK_INPUT=$(cat)

# ── claude-sentry session linking ────────────────────────────────────────────
# If this session was started by a claude-sentry launcher (CLAUDE_SENTRY_LINK_ID
# set), write the session_id to a state file. The paired claude-sentry app
# polls this file on its 2-second refresh tick and re-scopes itself when the
# session_id changes — that's how /resume auto-switches the sidebar.
if [[ -n "${CLAUDE_SENTRY_LINK_ID:-}" ]]; then
    _SENTRY_SESSION=$(echo "$_HOOK_INPUT" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
    if [[ -n "$_SENTRY_SESSION" ]]; then
        mkdir -p ~/.claude/state/sentry-links
        _SENTRY_STATE="$HOME/.claude/state/sentry-links/$CLAUDE_SENTRY_LINK_ID.json"
        if [[ -f "$_SENTRY_STATE" ]]; then
            # Preserve other fields (e.g. sentry_pid) while updating session_id
            _SENTRY_EXISTING=$(cat "$_SENTRY_STATE")
            echo "$_SENTRY_EXISTING" | python3 -c "
import sys, json
data = json.load(sys.stdin)
data['session_id'] = '$_SENTRY_SESSION'
print(json.dumps(data))
" > "$_SENTRY_STATE" 2>/dev/null || \
                printf '{"session_id":"%s"}\n' "$_SENTRY_SESSION" > "$_SENTRY_STATE"
        else
            printf '{"session_id":"%s"}\n' "$_SENTRY_SESSION" > "$_SENTRY_STATE"
        fi
    fi
fi
```

Same code lives at [`kitty/session-start-hook-snippet.sh`](kitty/session-start-hook-snippet.sh)
(filed under `kitty/` for historical reasons — it works with any launcher
that follows the contract).

---

## Anatomy of a launcher (#1 of the contract)

All launchers in this folder follow the same five-step shape. Below is the
shape annotated against `kitty/claude-with-sentry` — `iterm2/claude-with-sentry`
is identical except for step 4.

```
┌── Step ──────────────────────────────────────────────┬── Lines in kitty launcher ──┐
│ 1. Generate fresh CLAUDE_SENTRY_LINK_ID UUID         │ ~17                          │
│ 2. Write an env file that both panes will source     │ ~20–34                       │
│    (snapshot PATH, proxies, CA bundles, the UUID)    │                              │
│ 3. Write a run-claude.sh that:                       │ ~37–53                       │
│      sources env file → runs `claude --verbose`      │                              │
│      removes state file on Claude exit               │                              │
│ 4. Write a run-sentry.sh that:                       │ ~55–82                       │
│      sources env file → polls state file for up to   │                              │
│      15s → launches `claude-sentry --session <id>`   │                              │
│      or falls back to `--all-sessions` on timeout    │                              │
│ 5. Open a new terminal window with the two panes     │ ~85–98 (kitty session.conf)  │
│      (the terminal-specific bit — `kitty @ launch`,  │ (iterm2: ~95–107 via         │
│       `osascript` for iTerm2, etc.)                  │  osascript)                  │
└──────────────────────────────────────────────────────┴──────────────────────────────┘
```

The only step that changes per terminal is step 5. Steps 1–4 are pure bash
that works anywhere.

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

For a new terminal (WezTerm, Alacritty, tmux, …), copy whichever existing
launcher is closest and replace only **step 5** (the terminal-control block).
Steps 1–4 don't need to change.

PRs welcome.
