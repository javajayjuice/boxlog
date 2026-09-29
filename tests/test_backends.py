"""Every backend must answer the same queries the same way."""

from __future__ import annotations

import json
import logging

import pytest

from boxlog.backends import (
    LogEntry,
    MemoryBackend,
    RedisBackend,
    SQLiteBackend,
    backend_from_url,
)
from boxlog.handler import BackendHandler
from boxlog.query import LogQuery, filter_lines

RECORDS = [
    {"id": 1, "level": "INFO", "service": "billing", "source": "email", "event": "email.job.ok", "message": "done", "job_id": "case-1"},
    {"id": 2, "level": "ERROR", "service": "billing", "source": "whatsapp", "event": "azure.chat.failed", "message": "Upstream TIMEOUT", "request_id": "r-1"},
    {"id": 3, "level": "WARNING", "service": "api", "source": "whatsapp", "event": "http.payload_invalid", "message": "missing query", "request_id": "r-2", "tenant": "acme"},
    {"id": 4, "level": "DEBUG", "service": "api", "source": "copilot", "event": "azure.embed.start", "message": "start 100%_done", "request_id": "r-1"},
]


def entries(records=RECORDS):
    return [LogEntry(r, json.dumps(r)) for r in records]


@pytest.fixture(params=["memory", "sqlite", "redis"])
def backend(request, tmp_path, fake_redis):
    if request.param == "memory":
        b = MemoryBackend()
    elif request.param == "sqlite":
        b = SQLiteBackend(tmp_path / "logs.db")
    else:
        b = RedisBackend(client=fake_redis, key="t:logs")
    b.append(entries())
    yield b
    b.close()


def ids(result):
    return [r["id"] for r in result["records"]]


def messages(result):
    return [r["message"] for r in result["records"]]


# The SQLite backend renumbers records with its own row ids (still newest-first, still
# monotonic), so assertions compare messages rather than ids across backends.
def test_newest_first(backend):
    assert messages(backend.query(LogQuery())) == ["start 100%_done", "missing query", "Upstream TIMEOUT", "done"]


def test_level_floor(backend):
    assert messages(backend.query(LogQuery(level="WARNING"))) == ["missing query", "Upstream TIMEOUT"]
    assert messages(backend.query(LogQuery(level="ERROR"))) == ["Upstream TIMEOUT"]


def test_service_source_and_event_prefix(backend):
    assert messages(backend.query(LogQuery(service="api"))) == ["start 100%_done", "missing query"]
    assert messages(backend.query(LogQuery(source="whatsapp"))) == ["missing query", "Upstream TIMEOUT"]
    assert messages(backend.query(LogQuery(event="azure"))) == ["start 100%_done", "Upstream TIMEOUT"]


def test_field_filters_including_non_indexed_fields(backend):
    assert messages(backend.query(LogQuery(fields=(("request_id", "r-1"),)))) == ["start 100%_done", "Upstream TIMEOUT"]
    assert messages(backend.query(LogQuery(fields=(("job_id", "case-1"),)))) == ["done"]
    assert messages(backend.query(LogQuery(fields=(("tenant", "acme"),)))) == ["missing query"]


def test_text_search_is_case_insensitive(backend):
    assert messages(backend.query(LogQuery(q="timeout"))) == ["Upstream TIMEOUT"]


def test_text_search_treats_like_wildcards_literally(backend):
    assert messages(backend.query(LogQuery(q="100%_"))) == ["start 100%_done"]
    assert messages(backend.query(LogQuery(q="%"))) == ["start 100%_done"]


def test_limit(backend):
    assert len(backend.query(LogQuery(limit=2))["records"]) == 2


def test_live_tail_cursor(backend):
    everything = backend.query(LogQuery())
    latest = everything["latest"]
    assert latest == everything["records"][0]["id"]
    assert backend.query(LogQuery(after=latest))["records"] == []

    backend.append(entries([{"id": 99, "level": "INFO", "message": "new one"}]))
    newer = backend.query(LogQuery(after=latest))
    assert messages(newer) == ["new one"]
    assert newer["latest"] > latest


def test_facets(backend):
    facets = backend.query(LogQuery(level="ERROR"))["facets"]
    assert facets["services"] == ["api", "billing"]  # facets ignore filters
    assert facets["sources"] == ["copilot", "email", "whatsapp"]


def test_capping(tmp_path, fake_redis):
    for b in (MemoryBackend(max_records=3), RedisBackend(client=fake_redis, key="cap", max_records=3)):
        b.append(entries())
        assert len(b.query(LogQuery())["records"]) == 3


def test_sqlite_prunes_to_max(tmp_path):
    b = SQLiteBackend(tmp_path / "p.db", max_records=100)
    rows = [{"id": i, "level": "INFO", "message": f"m{i}"} for i in range(700)]
    b.append(entries(rows))
    count = b._conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    assert count <= 100
    assert messages(b.query(LogQuery(limit=1))) == ["m699"]


def test_sqlite_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    b = SQLiteBackend(path)
    b.append(entries())
    b.close()
    assert len(SQLiteBackend(path).query(LogQuery())["records"]) == 4


def test_filter_lines_skips_garbage():
    lines = ["not json", "[1, 2]", *(json.dumps(r) for r in reversed(RECORDS))]
    assert ids(filter_lines(lines, LogQuery())) == [4, 3, 2, 1]


# -- the handler in front of a backend ------------------------------------------


def _ship(backend, messages, logger_name="tests.ship"):
    handler = BackendHandler(backend, flush_interval=0.01)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        for m in messages:
            logger.info(m)
    finally:
        logger.removeHandler(handler)
        handler.close()
    return handler


def test_handler_ships_with_increasing_ids():
    backend = MemoryBackend()
    _ship(backend, ["one", "two", "three"])
    result = backend.query(LogQuery())
    assert messages(result) == ["three", "two", "one"]
    assert ids(result) == sorted(ids(result), reverse=True)


def test_handler_ignores_backend_client_loggers():
    backend = MemoryBackend()
    _ship(backend, ["internal"], logger_name="redis.connection")
    assert backend.query(LogQuery())["records"] == []


class Broken:
    def append(self, entries):
        raise ConnectionError("down")

    def query(self, query):
        return {}

    def close(self):
        pass


def test_failing_backend_never_raises(capsys):
    handler = _ship(Broken(), ["a", "b"])
    assert handler.dropped == 2
    assert "unavailable" in capsys.readouterr().err


# -- backend_from_url -------------------------------------------------------------


def test_backend_from_url(tmp_path, monkeypatch):
    assert isinstance(backend_from_url("memory://?max=5"), MemoryBackend)
    monkeypatch.chdir(tmp_path)
    sqlite = backend_from_url("sqlite:///sub/app.db?max=500")
    assert isinstance(sqlite, SQLiteBackend)
    assert sqlite.path.replace("\\", "/") == "sub/app.db"
    sqlite.close()
    with pytest.raises(ValueError):
        backend_from_url("postgres://x")
    with pytest.raises(ValueError):
        backend_from_url("sqlite://")


def test_backend_from_url_windows_absolute_path(tmp_path):
    target = (tmp_path / "abs.db").as_posix()
    b = backend_from_url(f"sqlite:///{target}")
    assert b.path == target
    b.close()


def test_backend_from_url_redis_key_and_max():
    b = backend_from_url("redis://localhost:6379/0?key=api:logs&max=50")
    assert (b._key, b._max) == ("api:logs", 50)
