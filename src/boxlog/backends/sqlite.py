"""A single SQLite file: no server, survives restarts, filters in SQL (indexed), so it
comfortably holds far more history than the list-based backends. stdlib only.

Several processes on one machine can share the file (WAL mode). Don't put it on a
network share.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Union

from boxlog.backends.base import LogEntry
from boxlog.query import FACET_WINDOW, LEVELS, LogQuery

DEFAULT_MAX_RECORDS = 100_000
_PRUNE_EVERY = 500  # inserts between prunes

# Promoted to real (indexed) columns; any other field is matched with json_extract().
_COLUMNS = ("service", "source", "event", "request_id", "job_id")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    level_no   INTEGER,
    service    TEXT,
    source     TEXT,
    event      TEXT,
    request_id TEXT,
    job_id     TEXT,
    body       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_level      ON logs (level_no);
CREATE INDEX IF NOT EXISTS logs_request_id ON logs (request_id);
CREATE INDEX IF NOT EXISTS logs_job_id     ON logs (job_id);
CREATE INDEX IF NOT EXISTS logs_event      ON logs (event);
CREATE INDEX IF NOT EXISTS logs_service    ON logs (service);
"""


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _like_contains(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


class SQLiteBackend:
    def __init__(
        self, path: Union[str, Path], *, max_records: int = DEFAULT_MAX_RECORDS
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._max = max_records
        self._lock = threading.Lock()
        self._inserts = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)

    def append(self, entries: list[LogEntry]) -> None:
        if not entries:
            return
        rows = [
            (
                entry.record.get("ts"),
                LEVELS.get(str(entry.record.get("level")), 0),
                *(
                    None if entry.record.get(col) is None else str(entry.record.get(col))
                    for col in _COLUMNS
                ),
                entry.line,
            )
            for entry in entries
        ]
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "INSERT INTO logs (ts, level_no, service, source, event, request_id, job_id, body)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._inserts += len(rows)
                if self._inserts >= _PRUNE_EVERY:
                    self._inserts = 0
                    self._conn.execute(
                        "DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?",
                        (self._max,),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, query: LogQuery) -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []
        if query.min_level:
            where.append("level_no >= ?")
            args.append(query.min_level)
        for column, value in (("service", query.service), ("source", query.source)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if query.event:
            where.append("event LIKE ? ESCAPE '\\'")
            args.append(_like_prefix(query.event))
        if query.q:
            where.append("body LIKE ? ESCAPE '\\'")  # LIKE is case-insensitive for ASCII
            args.append(_like_contains(query.q))
        for name, value in query.fields:  # names are validated identifiers (LogQuery)
            if name in _COLUMNS:
                where.append(f"{name} = ?")
            else:
                where.append(f"CAST(json_extract(body, '$.{name}') AS TEXT) = ?")
            args.append(value)
        if query.after is not None:
            where.append("id > ?")
            args.append(query.after)

        sql = "SELECT id, body FROM logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(query.limit)

        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            latest = self._conn.execute("SELECT MAX(id) FROM logs").fetchone()[0]
            services = self._distinct("service")
            sources = self._distinct("source")

        records = []
        for row_id, body in rows:
            try:
                record = json.loads(body)
            except ValueError:
                continue
            record["id"] = row_id  # the table's own cursor, monotonic across processes
            records.append(record)
        return {
            "records": records,
            "latest": latest,
            "count": len(records),
            "facets": {"services": services, "sources": sources},
        }

    def _distinct(self, column: str) -> list[str]:
        """Distinct non-null values of a fixed column among the newest records."""
        assert column in _COLUMNS  # never interpolate anything else into SQL
        rows = self._conn.execute(
            f"SELECT DISTINCT c FROM (SELECT {column} AS c FROM logs ORDER BY id DESC LIMIT ?)"
            " WHERE c IS NOT NULL",
            (FACET_WINDOW,),
        ).fetchall()
        return sorted(r[0] for r in rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
