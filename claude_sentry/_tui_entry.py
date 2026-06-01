"""Thin shim for the `claude-sentry` console script.

The TUI requires `textual`, which is now an *optional* extra. Without that
extra, importing `claude_sentry.app` fails with a confusing ModuleNotFoundError
deep inside its top-of-file imports. This wrapper catches that one specific
case and prints a friendly install hint, so the lightweight (audit-only)
install path remains uncluttered.
"""
from __future__ import annotations

import sys


def main() -> None:
    try:
        from . import app  # noqa: WPS433 — deferred on purpose
    except ImportError as exc:
        # We only want to pretty-print the missing-textual case. Any *other*
        # ImportError is a real bug — let it surface with its full traceback.
        if exc.name not in {"textual", "rich"}:
            raise
        sys.stderr.write(
            "claude-sentry: the sidebar TUI requires the optional 'tui' extra.\n"
            "\n"
            "Install it with:\n"
            "    pipx install 'claude-sentry[tui]'\n"
            "or, if claude-sentry is already installed via pipx:\n"
            "    pipx inject claude-sentry 'textual>=0.86,<9'\n"
            "\n"
            "Audit-only use (events.jsonl + claude-sentry-report) does NOT need "
            "this extra — only the live sidebar does.\n"
        )
        sys.exit(1)
    app.main()
