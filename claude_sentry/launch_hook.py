"""claude-sentry: SessionStart hook that docks the sidebar pane.

Guards:
  * only fires inside Windows Terminal (WT_SESSION present)
  * only fires once per (WT_SESSION, Claude session_id) — so resumes and
    nested sessions don't keep spawning new panes

The pane is passed --session <claude-session-id> so it can filter edits to
just this conversation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SENTRY_DIR = Path(os.path.expanduser("~/.claude/sentry"))
LOCK_DIR = SENTRY_DIR / "locks"
WIN_SESSION_DIR = SENTRY_DIR / "win-session"


def main() -> int:
    wt_session = os.environ.get("WT_SESSION")
    if not wt_session:
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    claude_session = payload.get("session_id", "") or ""

    # Record which Claude session is active in this WT window, so a manual
    # `claude-sentry` restart in the same window can re-attach to it.
    if claude_session:
        try:
            WIN_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            (WIN_SESSION_DIR / f"{wt_session}.txt").write_text(
                claude_session, encoding="utf-8"
            )
        except Exception:
            pass

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    # Lock key combines wt window + claude session — resumed sessions reuse it.
    lock = LOCK_DIR / f"{wt_session}__{claude_session}.lock"
    if lock.exists():
        return 0
    try:
        lock.write_text("")
    except Exception:
        return 0

    cmd = ["wt", "-w", "0", "split-pane", "-s", "0.25", "cmd", "/k",
           "claude-sentry"]
    if claude_session:
        cmd.extend(["--session", claude_session])
    try:
        subprocess.Popen(cmd, creationflags=0x00000008)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
