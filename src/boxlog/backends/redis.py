"""A capped Redis list: LPUSH + LTRIM, newest first. Shared across processes and
machines, survives restarts. Needs `pip install boxfusion-log[redis]`."""

from __future__ import annotations

from typing import Any, Optional

from boxlog.backends.base import LogEntry
from boxlog.query import LogQuery, filter_lines

DEFAULT_KEY = "boxlog"
DEFAULT_MAX_RECORDS = 10_000


class RedisBackend:
    def __init__(
        self,
        url: Optional[str] = None,
        *,
        client: Any = None,
        key: str = DEFAULT_KEY,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        if client is None:
            if url is None:
                raise ValueError("RedisBackend needs either url or client")
            try:
                from redis import Redis
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError(
                    "RedisBackend needs the redis package: pip install 'boxfusion-log[redis]'"
                ) from exc
            client = Redis.from_url(
                url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5
            )
        self._redis = client
        self._key = key
        self._max = max_records

    def append(self, entries: list[LogEntry]) -> None:
        if not entries:
            return
        pipe = self._redis.pipeline(transaction=False)
        pipe.lpush(self._key, *(entry.line for entry in entries))
        pipe.ltrim(self._key, 0, self._max - 1)
        pipe.execute()

    def query(self, query: LogQuery) -> dict[str, Any]:
        lines = self._redis.lrange(self._key, 0, self._max - 1)
        return filter_lines(
            (line.decode("utf-8", "replace") if isinstance(line, bytes) else line for line in lines),
            query,
        )

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:  # noqa: BLE001
            pass
