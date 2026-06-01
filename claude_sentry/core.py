"""Pure-Python primitives shared by the TUI and the audit-only CLIs.

Nothing in this module imports textual or rich, so it's safe to import from a
lightweight install that has none of the TUI dependencies. The TUI app re-uses
these via direct imports — there is one source of truth for paths, event
loading, native-builtin recognition, confirmations, and aggregation.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths & constants

SENTRY_DIR = Path(os.path.expanduser("~/.claude/sentry"))
LOG_FILE = SENTRY_DIR / "events.jsonl"
CONFIG_FILE = SENTRY_DIR / "config.json"
CONFIRM_FILE = SENTRY_DIR / "confirmations.json"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"


# ---------------------------------------------------------------------------
# Event log

def load_events() -> list[dict]:
    """Read the entire events log. Malformed lines are skipped silently — a
    truncated line at the tail (a hook mid-write) must never crash the reader."""
    if not LOG_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def parse_ts(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Claude-native built-ins. These ship inside Claude Code (no file on disk), so
# we recognise them out of the box instead of dumping them in the review queue.
# Source: https://code.claude.com/docs/en/commands  (built-in commands +
# bundled skills) and https://code.claude.com/docs/en/sub-agents (agents).

NATIVE_COMMANDS = {
    "add-dir", "agents", "allowed-tools", "android", "app", "background",
    "bashes", "batch", "bg", "branch", "btw", "bug", "checkpoint", "chrome",
    "clear", "code-review", "compact", "config", "context", "continue",
    "cost", "debug", "desktop", "diff", "doctor", "effort", "exit",
    "extra-usage", "feedback", "fewer-permission-prompts", "focus", "fork",
    "heapdump", "help", "hooks", "ide", "init", "insights",
    "install-github-app", "install-slack-app", "ios", "keybindings", "login",
    "logout", "mcp", "memory", "mobile", "model", "new", "passes",
    "permissions", "plan", "plugin", "powerup", "privacy-settings",
    "proactive", "quit", "radio", "rc", "recap", "release-notes",
    "reload-plugins", "reload-skills", "remote-control", "remote-env", "reset",
    "resume", "review", "rewind", "routines", "run", "run-skill-generator",
    "sandbox", "schedule", "scroll-speed", "security-review", "settings",
    "setup-bedrock", "setup-vertex", "share", "simplify", "skills", "stats",
    "status", "statusline", "stickers", "stop", "tasks", "team-onboarding",
    "teleport", "terminal-setup", "theme", "tp", "ultrareview", "undo",
    "upgrade", "usage", "usage-credits", "verify", "vim", "claude-api", "loop",
    "deep-research", "fast", "goal", "workflows",
}

# Built-in subagent types Claude Code ships with (case-insensitive).
NATIVE_AGENTS = {"general-purpose", "explore", "plan", "claude"}

DOCS_COMMANDS = "https://code.claude.com/docs/en/commands"
DOCS_AGENTS = "https://code.claude.com/docs/en/sub-agents"


def is_native(kind: str, name: str) -> bool:
    bare = name.split(":", 1)[-1]
    if kind == "agent":
        return bare.lower() in NATIVE_AGENTS
    return bare in NATIVE_COMMANDS


def native_doc_url(kind: str, name: str) -> str:
    """A deep link to the Claude docs that highlights this command/agent."""
    bare = name.split(":", 1)[-1]
    if kind == "agent":
        return DOCS_AGENTS
    # Text fragment (#:~:text=) scrolls to and highlights the command on load.
    return f"{DOCS_COMMANDS}#:~:text=%2F{bare}"


# ---------------------------------------------------------------------------
# Skill / agent file resolution

def find_skill_or_agent_file(kind: str, name: str) -> Path | None:
    """Resolve a recorded skill/agent name back to its source file."""
    if ":" in name:
        _, name = name.split(":", 1)
    if kind == "agent":
        candidates = [CLAUDE_DIR / "agents" / f"{name}.md"]
        for p in CLAUDE_DIR.glob(f"plugins/**/agents/{name}.md"):
            return p
    else:
        candidates = [
            CLAUDE_DIR / "skills" / name / "SKILL.md",
            CLAUDE_DIR / "commands" / f"{name}.md",
        ]
        for p in CLAUDE_DIR.glob(f"plugins/**/skills/{name}/SKILL.md"):
            return p
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Confirmations — permanent confirm/deny decisions for skills/agents that
# aren't backed by a file on disk (plugins we can't resolve, typos, …).
# Keyed "kind::name", e.g. "skill::verify", "agent::general-purpose".

def load_confirmations() -> dict:
    try:
        data = json.loads(CONFIRM_FILE.read_text(encoding="utf-8"))
        return {
            "confirmed": set(data.get("confirmed", [])),
            "denied": set(data.get("denied", [])),
        }
    except Exception:
        return {"confirmed": set(), "denied": set()}


def save_confirmations(state: dict) -> None:
    try:
        SENTRY_DIR.mkdir(parents=True, exist_ok=True)
        CONFIRM_FILE.write_text(
            json.dumps(
                {"confirmed": sorted(state["confirmed"]),
                 "denied": sorted(state["denied"])},
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def confirm_keys(keys: set[str]) -> None:
    state = load_confirmations()
    state["confirmed"] |= keys
    state["denied"] -= keys
    save_confirmations(state)


def deny_keys(keys: set[str]) -> None:
    state = load_confirmations()
    state["denied"] |= keys
    state["confirmed"] -= keys
    save_confirmations(state)


# ---------------------------------------------------------------------------
# Status classification: same rules the TUI uses, lifted into pure code so the
# audit report agrees with what the sidebar shows.

def status_of(kind: str, name: str, confirmations: dict | None = None) -> str:
    """One of: verified | native | confirmed | denied | unconfirmed.

    An on-disk file always wins, even over a 'denied' decision — a user who
    later installed something they once dismissed shouldn't have to re-confirm.
    """
    if find_skill_or_agent_file(kind, name) is not None:
        return "verified"
    if is_native(kind, name):
        return "native"
    conf = confirmations if confirmations is not None else load_confirmations()
    key = f"{kind}::{name}"
    if key in conf["denied"]:
        return "denied"
    if key in conf["confirmed"]:
        return "confirmed"
    return "unconfirmed"


# ---------------------------------------------------------------------------
# Aggregation

def aggregate_counts(events_: list[dict], kind: str,
                     within_days: int | None = None) -> dict[str, dict]:
    """Return {name: {'window': int, 'all': int}} for the given event type.

    `within_days` controls the 'window' bucket — passing None means all-time
    only (window == all). The original TUI uses a fixed 7-day window; the audit
    CLI passes 30 (or whatever the user asked for via --days)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)
              if within_days is not None else None)
    counts: dict[str, dict] = {}
    for e in events_:
        if e.get("type") != kind:
            continue
        name = e.get("target") or ""
        if not name:
            continue
        rec = counts.setdefault(name, {"window": 0, "all": 0})
        rec["all"] += 1
        if cutoff is None or parse_ts(e.get("ts", "")) >= cutoff:
            rec["window"] += 1
    return counts
