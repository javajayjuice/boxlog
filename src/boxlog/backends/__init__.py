"""Where records are persisted for the viewer.

    MemoryBackend    in-process ring buffer          (tests, scripts)
    SQLiteBackend    a local file, indexed queries    (single machine, no server)
    RedisBackend     a capped Redis list              (several processes / machines)

`backend_from_url()` builds one from a URL, which is handy for configuration:
    memory://                   memory://?max=5000
    sqlite:///logs/app.db       sqlite:///C:/logs/app.db     sqlite:///:memory:
    redis://host:6379/0?key=myapp:logs&max=20000     rediss://...
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from boxlog.backends.base import LogBackend, LogEntry
from boxlog.backends.memory import MemoryBackend
from boxlog.backends.redis import RedisBackend
from boxlog.backends.sqlite import SQLiteBackend

__all__ = [
    "LogBackend",
    "LogEntry",
    "MemoryBackend",
    "RedisBackend",
    "SQLiteBackend",
    "backend_from_url",
]


def backend_from_url(url: str) -> LogBackend:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    params = {k: v[-1] for k, v in parse_qs(parts.query).items()}

    def max_records(default: int) -> int:
        raw = params.get("max")
        if raw is None:
            return default
        value = int(raw)
        if value < 1:
            raise ValueError("max must be positive")
        return value

    if scheme == "memory":
        return MemoryBackend(max_records=max_records(10_000))
    if scheme == "sqlite":
        path = parts.path
        # sqlite:///C:/x.db arrives as "/C:/x.db"; sqlite:///rel.db as "/rel.db".
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        elif path.startswith("//"):
            path = path[1:]  # sqlite:////abs/path.db -> /abs/path.db
        elif path.startswith("/"):
            path = path[1:]  # relative to the working directory
        if not path:
            raise ValueError("sqlite URL needs a path, e.g. sqlite:///logs.db")
        return SQLiteBackend(path, max_records=max_records(100_000))
    if scheme in ("redis", "rediss", "unix"):
        clean = url.split("?", 1)[0]
        return RedisBackend(
            clean, key=params.get("key", "boxlog"), max_records=max_records(10_000)
        )
    raise ValueError(f"Unsupported backend URL scheme {scheme!r} (memory, sqlite, redis)")
