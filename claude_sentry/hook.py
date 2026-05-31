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
DEBUG_FILE = LOG_DIR / "debug.log"

# Keep the log bounded: once it passes the cap, prune to the most recent lines.
# ~120 bytes/line, so 12 MB ≈ 100k events — months of heavy use.
LOG_MAX_BYTES = 12 * 1024 * 1024
LOG_KEEP_LINES = 60_000
DEBUG_MAX_BYTES = 1 * 1024 * 1024


def _debug(where: str, exc: BaseException) -> None:
    """Record a swallowed error so failures aren't completely invisible. Itself
    fail-silent and size-capped — the hook must never crash the parent tool."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if DEBUG_FILE.exists() and DEBUG_FILE.stat().st_size > DEBUG_MAX_BYTES:
            DEBUG_FILE.write_text("", encoding="utf-8")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with DEBUG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{ts} hook {where}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def _maybe_trim() -> None:
    """If the log has grown past the cap, keep only the most recent lines."""
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size <= LOG_MAX_BYTES:
            return
        with LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= LOG_KEEP_LINES:
            return
        tmp = LOG_FILE.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(lines[-LOG_KEEP_LINES:])
        os.replace(tmp, LOG_FILE)  # atomic swap
    except Exception as exc:
        _debug("trim", exc)


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


def _norm(path) -> str:
    """Store paths with forward slashes so the same file logged by an Edit and
    by a Bash `rm` (which already normalises) dedups to one Activity row."""
    return str(path).replace("\\", "/")


def classify(tool_name: str, tool_input: dict, cwd: str) -> list[dict]:
    """Return zero or more event payload dicts. Bash deletions can yield many."""
    if tool_name == "Edit":
        path = tool_input.get("file_path")
        if not path:
            return []
        return [{
            "type": "edit", "action": "edited", "target": _norm(path),
            "removed": _lines(tool_input.get("old_string", "")),
            "added": _lines(tool_input.get("new_string", "")),
        }]
    if tool_name == "Write":
        path = tool_input.get("file_path")
        if not path:
            return []
        return [{
            "type": "edit", "action": "created", "target": _norm(path),
            "removed": 0,
            "added": _lines(tool_input.get("content", "")),
        }]
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path")
        if not path:
            return []
        edits = tool_input.get("edits", []) or []
        return [{
            "type": "edit", "action": "edited", "target": _norm(path),
            "removed": sum(_lines(e.get("old_string", "")) for e in edits),
            "added": sum(_lines(e.get("new_string", "")) for e in edits),
        }]
    if tool_name == "NotebookEdit":
        path = tool_input.get("notebook_path")
        if not path:
            return []
        return [{"type": "edit", "action": "edited", "target": _norm(path),
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


def parse_slash_command(prompt: str) -> str | None:
    """Return the command name from a prompt that starts with `/name`, else None.

    Only a LEADING slash counts (mid-text slashes like a file path are not
    commands). We log every such command without judging whether it's "real" —
    the viewer sorts genuine commands from typos via its confirm/deny queue, so
    a brand-new built-in command is never silently dropped here."""
    if not prompt:
        return None
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return None
    m = _SLASH_RE.match(stripped)
    return m.group(1) if m else None


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
    # Build the whole block and write it in a single call. A lone append of a
    # small block is effectively atomic, so two concurrent Claude sessions can't
    # interleave each other's lines mid-record.
    try:
        block = "".join(
            json.dumps({"ts": ts, "session_id": session_id, "cwd": cwd, **fields},
                       ensure_ascii=False) + "\n"
            for fields in events_
        )
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(block)
    except Exception as exc:
        _debug("append", exc)
        return
    _maybe_trim()


if __name__ == "__main__":
    sys.exit(main())
