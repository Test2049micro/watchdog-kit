"""Did a recorded agent run actually produce anything?

The claim under test is the one an agent makes about itself: a row marked
``completed``. Research on this failure mode is consistent that asking the model
does not work -- judge models stay near chance on completion claims -- so this
plugin never consults a model. It reads the client's own bookkeeping and looks for
external signals that contradict the status column:

* ``completed`` but the result column is null/empty;
* ``completed`` but the token/effort counter is implausibly small for a real run;
* ``running`` but untouched for longer than the stale window (a zombie the client
  still counts as in flight).

The database schema is not assumed. Column names, the table, and the thresholds
come from a spec file, so this works against any client that records runs in
SQLite -- ``examples/runs-spec.json`` shows the shape, and
``examples/specs/`` carries one worked example per style.

Opened read-only (``file:...?mode=ro&immutable=1``): a monitor that can write to
the database it audits is a monitor that can break the thing it audits.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..diff import Item

REQUIRED_COLUMNS = ("id", "status", "updated")
DEFAULT_SQLITE_WARM_UP = 10_000


@dataclass
class RunsSpec:
    db: str
    table: str
    columns: dict[str, str]
    thresholds: dict[str, float]
    completed_values: tuple[str, ...] = ("completed", "succeeded", "done")
    running_values: tuple[str, ...] = ("running", "in_progress", "started")
    time_unit: str = "epoch_seconds"

    @classmethod
    def load(cls, path: str | Path) -> "RunsSpec":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        missing = {"db", "table", "columns"} - set(data)
        if missing:
            raise ValueError(f"spec is missing {sorted(missing)}")
        columns = dict(data["columns"])
        absent = set(REQUIRED_COLUMNS) - set(columns)
        if absent:
            raise ValueError(f"spec columns must name {sorted(REQUIRED_COLUMNS)}, missing {sorted(absent)}")
        return cls(
            db=str(data["db"]),
            table=str(data["table"]),
            columns=columns,
            thresholds=dict(data.get("thresholds", {})),
            completed_values=tuple(data.get("completed_values", ("completed", "succeeded", "done"))),
            running_values=tuple(data.get("running_values", ("running", "in_progress", "started"))),
            time_unit=str(data.get("time_unit", "epoch_seconds")),
        )


def connect(db: str) -> sqlite3.Connection:
    resolved = Path(db).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"database not found: {resolved}")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _as_epoch(value: Any, unit: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if unit != "epoch_milliseconds" else float(value) / 1000.0
    text = str(value).strip()
    if text.isdigit():
        return _as_epoch(int(text), unit)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            import calendar
            import datetime

            parsed = datetime.datetime.strptime(text, fmt)
            return float(calendar.timegm(parsed.timetuple()))
        except ValueError:
            continue
    return None


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() in ("{}", "[]", "null", '""')
    if isinstance(value, (list, dict)):
        return not value
    return False


def _quote(identifier: str) -> str:
    """Guard against a spec that names a column with SQL in it.

    Specs are user-editable config, so identifiers are quoted and rejected outright
    if they hold anything but a plain name -- an injection point here would run
    inside the database being audited.
    """
    if not identifier or not all(ch.isalnum() or ch == "_" for ch in identifier):
        raise ValueError(f"unsafe identifier: {identifier!r}")
    return f'"{identifier}"'


def scan(
    spec: RunsSpec,
    *,
    now: float | None = None,
    lookback_hours: float = 48.0,
    connection: sqlite3.Connection | None = None,
) -> list[Item]:
    now = time.time() if now is None else now
    columns = spec.columns
    min_tokens = float(spec.thresholds.get("min_tokens", 500))
    running_stale = float(spec.thresholds.get("running_stale_seconds", 1800))
    window_seconds = lookback_hours * 3600

    updated = _quote(columns["updated"])
    query = (
        f"SELECT {_quote(columns['id'])} AS id, {_quote(columns['status'])} AS status, "
        f"{updated} AS updated_at FROM {_quote(spec.table)} "
        f"WHERE CAST({updated} AS TEXT) IS NOT NULL"
    )
    owns_connection = connection is None
    connection = connection or connect(spec.db)
    if connection.row_factory is None:
        # a caller-supplied connection may not read rows by name
        connection.row_factory = sqlite3.Row
    items: list[Item] = []
    try:
        available = {row[1] for row in connection.execute(f'PRAGMA table_info("{spec.table}")')}
        for name in columns.values():
            if name not in available:
                raise ValueError(f"spec names column {name!r}, not in table {spec.table!r}")
        extra = {}
        if "output" in columns:
            extra["output"] = _quote(columns["output"])
        if "tokens" in columns:
            extra["tokens"] = _quote(columns["tokens"])
        if extra:
            query = query.replace(
                "AS updated_at FROM",
                "AS updated_at, " + ", ".join(f"{q} AS {key}" for key, q in extra.items()) + " FROM",
            )
        for row in connection.execute(query):
            epoch = _as_epoch(row["updated_at"], spec.time_unit)
            if epoch is None or now - epoch > window_seconds:
                continue
            status = str(row["status"] or "")
            if status in spec.completed_values:
                if "output" in extra and _is_empty(row["output"]):
                    items.append(Item(str(row["id"]), "COMPLETED_EMPTY", "completed with no result payload"))
                    continue
                if "tokens" in extra:
                    tokens = row["tokens"]
                    if tokens is not None and float(tokens) < min_tokens:
                        items.append(
                            Item(str(row["id"]), "COMPLETED_THIN", f"completed with {tokens} tokens (< {min_tokens:g})")
                        )
                        continue
            elif status in spec.running_values:
                if now - epoch > running_stale:
                    items.append(
                        Item(str(row["id"]), "RUNNING_STUCK", f"still running {int(now - epoch)}s after last update")
                    )
    finally:
        if owns_connection:
            connection.close()
    return items
