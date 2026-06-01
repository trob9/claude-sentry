"""claude-sentry-confirm / claude-sentry-deny: headless equivalents of the
TUI's ✓ / ✗ buttons on the Unconfirmed tab.

Writes to the same confirmations.json the TUI uses, so a decision made from
either surface is immediately reflected in the other.

Usage
-----
  claude-sentry-confirm <name>           # confirm a single skill or agent
  claude-sentry-confirm skill::<name>    # disambiguate when the same bare
  claude-sentry-confirm agent::<name>    #   name exists as both kinds
  claude-sentry-confirm --all            # confirm every currently-unresolved item
  claude-sentry-confirm --list           # show what's unresolved, do nothing

`claude-sentry-deny` takes the same arguments but moves the items into the
denied bucket instead. Either command can flip a previous decision: confirming
something that was denied will silently move it across (and vice versa).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from .core import (
    confirm_keys,
    deny_keys,
    load_confirmations,
    load_events,
    status_of,
)


def _unresolved_keys(events_: list[dict]) -> dict[str, int]:
    """Return {"kind::name": count} for every (kind, name) currently in the
    unconfirmed bucket. Count is how many times it appears in the log — used
    by --list to sort 'most likely real' to the top, same as the TUI."""
    conf = load_confirmations()
    seen: Counter = Counter()
    for e in events_:
        kind = e.get("type")
        if kind not in ("skill", "agent"):
            continue
        name = e.get("target") or ""
        if not name:
            continue
        if status_of(kind, name, conf) == "unconfirmed":
            seen[f"{kind}::{name}"] += 1
    return dict(seen)


def _resolve_input(token: str, unresolved: dict[str, int]) -> set[str]:
    """Turn one user-supplied token (e.g. "fast", "skill::fast") into a set of
    "kind::name" keys.

    Without a kind prefix we try to match against unresolved items first. If
    exactly one matches, we use it; if both skill::<name> and agent::<name>
    exist, we refuse to guess and ask the user to disambiguate.
    """
    if "::" in token:
        kind, _, name = token.partition("::")
        if kind not in ("skill", "agent"):
            raise SystemExit(
                f"unknown kind '{kind}' — must be 'skill' or 'agent'"
            )
        return {f"{kind}::{name}"}

    matches = {k for k in unresolved if k.endswith(f"::{token}")}
    if len(matches) == 1:
        return matches
    if len(matches) > 1:
        listed = ", ".join(sorted(matches))
        raise SystemExit(
            f"'{token}' is ambiguous — matches {listed}. "
            f"Re-run with the full kind::name form."
        )
    # Not in the unresolved list — assume skill (TUI behaviour: any future
    # event for this name will pick up the decision regardless).
    return {f"skill::{token}"}


def _parse_args(prog: str, verb: str) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=prog,
        description=f"{verb.capitalize()} skills/agents in the claude-sentry log.",
    )
    p.add_argument("names", nargs="*",
                   help="One or more skill/agent names. Prefix with skill:: or "
                        "agent:: to disambiguate (e.g. skill::verify).")
    p.add_argument("--all", action="store_true",
                   help=f"{verb.capitalize()} every currently-unresolved item.")
    p.add_argument("--list", action="store_true",
                   help="Print the unresolved items and exit without changing anything.")
    return p.parse_args()


def _run(verb: str) -> None:
    prog = f"claude-sentry-{verb}"
    args = _parse_args(prog, verb)
    events_ = load_events()
    unresolved = _unresolved_keys(events_)

    if args.list:
        if not unresolved:
            print("Nothing to triage — the unresolved bucket is empty.")
            return
        print("Unresolved items (most-seen first):")
        width = max(len(k) for k in unresolved)
        for key, count in sorted(unresolved.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {key:<{width}}  seen {count}x")
        return

    if args.all:
        if not unresolved:
            print("Nothing to do — no unresolved items.")
            return
        keys = set(unresolved.keys())
    else:
        if not args.names:
            sys.stderr.write(
                f"{prog}: pass one or more names, or --all, or --list.\n"
            )
            sys.exit(2)
        keys = set()
        for token in args.names:
            keys |= _resolve_input(token, unresolved)

    if verb == "confirm":
        confirm_keys(keys)
        action = "Confirmed"
    else:
        deny_keys(keys)
        action = "Denied"
    for key in sorted(keys):
        print(f"  {action}: {key}")


def confirm_main() -> None:
    _run("confirm")


def deny_main() -> None:
    _run("deny")
