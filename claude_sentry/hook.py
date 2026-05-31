"""claude-sentry: hook that logs tool invocations to events.jsonl.

Reads the hook JSON payload from stdin and appends one event per call:
  {"ts": iso8601, "type": "edit"|"skill"|"agent"|"tool", "target": str, ...}

Handles two hook events:
  * PostToolUse      — every tool call (edits, deletions, Skill/Agent, tallies)
  * UserPromptSubmit — a user-typed `/slash-command`, logged as a skill so it
    shows up in the sidebar (these never go through the Skill *tool*, so they'd
    otherwise be invisible).

Designed to be cheap and fail-silent: never blocks the parent call.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(os.path.expanduser("~/.claude/sentry"))
LOG_FILE = LOG_DIR / "events.jsonl"


def _lines(s: str) -> int:
    if not s:
        return 0
    return len(s.splitlines())


# A "segment" is one command between shell separators. We split the whole
# command line on &&  ||  ;  |  and newlines, then only inspect segments that
# *start* with a delete command. This stops `rm a && echo done` from treating
# "echo" and "done" as files.
_SEGMENT_SPLIT = re.compile(r"(?:&&|\|\||[;&|\n])")
_DELETE_HEAD = re.compile(r"^\s*(?:sudo\s+)?(rm|del|erase|Remove-Item|ri)\b(.*)$",
                          re.IGNORECASE)


def _looks_like_path(tok: str) -> bool:
    """Reject things that aren't a concrete file target."""
    if not tok or tok.startswith("-"):
        return False
    # Glob patterns can't be resolved to a single real file — skip them.
    if any(c in tok for c in "*?[]"):
        return False
    # Redirections / fds / operators that survived a bad split.
    if any(c in tok for c in "<>"):
        return False
    if tok in ("2", "1", "&", "/dev/null", "$null", "NUL"):
        return False
    # PowerShell named-parameter values like -Path are already handled by the
    # leading-dash check; bare bareword "Force"/"Recurse" can't be distinguished
    # from a real file, so we accept them but they'll be filtered by existence
    # downstream (the file genuinely won't exist).
    return True


def _parse_deletions(command: str, cwd: str) -> list[str]:
    """Best-effort: extract concrete file targets from delete commands.

    Conservative by design — when in doubt, emit nothing rather than garbage.
    """
    targets: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        m = _DELETE_HEAD.match(segment)
        if not m:
            continue
        tail = m.group(2)
        try:
            parts = shlex.split(tail, posix=True)
        except ValueError:
            parts = tail.split()
        for p in parts:
            if not _looks_like_path(p):
                continue
            if cwd and not os.path.isabs(p):
                p = os.path.join(cwd, p)
            targets.append(p.replace("\\", "/"))
    return targets


def classify(tool_name: str, tool_input: dict, cwd: str) -> list[dict]:
    """Return zero or more event payload dicts. Bash deletions can yield many."""
    if tool_name == "Edit":
        path = tool_input.get("file_path")
        if not path:
            return []
        return [{
            "type": "edit", "action": "edited", "target": str(path),
            "removed": _lines(tool_input.get("old_string", "")),
            "added": _lines(tool_input.get("new_string", "")),
        }]
    if tool_name == "Write":
        path = tool_input.get("file_path")
        if not path:
            return []
        return [{
            "type": "edit", "action": "created", "target": str(path),
            "removed": 0,
            "added": _lines(tool_input.get("content", "")),
        }]
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path")
        if not path:
            return []
        edits = tool_input.get("edits", []) or []
        return [{
            "type": "edit", "action": "edited", "target": str(path),
            "removed": sum(_lines(e.get("old_string", "")) for e in edits),
            "added": sum(_lines(e.get("new_string", "")) for e in edits),
        }]
    if tool_name == "NotebookEdit":
        path = tool_input.get("notebook_path")
        if not path:
            return []
        return [{"type": "edit", "action": "edited", "target": str(path),
                 "removed": 0, "added": 0}]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""
        targets = _parse_deletions(cmd, cwd)
        return [{"type": "edit", "action": "deleted", "target": t,
                 "removed": 0, "added": 0} for t in targets]
    if tool_name == "Skill":
        skill = tool_input.get("skill")
        if not skill:
            return []
        return [{"type": "skill", "target": str(skill)}]
    if tool_name in ("Agent", "Task"):
        agent = tool_input.get("subagent_type") or "general-purpose"
        return [{"type": "agent", "target": str(agent)}]
    return []


_SLASH_RE = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_:-]*)")
_CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))

# Built-in Claude Code slash commands that have no file on disk (they're bundled
# in the CLI). Kept short and only the common ones — anything not here and not
# on disk is treated as a typo/unknown and skipped.
_BUILTIN_COMMANDS = {
    "verify", "run", "init", "review", "security-review", "code-review",
    "simplify", "claude-api", "loop", "schedule", "update-config",
    "keybindings-help", "fewer-permission-prompts",
    "status", "model", "clear", "compact", "cost", "resume", "help",
    "config", "memory", "agents", "mcp", "doctor", "login", "logout",
}


def command_exists(name: str) -> bool:
    """True if `name` is a real slash command — an installed skill/command on
    disk, or a known built-in. Filters out typos like `/test`."""
    bare = name.split(":", 1)[1] if ":" in name else name
    if bare in _BUILTIN_COMMANDS:
        return True
    if (_CLAUDE_DIR / "skills" / bare / "SKILL.md").exists():
        return True
    if (_CLAUDE_DIR / "commands" / f"{bare}.md").exists():
        return True
    for _ in _CLAUDE_DIR.glob(f"plugins/**/skills/{bare}/SKILL.md"):
        return True
    for _ in _CLAUDE_DIR.glob(f"plugins/**/commands/{bare}.md"):
        return True
    return False


def parse_slash_command(prompt: str) -> str | None:
    """Return the command name from a prompt that starts with `/name`, else None.
    Ignores prompts that merely contain a slash mid-text (e.g. a file path), and
    unknown commands that don't resolve to an installed or built-in command."""
    if not prompt:
        return None
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return None
    m = _SLASH_RE.match(stripped)
    if not m:
        return None
    name = m.group(1)
    return name if command_exists(name) else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    hook_event = payload.get("hook_event_name", "")

    # UserPromptSubmit: capture a leading /slash-command as a skill event.
    if hook_event == "UserPromptSubmit" or ("prompt" in payload and "tool_name" not in payload):
        cmd = parse_slash_command(payload.get("prompt", "") or "")
        if not cmd:
            return 0
        _append([{"type": "skill", "target": cmd}], session_id, cwd)
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    events_ = classify(tool_name, tool_input, cwd)
    # Always log a tool-call event for per-session tool tallies
    if tool_name:
        events_.append({"type": "tool", "target": tool_name})
    if not events_:
        return 0
    _append(events_, session_id, cwd)
    return 0


def _append(events_: list[dict], session_id: str, cwd: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            for fields in events_:
                event = {"ts": ts, "session_id": session_id, "cwd": cwd, **fields}
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
