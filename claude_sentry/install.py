"""claude-sentry-install: idempotently wire claude-sentry's hooks into
~/.claude/settings.json.

Flags:
  --uninstall    Remove all claude-sentry hooks from settings.json.
  --no-launcher  Skip the SessionStart auto-launch hook (Windows only).
  --audit-only   Lightweight install: skip the launcher hook AND print a hint
                 pointing the user at claude-sentry-report / -confirm / -deny
                 instead of the sidebar TUI. Use this when you only want the
                 background event log + monthly auditing, with no TUI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))

HOOK_CMD = "claude-sentry-hook"
LAUNCH_CMD = "claude-sentry-launch"

# A single PostToolUse hook with a wildcard matcher catches everything —
# edits, Bash, Skill, Task. Adding a second PreToolUse hook for Skill/Task
# would double-count those, because PostToolUse[*] already fires for them.
POST_MATCHER = "*"
PRE_MATCHER = "Skill|Task|Agent"  # legacy — removed on install if present


def _load() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        print(f"warn: could not parse {SETTINGS}; aborting.", file=sys.stderr)
        sys.exit(1)


def _save(cfg: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _has_hook(entries: list, matcher: str, cmd: str) -> bool:
    for e in entries:
        if e.get("matcher") != matcher:
            continue
        for h in e.get("hooks", []) or []:
            if h.get("command") == cmd:
                return True
    return False


def _add_hook(entries: list, matcher: str, cmd: str) -> bool:
    if _has_hook(entries, matcher, cmd):
        return False
    entries.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": cmd}],
    })
    return True


def _remove_hook(entries: list, matcher: str, cmd: str) -> bool:
    removed = False
    new_entries = []
    for e in entries:
        if e.get("matcher") != matcher:
            new_entries.append(e)
            continue
        hooks = [h for h in (e.get("hooks") or []) if h.get("command") != cmd]
        if hooks:
            new_entries.append({**e, "hooks": hooks})
        else:
            removed = True
    if removed:
        entries[:] = new_entries
    return removed


def install(with_launcher: bool, audit_only: bool = False) -> None:
    cfg = _load()
    hooks = cfg.setdefault("hooks", {})
    added: list[str] = []
    if _add_hook(hooks.setdefault("PostToolUse", []), POST_MATCHER, HOOK_CMD):
        added.append(f"PostToolUse[{POST_MATCHER}] → {HOOK_CMD}")
    # UserPromptSubmit captures user-typed /slash-commands (they never reach the
    # Skill tool, so PostToolUse alone can't see them).
    if _add_hook(hooks.setdefault("UserPromptSubmit", []), "*", HOOK_CMD):
        added.append(f"UserPromptSubmit[*] → {HOOK_CMD}")
    # Remove the legacy PreToolUse Skill|Task|Agent hook if a previous install
    # added it — it double-counts against PostToolUse[*].
    if "PreToolUse" in hooks and _remove_hook(hooks["PreToolUse"], PRE_MATCHER, HOOK_CMD):
        added.append(f"(removed legacy PreToolUse[{PRE_MATCHER}] to stop double-count)")
    if with_launcher and sys.platform == "win32":
        if _add_hook(hooks.setdefault("SessionStart", []), "*", LAUNCH_CMD):
            added.append(f"SessionStart[*] → {LAUNCH_CMD}")
    _save(cfg)
    if added:
        print("Installed:")
        for a in added:
            print(f"  + {a}")
    else:
        print("All hooks already installed — nothing to do.")
    print(f"\nSettings file: {SETTINGS}")
    if audit_only:
        print(
            "\nAudit-only mode — no sidebar will launch. Useful commands:\n"
            "  claude-sentry-report          # last 30 days of skill/agent/tool usage\n"
            "  claude-sentry-report --days 7 # last 7 days\n"
            "  claude-sentry-confirm <name>  # mark an unconfirmed item as real\n"
            "  claude-sentry-deny <name>     # dismiss an unconfirmed item (typo, etc.)"
        )


def uninstall() -> None:
    cfg = _load()
    hooks = cfg.get("hooks", {})
    removed: list[str] = []
    for ev, matcher, cmd in (
        ("PostToolUse", POST_MATCHER, HOOK_CMD),
        ("UserPromptSubmit", "*", HOOK_CMD),
        ("PreToolUse", PRE_MATCHER, HOOK_CMD),
        ("SessionStart", "*", LAUNCH_CMD),
    ):
        if ev in hooks and _remove_hook(hooks[ev], matcher, cmd):
            removed.append(f"{ev}[{matcher}] → {cmd}")
    _save(cfg)
    if removed:
        print("Removed:")
        for r in removed:
            print(f"  − {r}")
    else:
        print("Nothing to remove.")


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-sentry-install")
    p.add_argument("--uninstall", action="store_true",
                   help="Remove claude-sentry's hooks from settings.json.")
    p.add_argument("--no-launcher", action="store_true",
                   help="Skip the SessionStart auto-launch hook (Windows only).")
    p.add_argument("--audit-only", action="store_true",
                   help="Lightweight install: skip the launcher hook and print "
                        "hints for the report/confirm/deny CLIs instead of the TUI.")
    args = p.parse_args()
    if args.uninstall:
        uninstall()
    else:
        # --audit-only implies --no-launcher (the launcher only makes sense
        # alongside the sidebar TUI).
        with_launcher = not (args.no_launcher or args.audit_only)
        install(with_launcher=with_launcher, audit_only=args.audit_only)


if __name__ == "__main__":
    main()
