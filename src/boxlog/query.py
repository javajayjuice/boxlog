"""The query model shared by every backend and the viewer API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from boxlog.context import FIELD_NAME

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
FACET_WINDOW = 2000  # facets (service / source dropdowns) come from this many newest records

# Query-string parameters with a fixed meaning; any other lower_snake_case parameter is an
# equality filter on that top-level field (e.g. ?request_id=abc&tenant=acme).
RESERVED_PARAMS = frozenset({"level", "service", "source", "event", "q", "after", "limit"})


@dataclass(frozen=True)
class LogQuery:
    level: Optional[str] = None  # minimum level
    service: Optional[str] = None
    source: Optional[str] = None
    event: Optional[str] = None  # prefix match: "db" matches "db.query.ok"
    q: Optional[str] = None  # case-insensitive substring over the whole record
    fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    after: Optional[int] = None  # only records with id > after (live-tail cursor)
    limit: int = DEFAULT_LIMIT

    @property
    def min_level(self) -> int:
        return LEVELS.get((self.level or "").upper(), 0)

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> "LogQuery":
        """Build from query-string parameters. Raises ValueError on bad input."""

        def opt(name: str, max_len: int) -> Optional[str]:
            value = (params.get(name) or "").strip()
            if len(value) > max_len:
                raise ValueError(f"{name} is longer than {max_len} characters")
            return value or None

        level = opt("level", 16)
        if level and level.upper() not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")

        after_raw = opt("after", 32)
        limit_raw = opt("limit", 8)
        try:
            after = int(after_raw) if after_raw else None
            limit = int(limit_raw) if limit_raw else DEFAULT_LIMIT
        except ValueError as exc:
            raise ValueError("after and limit must be integers") from exc
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")

        fields = []
        for name, value in params.items():
            if name in RESERVED_PARAMS or not value:
                continue
            if not FIELD_NAME.match(name):
                raise ValueError(f"unknown parameter {name!r}")
            if len(value) > 256:
                raise ValueError(f"{name} is longer than 256 characters")
            fields.append((name, value))

        return cls(
            level=level.upper() if level else None,
            service=opt("service", 128),
            source=opt("source", 128),
            event=opt("event", 128),
            q=opt("q", 256),
            fields=tuple(sorted(fields)),
            after=after,
            limit=limit,
        )


def matches(record: Mapping[str, Any], query: LogQuery) -> bool:
    if query.min_level and LEVELS.get(record.get("level", ""), 0) < query.min_level:
        return False
    if query.service and record.get("service") != query.service:
        return False
    if query.source and record.get("source") != query.source:
        return False
    if query.event and not str(record.get("event") or "").startswith(query.event):
        return False
    for name, value in query.fields:
        if str(record.get(name)) != value:
            return False
    return True


def empty_result() -> dict[str, Any]:
    return {"records": [], "latest": None, "count": 0, "facets": {"services": [], "sources": []}}


def filter_lines(lines: Iterable[str], query: LogQuery) -> dict[str, Any]:
    """Query JSON lines held newest-first (the memory and Redis backends).

    Cheap enough for the few-thousand-record buffers those backends are meant for; the
    SQLite backend does the same filtering in SQL instead.
    """
    needle = query.q.lower() if query.q else None
    records: list[dict[str, Any]] = []
    latest: Optional[int] = None
    services: set[str] = set()
    sources: set[str] = set()
    collecting = True

    for index, line in enumerate(lines):
        if not collecting and index >= FACET_WINDOW:
            break
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue

        record_id = record.get("id")
        if latest is None and isinstance(record_id, int):
            latest = record_id
        if index < FACET_WINDOW:
            if record.get("service"):
                services.add(str(record["service"]))
            if record.get("source"):
                sources.add(str(record["source"]))

        if not collecting:
            continue
        if query.after is not None and isinstance(record_id, int) and record_id <= query.after:
            collecting = False  # newest-first: everything from here on is older
            continue
        if needle and needle not in line.lower():
            continue
        if matches(record, query):
            records.append(record)
            if len(records) >= query.limit:
                collecting = False

    return {
        "records": records,
        "latest": latest,
        "count": len(records),
        "facets": {"services": sorted(services), "sources": sorted(sources)},
    }
