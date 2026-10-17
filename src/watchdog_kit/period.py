"""Infer how often a launchd job is *supposed* to run, from its own plist.

A staleness judgement needs an expected cadence, and copying a schedule into a
config file is how you get a confidently wrong alert two weeks later when someone
edits the plist. ``launchctl`` does not report a schedule at all, so the plist is
the only machine-readable truth, and ``StartCalendarInterval`` needs interpreting:

===== ========================================= =========================
fields present                                  base period
----- ----------------------------------------- -------------------------
Month                                           1 year
Day (no Month)                                1 month
Weekday (no Day/Month)                        1 week
Hour (no Day/Weekday/Month)                   1 day
Minute only, or nothing further               1 hour
===== ========================================= =========================

An array of calendar entries means "several times per base period", so the
cadence is the base divided by how many distinct times it names. That is an
approximation, deliberately: it is the value that makes the alert fire *later*
than reality rather than earlier, which is the direction you want a false
positive to lean.

``StartInterval`` needs no interpretation, and a job with both is treated as the
shorter of the two, because either trigger satisfies it.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

MINUTE = 60
HOUR = 3600
DAY = 86400
WEEK = 7 * DAY
MONTH = 30 * DAY
YEAR = 365 * DAY


@dataclass
class Period:
    seconds: int
    source: str
    approximate: bool = False

    def with_slack(self, slack: float) -> float:
        return self.seconds * slack


def calendar_period(entries: list[dict]) -> Period:
    if not entries:
        return Period(0, "empty", approximate=True)
    base = _base_period(entries[0])
    unit = _unit_of(entries[0])
    if unit in ("day", "week", "month", "year") and len(entries) > 1:
        distinct = len({tuple(sorted((k, v) for k, v in entry.items())) for entry in entries})
        seconds = max(int(base / max(1, distinct)), HOUR)
        return Period(seconds, "StartCalendarInterval[]", approximate=True)
    return Period(base, "StartCalendarInterval", approximate=True)


def _unit_of(entry: dict) -> str:
    if "Month" in entry:
        return "year"
    if "Day" in entry:
        return "month"
    if "Weekday" in entry:
        return "week"
    if "Hour" in entry:
        return "day"
    return "hour"


def _base_period(entry: dict) -> int:
    return {
        "year": YEAR,
        "month": MONTH,
        "week": WEEK,
        "day": DAY,
        "hour": HOUR,
    }[_unit_of(entry)]


def infer_period(plist: dict) -> Period:
    """Expected cadence for a launchd job, or ``Period(0)`` when unanalysable."""
    candidates: list[Period] = []
    if isinstance(plist.get("StartInterval"), int) and plist["StartInterval"] > 0:
        candidates.append(Period(int(plist["StartInterval"]), "StartInterval"))
    calendar = plist.get("StartCalendarInterval")
    if calendar is not None:
        entries = calendar if isinstance(calendar, list) else [calendar]
        entries = [e for e in entries if isinstance(e, dict)]
        if entries:
            candidates.append(calendar_period(entries))
    if not candidates:
        return Period(0, "none", approximate=True)
    return min(candidates, key=lambda p: p.seconds)


def read_plist(path: str | Path) -> dict:
    with open(path, "rb") as handle:
        return plistlib.load(handle)
