"""Ships log records to a backend without ever blocking the caller.

emit() only turns the record into a dict and puts it on an in-process queue. A daemon
thread drains the queue in batches and writes them to the backend. If the backend is
down, records are dropped and a single warning goes to stderr: logging must never raise
into, slow down, or recurse through the code that is logging.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from typing import Iterable, Optional

from boxlog.backends.base import LogBackend, LogEntry
from boxlog.formatter import JsonFormatter

# A backend's own client library must not feed records back into that backend.
DEFAULT_IGNORED_LOGGERS = ("redis", "boxlog.backends")

_STOP = object()


class _MonotonicIds:
    """Strictly increasing integer ids, nanosecond-based so they keep increasing across
    restarts. The viewer uses them as its live-tail cursor."""

    def __init__(self) -> None:
        self._last = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._last = max(self._last + 1, time.time_ns())
            return self._last


class BackendHandler(logging.Handler):
    def __init__(
        self,
        backend: LogBackend,
        *,
        formatter: Optional[JsonFormatter] = None,
        ignored_loggers: Iterable[str] = DEFAULT_IGNORED_LOGGERS,
        batch_size: int = 200,
        flush_interval: float = 0.5,
        max_queue: int = 50_000,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.formatter_json = formatter or JsonFormatter()
        self._ignored = tuple(ignored_loggers)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._ids = _MonotonicIds()
        self._warned = False
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="boxlog-shipper", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        """Records lost because the queue was full or the backend failed."""
        return self._dropped

    # -- producer side (any thread, including an event loop) ----------------

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed or (self._ignored and record.name.startswith(self._ignored)):
            return
        try:
            data = self.formatter_json.to_dict(record)
            data["id"] = self._ids.next()
            line = json.dumps(data, default=str, ensure_ascii=False)
            self._queue.put_nowait(LogEntry(data, line))
        except queue.Full:
            self._dropped += 1
        except Exception:  # noqa: BLE001
            self.handleError(record)

    # -- consumer side (the shipper thread) ---------------------------------

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                continue
            if first is _STOP:
                return
            batch = [first]
            stop_after = False
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is _STOP:
                    stop_after = True
                    break
                batch.append(item)
            self._write(batch)
            if stop_after:
                return

    def _write(self, batch: list[LogEntry]) -> None:
        try:
            self.backend.append(batch)
            self._warned = False
        except Exception as exc:  # noqa: BLE001
            self._dropped += len(batch)
            if not self._warned:
                self._warned = True
                print(
                    f"[boxlog] log backend {type(self.backend).__name__} unavailable, "
                    f"dropping records: {exc!r}",
                    file=sys.stderr,
                )

    def close(self, timeout: float = 5.0) -> None:
        """Flush what's queued, stop the shipper thread and close the backend."""
        if not self._closed:
            self._closed = True
            if self._thread.is_alive():
                try:
                    self._queue.put(_STOP, timeout=1)
                except queue.Full:
                    pass
                self._thread.join(timeout)
            try:
                self.backend.close()
            except Exception:  # noqa: BLE001
                pass
        super().close()
