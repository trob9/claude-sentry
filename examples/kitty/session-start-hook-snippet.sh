#!/usr/bin/env bash
# session-start-hook-snippet.sh
#
# Drop this block into your Claude Code SessionStart hook to enable
# claude-with-sentry auto-linking and /resume support.
#
# Prerequisites:
#   - The hook receives JSON on stdin (session_id, transcript_path, etc.)
#   - You must have already read hook input into _HOOK_INPUT before this block
#     runs. If this is your whole hook, add the read line shown below.
#
# Usage — source this from your hook, or paste the block directly:
#
#   _HOOK_INPUT=$(cat)   # read hook JSON (only needed once per hook file)
#   source /path/to/session-start-hook-snippet.sh

# ── claude-sentry session linking ────────────────────────────────────────────
# If this session was started by claude-with-sentry, write the session_id to a
# state file so the sentry pane wrapper can pick it up and launch claude-sentry
# scoped to this session. On /resume, the state file already exists with a
# sentry_pid — preserve it and signal SIGUSR1 so the running claude-sentry
# re-reads session_id and re-scopes its view.
if [[ -n "${CLAUDE_SENTRY_LINK_ID:-}" ]]; then
    _SENTRY_SESSION=$(echo "$_HOOK_INPUT" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
    if [[ -n "$_SENTRY_SESSION" ]]; then
        mkdir -p ~/.claude/state/sentry-links
        _SENTRY_STATE="$HOME/.claude/state/sentry-links/$CLAUDE_SENTRY_LINK_ID.json"
        if [[ -f "$_SENTRY_STATE" ]]; then
            _SENTRY_EXISTING=$(cat "$_SENTRY_STATE")
            # Preserve sentry_pid while updating session_id
            echo "$_SENTRY_EXISTING" | python3 -c "
import sys, json
data = json.load(sys.stdin)
data['session_id'] = '$_SENTRY_SESSION'
print(json.dumps(data))
" > "$_SENTRY_STATE" 2>/dev/null || \
                printf '{"session_id":"%s"}\n' "$_SENTRY_SESSION" > "$_SENTRY_STATE"
            # Signal the sentry app to reload onto the new session
            _SENTRY_PID=$(echo "$_SENTRY_EXISTING" | python3 -c \
                "import sys,json; print(json.load(sys.stdin).get('sentry_pid',''))" 2>/dev/null)
            if [[ -n "$_SENTRY_PID" ]] && kill -0 "$_SENTRY_PID" 2>/dev/null; then
                kill -USR1 "$_SENTRY_PID" 2>/dev/null || true
            fi
        else
            printf '{"session_id":"%s"}\n' "$_SENTRY_SESSION" > "$_SENTRY_STATE"
        fi
    fi
fi
# ─────────────────────────────────────────────────────────────────────────────
