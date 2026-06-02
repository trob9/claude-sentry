"""claude-sentry-report: headless audit of events.jsonl.

Prints (in order):
  1. Unresolved items — anything that *looks* like a skill or agent but isn't
     installed on disk and isn't a Claude built-in. The user should triage
     these with `claude-sentry-confirm <name>` / `claude-sentry-deny <name>`.
     They're surfaced at the top because they distort the real counts until
     resolved.
  2. Skills usage (verified + native + confirmed; denied are excluded).
  3. Agents usage (same rules).
  4. Tools usage (no native/confirm logic — every tool name is real).

Use `--days N` to restrict the window column (default 30). `--all` drops the
window column and shows all-time counts only. `--json` emits a machine-readable
dump instead of the human table.

No textual / rich dependency — runs on the lightweight install.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from .core import (
    LOG_FILE,
    aggregate_counts,
    load_confirmations,
    load_events,
    parse_ts,
    status_of,
)

# Items in these statuses are "real" usage and roll up into the main tables.
REAL_STATUSES = ("verified", "native", "confirmed")


def _filter_by_days(events_: list[dict], days: int | None) -> list[dict]:
    if days is None:
        return events_
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [e for e in events_ if parse_ts(e.get("ts", "")) >= cutoff]


def _classify_skill_agent(events_: list[dict]) -> dict:
    """Group skill/agent events into {kind: {status: {name: count}}}.

    Walk the events once and bucket each (kind, name) by its current status.
    Confirmation state is loaded once and reused for every check (each call to
    `status_of` otherwise re-reads the JSON file).
    """
    conf = load_confirmations()
    out: dict[str, dict[str, Counter]] = {
        "skill": {s: Counter() for s in REAL_STATUSES + ("unconfirmed", "denied", "renamed")},
        "agent": {s: Counter() for s in REAL_STATUSES + ("unconfirmed", "denied", "renamed")},
    }
    for e in events_:
        kind = e.get("type")
        if kind not in ("skill", "agent"):
            continue
        name = e.get("target") or ""
        if not name:
            continue
        status = status_of(kind, name, conf)
        out[kind][status][name] += 1
    return out


def _print_table(title: str, rows: list[tuple[str, int, int]],
                 *, window_label: str, show_window: bool) -> None:
    """Two-or-three-column table (name | window | all). No external deps."""
    print(f"\n{title}")
    if not rows:
        print("  (no usage in this window)")
        return
    name_w = max(4, min(40, max(len(r[0]) for r in rows)))
    head = f"  {'name':<{name_w}}"
    if show_window:
        head += f"  {window_label:>6}"
    head += f"  {'all':>6}"
    print(head)
    print("  " + "-" * (name_w + (10 if show_window else 0) + 8))
    for name, window, all_ in rows:
        line = f"  {name[:name_w]:<{name_w}}"
        if show_window:
            line += f"  {window:>6}"
        line += f"  {all_:>6}"
        print(line)


def _print_unresolved(classified: dict) -> int:
    """List unconfirmed items at the top. Returns the count so callers can
    decide whether the rest of the report is trustworthy."""
    pending: list[tuple[str, str, int]] = []
    for kind in ("skill", "agent"):
        for name, count in classified[kind]["unconfirmed"].most_common():
            pending.append((kind, name, count))
    if not pending:
        return 0
    print("\nUnresolved — needs triage")
    print("  These look like skills/agents but aren't installed and aren't a")
    print("  known Claude built-in. They distort the usage tables until you")
    print("  resolve them with one of:")
    print("    claude-sentry-confirm <name>   # it's real, count it")
    print("    claude-sentry-deny    <name>   # typo / one-off, ignore it")
    print()
    name_w = max(4, min(40, max(len(n) for _, n, _ in pending)))
    print(f"  {'kind':<6}  {'name':<{name_w}}  {'seen':>6}")
    print("  " + "-" * (8 + name_w + 8))
    for kind, name, count in pending:
        print(f"  {kind:<6}  {name[:name_w]:<{name_w}}  {count:>6}")
    return len(pending)


def _human_report(events_: list[dict], days: int | None) -> None:
    classified = _classify_skill_agent(events_)
    window_label = "all" if days is None else f"{days}d"
    show_window = days is not None

    print(f"claude-sentry report  ·  log: {LOG_FILE}")
    if days is None:
        print("  window: all time")
    else:
        print(f"  window: last {days} days (counts under '{window_label}' below; "
              f"'all' is all-time)")
    if not events_:
        print("\n(no events logged yet — has the hook fired? "
              "Run `claude-sentry-install` and start a Claude session.)")
        return

    _print_unresolved(classified)

    # Build the "real usage" tables: merge verified/native/confirmed buckets
    # for each kind, exclude denied. Sort by all-time count desc, then name.
    for kind, title in (("skill", "Skills"), ("agent", "Agents")):
        merged: Counter = Counter()
        for status in REAL_STATUSES:
            merged.update(classified[kind][status])
        if days is not None:
            window_events = _filter_by_days(events_, days)
            window_counts = aggregate_counts(window_events, kind)
            rows = sorted(
                [(name, window_counts.get(name, {"all": 0})["all"], all_)
                 for name, all_ in merged.items()],
                key=lambda r: (-r[2], r[0].lower()),
            )
        else:
            rows = sorted(
                [(name, 0, all_) for name, all_ in merged.items()],
                key=lambda r: (-r[2], r[0].lower()),
            )
        _print_table(title, rows, window_label=window_label, show_window=show_window)

    # Tools: every event is real (no native/confirm logic on tool names).
    tool_all = aggregate_counts(events_, "tool")
    if days is not None:
        window_events = _filter_by_days(events_, days)
        tool_window = aggregate_counts(window_events, "tool")
        rows = sorted(
            [(name, tool_window.get(name, {"all": 0})["all"], rec["all"])
             for name, rec in tool_all.items()],
            key=lambda r: (-r[2], r[0].lower()),
        )
    else:
        rows = sorted(
            [(name, 0, rec["all"]) for name, rec in tool_all.items()],
            key=lambda r: (-r[2], r[0].lower()),
        )
    _print_table("Tools", rows, window_label=window_label, show_window=show_window)


def _json_report(events_: list[dict], days: int | None) -> None:
    """Machine-readable equivalent. Stable schema so cron-driven cleanups can
    parse it without breaking on cosmetic table changes."""
    classified = _classify_skill_agent(events_)
    window_events = _filter_by_days(events_, days) if days is not None else events_

    def kind_block(kind: str) -> dict:
        real: Counter = Counter()
        for status in REAL_STATUSES:
            real.update(classified[kind][status])
        window_counts = aggregate_counts(window_events, kind)
        return {
            "unresolved": dict(classified[kind]["unconfirmed"]),
            "denied": dict(classified[kind]["denied"]),
            "real": {
                name: {
                    "all": all_,
                    "window": window_counts.get(name, {"all": 0})["all"],
                }
                for name, all_ in real.items()
            },
        }

    tool_all = aggregate_counts(events_, "tool")
    tool_window = (aggregate_counts(window_events, "tool")
                   if days is not None else {})
    out = {
        "log_file": str(LOG_FILE),
        "window_days": days,
        "skills": kind_block("skill"),
        "agents": kind_block("agent"),
        "tools": {
            name: {
                "all": rec["all"],
                "window": tool_window.get(name, {"all": 0})["all"],
            }
            for name, rec in tool_all.items()
        },
    }
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> None:
    p = argparse.ArgumentParser(
        prog="claude-sentry-report",
        description="Print a usage audit of the claude-sentry event log.",
    )
    p.add_argument("--days", type=int, default=30,
                   help="Window for the 'window' column (default: 30). "
                        "Use --all for no window.")
    p.add_argument("--all", action="store_true",
                   help="Show all-time counts only — drop the window column.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of the human table.")
    args = p.parse_args()

    days = None if args.all else args.days
    events_ = load_events()

    if args.json:
        _json_report(events_, days)
    else:
        _human_report(events_, days)
