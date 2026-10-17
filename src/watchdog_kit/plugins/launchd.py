"""Ask whether scheduled jobs are actually still doing their job.

Three false-positive sources are designed out, each from an observed failure:

1. **First observation is never an alert.** A fresh baseline yields ``BASELINE``,
   because "we have not seen this before" and "it has stopped" are identical
   readings until a second round exists.
2. **Cadence comes from the plist**, not from a copied-out config, and the
   staleness test needs the run counter to have been *still* for at least
   ``slack`` x period. Two quick polls must not condemn every hourly job on the
   machine.
3. **A job that runs and exits non-zero is ``DEGRADED``, not stopped** -- it is
   erroring, which is a different conversation from not running, and it is the
   state most easily misread as "quietly fine because launchd says it ran".

The run counter comes from ``launchctl print``, which is the only place that
reports an actual execution count; ``launchctl list`` shows load state, not life.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..diff import Item
from ..period import Period, infer_period
from ..strike import ACTION_FAIL, ACTION_OK, StrikeLedger

RUNS_RE = re.compile(r"^\s*runs\s*=\s*(\d+)", re.MULTILINE)
EXIT_RE = re.compile(r"^\s*last exit code\s*=\s*(\S+)", re.MULTILINE)
STATE_RE = re.compile(r"^\s*state\s*=\s*(\w+)", re.MULTILINE)


@dataclass
class Job:
    label: str
    path: Path
    period: Period
    stdout_path: Path | None = None
    disabled: bool = False
    error: str | None = None

    @property
    def is_periodic(self) -> bool:
        return self.period.seconds > 0


@dataclass
class Status:
    runs: int | None = None
    code: int | None = None
    state: str | None = None
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.runs is not None


def parse_launchctl_print(text: str) -> Status:
    runs = RUNS_RE.search(text)
    code = EXIT_RE.search(text)
    state = STATE_RE.search(text)
    parsed_code: int | None = None
    if code:
        raw = code.group(1)
        try:
            parsed_code = int(raw)
        except ValueError:
            parsed_code = None  # "exit code was signaled by a post-exec context failure"
    return Status(
        runs=int(runs.group(1)) if runs else None,
        code=parsed_code,
        state=state.group(1) if state else None,
    )


def read_status(label: str, uid: int | None = None) -> Status:
    uid = os.getuid() if uid is None else uid
    for domain in (f"gui/{uid}", "system"):
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return parse_launchctl_print(result.stdout)
    return Status(error="not-loaded")


def _disabled_labels(uid: int) -> set[str]:
    """Disabled job labels, read once.

    ``launchctl list`` omits disabled jobs entirely, so absence from it proves
    nothing; ``print-disabled`` is the only list that names them.
    """
    result = subprocess.run(["launchctl", "print-disabled", f"gui/{uid}"], capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return set(re.findall(r'"([^"]+)"\s*=\s*1', result.stdout))


def discover(
    directories: list[Path],
    *,
    exclude_prefixes: tuple[str, ...] = (),
    uid: int | None = None,
) -> list[Job]:
    uid = os.getuid() if uid is None else uid
    disabled = _disabled_labels(uid)
    jobs: list[Job] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.glob("*.plist")):
            if entry.name.endswith(".disabled") or ".disabled" in entry.suffixes:
                continue
            label = entry.stem
            if label.startswith(exclude_prefixes):
                continue
            try:
                with open(entry, "rb") as handle:
                    plist = plistlib.load(handle)
            except Exception as exc:  # noqa: BLE001 - plistlib raises several unrelated types
                # a plist that cannot be parsed is a finding, not a file to walk
                # past: a job whose schedule is unreadable is a job nobody is
                # watching any more
                jobs.append(
                    Job(
                        label=label,
                        path=entry,
                        period=Period(0, "parse-error", approximate=True),
                        error=f"plist unreadable ({type(exc).__name__})",
                    )
                )
                continue
            stdout = plist.get("StandardOutPath")
            jobs.append(
                Job(
                    label=label,
                    path=entry,
                    period=infer_period(plist),
                    stdout_path=Path(stdout) if stdout else None,
                    disabled=label in disabled,
                )
            )
    return jobs


def scan(
    jobs: list[Job],
    snapshot_seen: dict,
    *,
    now: float | None = None,
    slack: float = 2.5,
    status_reader: Callable[[str], Status] = read_status,
    strikes: StrikeLedger | None = None,
) -> list[Item]:
    """One round of observation. The caller owns snapshot persistence."""
    now = time.time() if now is None else now
    items: list[Item] = []
    for job in jobs:
        entry = snapshot_seen.get(job.label, {})
        if job.error:
            items.append(Item(job.label, "WARN", job.error))
            continue
        if job.disabled:
            items.append(Item(job.label, "SKIP", "disabled"))
            continue
        status = status_reader(job.label)
        if not status.loaded:
            items.append(Item(job.label, "WARN", status.error or "not-loaded"))
            continue

        previous_runs = entry.get("runs")
        advanced = previous_runs is not None and status.runs > int(previous_runs)
        record = {
            "runs": status.runs,
            "code": status.code,
            "seen_at": now,
            "last_advance_at": now if advanced else entry.get("last_advance_at", now),
        }
        snapshot_seen[job.label] = {**entry, **record}

        if previous_runs is None:
            items.append(Item(job.label, "BASELINE", f"first run count {status.runs}"))
            continue

        if advanced:
            decision = strikes.observe(job.label, status.code, runs=status.runs) if strikes else None
            if decision is None or decision.action == ACTION_OK:
                items.append(Item(job.label, "OK", f"runs {previous_runs}->{status.runs}"))
            elif decision.action == ACTION_FAIL:
                items.append(Item(job.label, "DEGRADED", f"ran but exit {status.code}: {decision.reason}"))
            else:
                items.append(
                    Item(job.label, "DEGRADED" if (status.code or 0) != 0 else "OK",
                         f"ran, exit {status.code} ({decision.reason})")
                )
            continue

        if (status.code or 0) != 0:
            decision = strikes.observe(job.label, status.code, runs=status.runs) if strikes else None
            reason = decision.reason if decision else "static history"
            if decision and decision.action == ACTION_FAIL:
                items.append(Item(job.label, "DEGRADED", f"exit {status.code} unresolved: {reason}"))
            else:
                items.append(Item(job.label, "DEGRADED", f"exit {status.code}: {reason}"))
            continue

        if not job.is_periodic:
            items.append(Item(job.label, "WARN", "cadence not inferable from plist"))
            continue

        idle_for = now - float(entry.get("last_advance_at", entry.get("seen_at", now)))
        if idle_for > job.period.with_slack(slack):
            if _logs_are_silent(job):
                items.append(Item(job.label, "QUIET", f"log exists but is empty for {int(idle_for)}s"))
            else:
                items.append(
                    Item(
                        job.label,
                        "STALE",
                        f"run count unchanged for {int(idle_for)}s (period {job.period.seconds}s x{slack})",
                    )
                )
            continue
        items.append(Item(job.label, "OK", f"within period (idle {int(idle_for)}s)"))
    return items


def _logs_are_silent(job: Job) -> bool:
    if not job.stdout_path or not job.stdout_path.exists():
        return False
    try:
        return job.stdout_path.stat().st_size == 0
    except OSError:
        return False
