from __future__ import annotations

import logging

import pytest

import boxlog
from boxlog._record import CONTEXT_ATTR
from boxlog.formatter import ContextFilter, JsonFormatter
from boxlog.redaction import RedactionFilter


class Capture(logging.Handler):
    """Records exactly as a backend would receive them: filters applied, then to_dict."""

    def __init__(self, formatter: JsonFormatter | None = None) -> None:
        super().__init__()
        self.addFilter(ContextFilter())
        self.addFilter(RedactionFilter())
        self.json = formatter or JsonFormatter()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.json.to_dict(record))

    def events(self) -> list:
        return [r.get("event") for r in self.records]

    def find(self, **match) -> dict:
        for record in self.records:
            if all(record.get(k) == v for k, v in match.items()):
                return record
        raise AssertionError(f"no record matching {match}; events={self.events()}")


@pytest.fixture
def capture():
    handler = Capture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous)


@pytest.fixture
def isolated_root():
    """setup() rewires the root logger - restore it (and boxlog's state) afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    boxlog.shutdown()
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


class FakeRedis:
    """Just enough of redis-py for RedisBackend: pipeline/lpush/ltrim/lrange/close."""

    def __init__(self) -> None:
        self.lists: dict[str, list] = {}
        self.closed = False

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)

    def lpush(self, key, *values):
        lst = self.lists.setdefault(key, [])
        for value in values:
            lst.insert(0, value)
        return len(lst)

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]

    def lrange(self, key, start, end):
        return list(self.lists.get(key, [])[start : end + 1])

    def close(self):
        self.closed = True


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list = []

    def lpush(self, key, *values):
        self._ops.append(("lpush", key, values))
        return self

    def ltrim(self, key, start, end):
        self._ops.append(("ltrim", key, (start, end)))
        return self

    def execute(self):
        for op, key, args in self._ops:
            getattr(self._redis, op)(key, *args)
        self._ops.clear()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


__all__ = ["CONTEXT_ATTR"]
