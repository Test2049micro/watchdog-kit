"""The change layer: report what moved, stay silent about what merely persists.

Why this exists: a monitor that re-announces the same three broken jobs every
hour stops being read within a week. That is not an alerting-quality problem, it
is an information-content one -- the number was already known, so the message
carries nothing. Humans react to *change*, so the output should be made of
change:

* ``NEW``       -- healthy last round, broken this round: the thing to act on
* ``CHANGED``   -- still broken, but differently (a new failure mode)
* ``RECOVER``   -- broken last round, healthy now: also worth saying, because it
  confirms an intervention worked
* ``PERSIST``   -- unchanged failure: silent by default
* ``ESCALATE``  -- unchanged failure older than ``escalate_after``: allowed to
  make noise exactly once, so a long-standing problem cannot fade out of view by
  being consistently ignored

``PERSIST`` items are still counted in the payload, so a dashboard can chart
them; they simply do not trigger the exit code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .state import Snapshot

OK_STATES = frozenset({"OK", "SKIP", "BASELINE"})
DEFAULT_ESCALATE_AFTER = 7 * 24 * 3600.0


@dataclass
class Item:
    """One observed subject: a job, a run, a seat."""

    id: str
    state: str
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        return self.state not in OK_STATES


@dataclass
class Diff:
    new: list[Item] = field(default_factory=list)
    changed: list[Item] = field(default_factory=list)
    recovered: list[Item] = field(default_factory=list)
    persisted: list[Item] = field(default_factory=list)
    escalated: list[Item] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> list[Item]:
        return self.new + self.changed + self.escalated

    @property
    def loud(self) -> bool:
        return bool(self.actionable)

    @property
    def exit_code(self) -> int:
        if self.actionable:
            return 1
        return 2 if self.recovered else 0

    def summary(self) -> str:
        return (
            f"NEW {len(self.new)} CHANGED {len(self.changed)} RECOVER {len(self.recovered)} "
            f"PERSIST {len(self.persisted)} ESCALATE {len(self.escalated)}"
        )

    def to_dict(self) -> dict:
        return {
            "new": [self._row(i) for i in self.new],
            "changed": [self._row(i) for i in self.changed],
            "recover": [self._row(i) for i in self.recovered],
            "persist": [self._row(i) for i in self.persisted],
            "escalate": [self._row(i) for i in self.escalated],
            "unattributed": self.noise,
            "summary": self.summary(),
            "exit_code": self.exit_code,
        }

    @staticmethod
    def _row(item: Item) -> dict:
        return {"id": item.id, "state": item.state, "detail": item.detail}


def classify(
    items: list[Item],
    snapshot: Snapshot,
    *,
    now: float | None = None,
    escalate_after: float = DEFAULT_ESCALATE_AFTER,
) -> Diff:
    """Compare this round's items with the previous snapshot and update it.

    Entries are merged, never replaced: a plugin may have stored counters in the
    same record (a launchd run count, say), and losing them between rounds would
    turn every second poll into a false "first sighting".
    """
    now = time.time() if now is None else now
    diff = Diff()
    current = {item.id: item for item in items}

    def record(item: Item, previous: dict | None, *, first_seen: float, escalated: bool = False) -> None:
        merged = dict(previous or {})
        merged.update({"state": item.state, "detail": item.detail,
                       "first_seen": first_seen, "escalated": escalated, "seen_at": now})
        snapshot.seen[item.id] = merged

    for item in current.values():
        previous = snapshot.seen.get(item.id)
        before = previous.get("state") if previous else None
        was_problem = bool(before) and before not in OK_STATES

        if not item.is_problem:
            if was_problem:
                diff.recovered.append(item)
            record(item, previous, first_seen=now)
            continue

        if previous is None or not was_problem:
            diff.new.append(item)
            record(item, previous, first_seen=now)
            continue

        if before != item.state:
            diff.changed.append(item)
            # a changed failure mode restarts the clock: same id, different claim
            record(item, previous, first_seen=now)
            continue

        diff.persisted.append(item)
        first_seen = float(previous.get("first_seen", now))
        if now - first_seen >= escalate_after and not previous.get("escalated"):
            diff.escalated.append(item)
            record(item, previous, first_seen=first_seen, escalated=True)
        else:
            record(item, previous, first_seen=first_seen, escalated=bool(previous.get("escalated")))

    for orphan in sorted(set(snapshot.seen) - set(current)):
        entry = snapshot.seen[orphan]
        if entry.get("state") and entry["state"] not in OK_STATES:
            diff.recovered.append(Item(id=orphan, state="OK", detail="no longer observed"))
        del snapshot.seen[orphan]
    return diff
