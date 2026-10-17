"""Strike and cooldown: how many observations a failure needs before action.

The trap this avoids is specific to ``launchd``: ``last exit code`` in
``launchctl print`` is *static history*, not an event. Poll it twice and you get
the same non-zero value twice, which looks exactly like "it failed again". A
monitor built on that reading either screams every poll or, worse, restarts a job
that has been idle and healthy for a day.

So a strike only advances when the *evidence changed*:

* a new non-zero code, or
* a run count that increased (a genuine new attempt), or
* the same code having gone unsaid for longer than ``escalate_after``.

``cooldown`` then limits how often the same key may be raised again, which is what
keeps a self-healing action from bouncing a service every few minutes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

ACTION_OK = "ok"
ACTION_WARN = "warn"
ACTION_FAIL = "fail"


@dataclass
class Decision:
    action: str
    reason: str
    streak: int

    @property
    def is_failure(self) -> bool:
        return self.action == ACTION_FAIL


class StrikeLedger:
    """Per-key bookkeeping for "how sure are we that this is new bad news"."""

    def __init__(
        self,
        state: dict,
        *,
        escalate_after: float = 6 * 3600,
        cooldown: float = 30 * 60,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self.escalate_after = escalate_after
        self.cooldown = cooldown
        self.now = now

    def observe(
        self,
        key: str,
        code: int | None,
        *,
        runs: int | None = None,
        signal: bool | None = None,
    ) -> Decision:
        now = self.now()
        entry = self.state.setdefault(key, {})
        prev_code = entry.get("code")
        prev_runs = entry.get("runs")
        streak = int(entry.get("streak", 0))

        if code is None:
            # no reading is neither a pass nor a failure
            return Decision(ACTION_WARN, "no-reading", streak)

        if code == 0 and not signal:
            cleared = streak > 0
            self.state[key] = {"code": 0, "runs": runs, "streak": 0, "first_at": now}
            return Decision(ACTION_OK, "recovered" if cleared else "healthy", 0)

        new_attempt = runs is not None and prev_runs is not None and runs > int(prev_runs)
        code_changed = prev_code is not None and prev_code != code
        first_failure = prev_code is None
        same_unchanged = prev_code == code and not new_attempt
        first_at = float(entry.get("first_at", now))
        # a clock that steps backwards (NTP, a sleep-resume jump) must not produce a
        # negative age that permanently silences escalation
        age = max(0.0, now - first_at)

        entry.setdefault("seen_at", now)
        if same_unchanged:
            # static history: re-reading the same code is not another failure.
            # The only thing that may speak after that is an unresolved age.
            if age >= self.escalate_after and not entry.get("escalated"):
                entry.update(escalated=True, streak=streak + 1, last_at=now)
                return Decision(ACTION_FAIL, f"same-code-{code}-unresolved-{int(age)}s", streak + 1)
            return Decision(ACTION_WARN, "persisting", streak or 1)

        if first_failure:
            entry.update(code=code, runs=runs, streak=1, first_at=now, raised_at=now,
                         escalated=False, seen_at=now)
            return Decision(ACTION_WARN, "first-failure", 1)

        streak += 1
        entry.update(
            code=code,
            runs=runs,
            streak=streak,
            escalated=False,
            first_at=now if code_changed else first_at,
        )
        reason = "code-changed" if code_changed else "new-attempt-failed"
        if now - float(entry.get("raised_at", 0)) < self.cooldown:
            entry.setdefault("raised_at", now)
            return Decision(ACTION_WARN, f"in-cooldown-after-{reason}", streak)
        entry["raised_at"] = now
        return Decision(ACTION_FAIL, reason, streak)
