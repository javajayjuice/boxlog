"""The backend protocol.

Backends are synchronous. append() is only ever called from the handler's shipper
thread, and the viewer runs query() in a worker thread, so a backend never blocks an
event loop.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol, runtime_checkable

from boxlog.query import LogQuery


class LogEntry(NamedTuple):
    record: dict[str, Any]  # the structured record (for backends that index columns)
    line: str  # the same record as a JSON line (for backends that store text)


@runtime_checkable
class LogBackend(Protocol):
    def append(self, entries: list[LogEntry]) -> None: ...

    def query(self, query: LogQuery) -> dict[str, Any]:
        """Return {"records": [...newest first], "latest": id|None, "count": n,
        "facets": {"services": [...], "sources": [...]}}."""
        ...

    def close(self) -> None: ...
