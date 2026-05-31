"""claude-sentry-install: idempotently wire claude-sentry's hooks into
~/.claude/settings.json.

Run with `--uninstall` to remove them. Run with `--no-launcher` on Windows to
skip the SessionStart auto-launch hook (you'll start the sidebar by running
`claude-sentry` yourself).
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


def install(with_launcher: bool) -> None:
    cfg = _load()
    hooks = cfg.setdefault("hooks", {})
    added: list[str] = []
    if _add_hook(hooks.setdefault("PostToolUse", []), POST_MATCHER, HOOK_CMD):
        added.append(f"PostToolUse[{POST_MATCHER}] → {HOOK_CMD}")
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


def uninstall() -> None:
    cfg = _load()
    hooks = cfg.get("hooks", {})
    removed: list[str] = []
    for ev, matcher, cmd in (
        ("PostToolUse", POST_MATCHER, HOOK_CMD),
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
    args = p.parse_args()
    if args.uninstall:
        uninstall()
    else:
        install(with_launcher=not args.no_launcher)


if __name__ == "__main__":
    main()
