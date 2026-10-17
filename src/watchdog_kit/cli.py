"""``watchdog-kit`` command line.

Exit codes carry the meaning, so a scheduled job can branch without parsing text:

===== =====================================================================
0     nothing changed since last round (possibly: everything still broken
      and already known -- that is what silence means here)
1     NEW, CHANGED or ESCALATED problems -- someone should look
2     recoveries only
3     scan error (unreadable state, missing database, bad spec)
===== =====================================================================
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .diff import DEFAULT_ESCALATE_AFTER, Item, classify
from .state import StateCorrupt, load, save
from .strike import StrikeLedger

STATE_ROOT = Path.home() / ".local" / "state" / "watchdog-kit"


def _state_path(plugin: str, override: str | None) -> Path:
    """One file per plugin namespace.

    Two snapshots back the ``launchd`` command (observations and strike history),
    so an override must never collapse them onto the same file: whichever saved
    last would silently reset the other to a first run, and a reset baseline means
    a suppressed alert.
    """
    if not override:
        return STATE_ROOT / f"{plugin}.json"
    target = Path(override).expanduser()
    if target.is_dir() or override.endswith("/") or override.endswith(str(Path("/"))):
        return target / f"{plugin}.json"
    return target.with_name(f"{target.stem}-{plugin}{target.suffix or '.json'}")


def _report(diff, snapshot, args, items) -> int:
    save(snapshot, now=args._now)
    if args.json:
        payload = diff.to_dict()
        payload["observed"] = len(items)
        payload["counts"] = {k: sum(1 for i in items if i.state == k) for k in sorted({i.state for i in items})}
        print(json.dumps(payload, ensure_ascii=False))
        return diff.exit_code
    for item in diff.actionable:
        print(f"  {item.state:<16} {item.id}  {item.detail}")
    for item in diff.recovered:
        print(f"  {'RECOVER':<16} {item.id}")
    print(diff.summary() + f"  (observed {len(items)})")
    if args.show_all:
        for item in diff.persisted:
            print(f"  {'PERSIST':<16} {item.id}  {item.detail}")
    elif diff.persisted:
        print(f"  {len(diff.persisted)} unchanged problems suppressed; --show-all lists them")
    return diff.exit_code


def cmd_launchd(args: argparse.Namespace) -> int:
    from .plugins import launchd

    directories = [Path(d).expanduser() for d in args.dir]
    jobs = launchd.discover(directories, exclude_prefixes=tuple(args.exclude))
    snapshot = load(_state_path("launchd", args.state))
    # strike history lives in its own file: the diff layer prunes ids it no
    # longer observes, and a bookkeeping key must not be mistaken for a subject
    strikes_snapshot = load(_state_path("launchd-strikes", args.state))
    strikes = StrikeLedger(
        strikes_snapshot.seen,
        escalate_after=args.escalate_after,
        cooldown=args.cooldown,
        now=lambda: args._now,
    )
    items = launchd.scan(
        jobs,
        snapshot.seen,
        now=args._now,
        slack=args.slack,
        strikes=strikes,
        status_reader=args._status_reader or launchd.read_status,
    )
    diff = classify(items, snapshot, now=args._now, escalate_after=args.escalate_after)
    if diff.scan_empty:
        print(f"error: {diff.summary()}", file=sys.stderr)
        return 3
    code = _report(diff, snapshot, args, items)
    save(strikes_snapshot, now=args._now)
    return code


def cmd_runs(args: argparse.Namespace) -> int:
    from .plugins import sqlruns

    spec = sqlruns.RunsSpec.load(args.spec)
    items = sqlruns.scan(spec, now=args._now, lookback_hours=args.lookback_hours)
    snapshot = load(_state_path("runs", args.state))
    diff = classify(items, snapshot, now=args._now, escalate_after=args.escalate_after)
    if diff.scan_empty:
        print(f"error: {diff.summary()}", file=sys.stderr)
        return 3
    return _report(diff, snapshot, args, items)


def cmd_diff(args: argparse.Namespace) -> int:
    """Generic Unix filter: ``<anything> --json | watchdog-kit diff``."""
    try:
        payload = json.loads(sys.stdin.read() or "[]")
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not JSON: {exc}", file=sys.stderr)
        return 3
    rows = payload.get("items", payload) if isinstance(payload, dict) else payload
    items = []
    for row in rows:
        if not isinstance(row, dict) or "id" not in row or "state" not in row:
            print(f"error: each item needs id and state, got {row!r}", file=sys.stderr)
            return 3
        items.append(Item(id=str(row["id"]), state=str(row["state"]), detail=str(row.get("detail", ""))))
    snapshot = load(_state_path(args.name, args.state))
    diff = classify(items, snapshot, now=args._now, escalate_after=args.escalate_after)
    if diff.scan_empty:
        print(f"error: {diff.summary()}", file=sys.stderr)
        return 3
    return _report(diff, snapshot, args, items)


def _default_dirs() -> list[str]:
    return [str(Path.home() / "Library/LaunchAgents"), "/Library/LaunchDaemons"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watchdog-kit", description="Change-only monitoring.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", help="state file path (default ~/.local/state/watchdog-kit/<plugin>.json)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--escalate-after", type=float, default=DEFAULT_ESCALATE_AFTER,
                        help="seconds a problem may persist before it is raised once (default 7 days)")
    common.add_argument("--now", type=float, help="override the clock (for tests and replays)")
    common.add_argument("--show-all", action="store_true", help="also list PERSIST items")
    sub = parser.add_subparsers(dest="command", required=True)

    launchd = sub.add_parser("launchd", parents=[common], help="are scheduled jobs still running?")
    launchd.add_argument("--dir", action="append", default=[], help="launchd directory (default: user + system)")
    launchd.add_argument("--exclude", action="append", default=[], help="label prefix to skip, repeatable")
    launchd.add_argument("--slack", type=float, default=2.5, help="multiplier on the inferred period")
    launchd.add_argument("--cooldown", type=float, default=1800, help="seconds between strike escalations")

    runs = sub.add_parser("runs", parents=[common], help="do recorded runs match their own claims?")
    runs.add_argument("--spec", required=True, help="JSON spec: db, table, columns, thresholds")
    runs.add_argument("--lookback-hours", type=float, default=48.0, help="how far back to audit")

    generic = sub.add_parser("diff", parents=[common], help="stdin items JSON -> only what changed")
    generic.add_argument("--name", default="stdin", help="state namespace for this feed")

    def finish(sub, func):
        sub.set_defaults(func=func, _now=None, _status_reader=None)

    finish(launchd, cmd_launchd)
    finish(runs, cmd_runs)
    finish(generic, cmd_diff)
    return parser


def main(argv: Sequence[str] | None = None, *, now: float | None = None, status_reader=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `or` would swallow --now 0 (a legitimate epoch clock for replays)
    args._now = now if now is not None else (args.now if args.now is not None else time.time())
    args._status_reader = status_reader
    try:
        return args.func(args)
    except StateCorrupt as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (TypeError, AttributeError, json.JSONDecodeError) as exc:
        # a malformed state file or spec is a scan error; exit 1 would read as
        # "there are new problems", and 2 as "everything recovered"
        print(f"error: unreadable input: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
