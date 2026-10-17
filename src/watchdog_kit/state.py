"""Atomic, bounded, JSON state files.

Every watcher in this family needs the same three things, and each one used to
re-implement them slightly differently:

* a write that cannot leave a half-written file behind if it is killed mid-run
  (a corrupted state file reads as "first run ever", which silently resets every
  baseline and suppresses the next round of alerts);
* a bound on growth, because ids accumulate forever in a long-lived monitor;
* a schema both the writer and the reader agree on, so a plugin swap does not
  look like 400 regressions.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 1
DEFAULT_CAP = 4000


@dataclass
class Snapshot:
    """Persisted view of one plugin's state.

    ``first_read`` distinguishes "no state file exists" from "the state file is
    empty", which matters because a monitor that cannot tell them apart treats a
    deleted state file as a clean slate and re-announces every old problem as new.
    """
    path: Path
    seen: dict[str, dict] = field(default_factory=dict)
    ts: float | None = None
    version: int = STATE_VERSION
    first_read: bool = True

def load(path: str | Path) -> Snapshot:
    target = Path(path)
    if not target.exists():
        return Snapshot(path=target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # unreadable state must not be mistaken for "nothing has ever been seen";
        # the caller decides, and the CLI reports it as a scan error
        raise StateCorrupt(target)
    if not isinstance(data, dict):
        raise StateCorrupt(target)
    seen = data.get("seen", {})
    if not isinstance(seen, dict):
        raise StateCorrupt(target)
    return Snapshot(
        path=target,
        seen={k: (v if isinstance(v, dict) else {"state": str(v)}) for k, v in seen.items()},
        ts=data.get("ts"),
        version=int(data.get("version", 0)),
        first_read=False,
    )


class StateCorrupt(RuntimeError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"state file is not readable JSON: {path}")
        self.path = path


def save(snapshot: Snapshot, now: float | None = None, cap: int = DEFAULT_CAP) -> None:
    now = time.time() if now is None else now
    if len(snapshot.seen) > cap:
        # keep the most recently touched entries; dropping the oldest is safe
        # because a dropped id reappears as NEW, which is louder than staying
        # silent, and only after the monitor has stopped talking about it
        ordered = sorted(snapshot.seen.items(), key=lambda kv: kv[1].get("seen_at", 0))
        snapshot.seen = dict(ordered[-cap:])
    snapshot.ts = now
    payload = {
        "version": STATE_VERSION,
        "ts": now,
        "seen": snapshot.seen,
    }
    directory = snapshot.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(directory), prefix=f".{snapshot.path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, snapshot.path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    _fsync_dir(directory)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def merge_seen(snapshot: Snapshot, entries: Mapping[str, dict], now: float) -> None:
    """Record the current round's ids, refreshing ``seen_at`` for the cap."""
    for key, value in entries.items():
        existing = snapshot.seen.get(key, {})
        merged = dict(existing)
        merged.update(value)
        merged["seen_at"] = now
        snapshot.seen[key] = merged
