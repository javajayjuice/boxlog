"""setup(), attach/detach, and the CLI."""

from __future__ import annotations

import json
import logging
import time

import pytest

import boxlog
from boxlog import MemoryBackend
from boxlog.cli import main
from boxlog.query import LogQuery


def _wait_for(backend, count, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = backend.query(LogQuery(limit=1000))["records"]
        if len(records) >= count:
            return records
        time.sleep(0.02)
    return backend.query(LogQuery(limit=1000))["records"]


def test_setup_json_stdout_with_service(isolated_root, capsys):
    boxlog.setup(service="svc", level="INFO")
    logging.getLogger("t.setup").info("hello", extra={"event": "t.hello"})
    line = [l for l in capsys.readouterr().out.splitlines() if "hello" in l][-1]
    record = json.loads(line)
    assert record["service"] == "svc"
    assert record["event"] == "t.hello"


def test_setup_text_stdout(isolated_root, capsys):
    boxlog.setup(stdout="text")
    logging.getLogger("t.setup").warning("careful")
    out = capsys.readouterr().out
    assert "WARNING" in out and "careful" in out and not out.lstrip().startswith("{")


def test_setup_is_idempotent(isolated_root):
    boxlog.setup()
    boxlog.setup()
    names = [h.get_name() for h in isolated_root.handlers]
    assert names.count("boxlog-stdout") == 1


def test_setup_ships_to_a_backend_and_redacts(isolated_root):
    backend = MemoryBackend()
    boxlog.setup(service="svc", stdout=None, backend=backend)
    with boxlog.bind(request_id="r-1"):
        logging.getLogger("t.ship").info("token=abc123 used", extra={"password": "p"})
    boxlog.shutdown()
    record = backend.query(LogQuery())["records"][0]
    assert record["request_id"] == "r-1"
    assert record["service"] == "svc"
    assert "abc123" not in record["message"]
    assert record["extra"]["password"] == "[redacted]"


def test_attach_backend_later_and_replace(isolated_root):
    boxlog.setup(stdout=None)
    first, second = MemoryBackend(), MemoryBackend()
    boxlog.attach_backend(first)
    logging.getLogger("t.attach").info("to first")
    assert _wait_for(first, 1)
    boxlog.attach_backend(second)
    logging.getLogger("t.attach").info("to second")
    boxlog.detach_backend()
    assert [r["message"] for r in first.query(LogQuery())["records"]] == ["to first"]
    assert [r["message"] for r in second.query(LogQuery())["records"]] == ["to second"]


def test_resetup_keeps_the_backend_and_applies_new_settings(isolated_root):
    backend = MemoryBackend()
    boxlog.setup(service="one", stdout=None, backend=backend)
    boxlog.setup(service="two", stdout=None)
    logging.getLogger("t.resetup").info("after")
    boxlog.shutdown()
    assert backend.query(LogQuery())["records"][0]["service"] == "two"


def test_attach_backend_accepts_a_url(isolated_root):
    boxlog.setup(stdout=None)
    handler = boxlog.attach_backend("memory://?max=10")
    assert isinstance(handler.backend, MemoryBackend)


def test_quiet_loggers(isolated_root):
    boxlog.setup(quiet_loggers=("noisy.lib",))
    assert logging.getLogger("noisy.lib").level == logging.WARNING


def test_uvicorn_capture(isolated_root):
    boxlog.setup()
    assert logging.getLogger("uvicorn.error").propagate is True
    assert logging.getLogger("uvicorn.access").disabled is True


# -- CLI --------------------------------------------------------------------------


def test_cli_token(capsys):
    assert main(["token"]) == 0
    assert len(capsys.readouterr().out.strip()) >= 43


def test_cli_serve_requires_a_long_token(monkeypatch, capsys):
    monkeypatch.setenv("BOXLOG_TOKEN", "short")
    assert main(["serve", "memory://"]) == 2
    assert "at least 32" in capsys.readouterr().err


def test_cli_serve_rejects_bad_backend(monkeypatch, capsys):
    monkeypatch.setenv("BOXLOG_TOKEN", "t" * 43)
    assert main(["serve", "postgres://x"]) == 2
    assert "Invalid backend" in capsys.readouterr().err


def test_cli_serve_runs_uvicorn(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setenv("BOXLOG_TOKEN", "t" * 43)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert main(["serve", "memory://", "--port", "9999"]) == 0
    assert seen["port"] == 9999 and seen["host"] == "127.0.0.1"
    assert type(seen["app"]).__name__ == "LogViewer"


def test_cli_requires_a_command():
    with pytest.raises(SystemExit):
        main([])
