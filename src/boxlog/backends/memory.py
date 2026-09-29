"""In-process ring buffer. Zero setup, lost on restart - good for tests and scripts."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from boxlog.backends.base import LogEntry
from boxlog.query import LogQuery, filter_lines


class MemoryBackend:
    def __init__(self, max_records: int = 10_000) -> None:
        self._lines: deque[str] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def append(self, entries: list[LogEntry]) -> None:
        with self._lock:
            for entry in entries:
                self._lines.appendleft(entry.line)

    def lines(self) -> list[str]:
        """Newest first."""
        with self._lock:
            return list(self._lines)

    def query(self, query: LogQuery) -> dict[str, Any]:
        return filter_lines(self.lines(), query)

    def close(self) -> None:
        pass
