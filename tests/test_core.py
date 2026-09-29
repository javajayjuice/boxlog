"""Context, redaction, formatting, log_step."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from boxlog import bind, current, log_step
from boxlog.formatter import JsonFormatter, TextFormatter
from boxlog.redaction import REDACTED, Redactor

log = logging.getLogger("tests.core")


# -- context ----------------------------------------------------------------


def test_bind_sets_and_restores():
    assert current() == {}
    with bind(request_id="r-1", tenant="acme"):
        assert current() == {"request_id": "r-1", "tenant": "acme"}
        with bind(job_id="j-1", tenant=None):
            assert current() == {"request_id": "r-1", "job_id": "j-1"}
        assert current() == {"request_id": "r-1", "tenant": "acme"}
    assert current() == {}


@pytest.mark.parametrize("name", ["RequestId", "has space", "1abc", "message", "level", "extra"])
def test_bind_rejects_bad_or_reserved_names(name):
    with pytest.raises(ValueError):
        with bind(**{name: "x"}):
            pass


async def test_context_survives_awaits_and_tasks(capture):
    async def child():
        await asyncio.sleep(0)
        log.info("from child task")

    with bind(request_id="r-async"):
        await asyncio.create_task(child())
    assert capture.find(message="from child task")["request_id"] == "r-async"


def test_bound_fields_are_top_level(capture):
    with bind(request_id="r-2", tenant="acme"):
        log.info("hello", extra={"event": "t.hello", "n": 1})
    record = capture.records[-1]
    assert (record["request_id"], record["tenant"], record["event"]) == ("r-2", "acme", "t.hello")
    assert record["extra"] == {"n": 1}


def test_explicit_extra_overrides_bound_field(capture):
    with bind(source="email"):
        log.info("x", extra={"source": "scraping"})
    assert capture.records[-1]["source"] == "scraping"


def test_promoted_extras_are_lifted_even_when_unbound(capture):
    log.info("x", extra={"request_id": "one-off"})
    assert capture.records[-1]["request_id"] == "one-off"
    assert "extra" not in capture.records[-1]


# -- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,secret",
    [
        ("Authorization: Bearer abc.DEF-123_xyz", "abc.DEF-123_xyz"),
        ("header Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ('{"accessToken": "tok-value", "ok": true}', "tok-value"),
        ("login password=hunter2&next=/", "hunter2"),
        ("api_key: sk-live-123", "sk-live-123"),
        ("redis://default:s3cretpw@cache.example:6380/0", "s3cretpw"),
    ],
)
def test_secrets_in_text_are_redacted(text, secret):
    out = Redactor().text(text)
    assert secret not in out
    assert REDACTED in out


def test_non_secret_text_is_untouched():
    text = "total_tokens=42 queue_key=rag:q user=alice"
    assert Redactor().text(text) == text


def test_sensitive_keys_are_masked_but_counts_are_not(capture):
    log.info(
        "x",
        extra={
            "password": "hunter2",
            "access_token": "t-1",
            "client_secret": "cs",
            "headers": {"Authorization": "Bearer zzz", "Accept": "json"},
            "total_tokens": 42,
            "max_tokens": 800,
        },
    )
    extra = capture.records[-1]["extra"]
    assert extra["password"] == extra["access_token"] == extra["client_secret"] == REDACTED
    assert extra["headers"] == {"Authorization": REDACTED, "Accept": "json"}
    assert (extra["total_tokens"], extra["max_tokens"]) == (42, 800)


def test_bound_context_is_redacted(capture):
    with bind(session_token="abc"):
        log.info("x")
    assert capture.records[-1]["session_token"] == REDACTED


def test_exception_text_and_traceback_are_redacted(capture):
    try:
        raise RuntimeError("login failed for password=hunter2")
    except RuntimeError:
        log.exception("boom")
    record = capture.records[-1]
    assert "hunter2" not in record["exc_message"]
    assert "hunter2" not in record["traceback"]


def test_custom_keys_and_patterns():
    r = Redactor(extra_keys=["national_id"], extra_patterns=[r"\b\d{13}\b"])
    assert r.value("national_id", "8001015009087") == REDACTED
    assert "8001015009087" not in r.text("id 8001015009087 on file")


def test_a_bad_format_string_is_still_logged(capture):
    log.info("%s and %s", "only-one")
    assert capture.records[-1]["message"].startswith("%s and %s")


# -- formatters ---------------------------------------------------------------


def test_json_formatter_shape():
    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "hi %s", ("there",), None)
    record.event = "x.y"
    data = json.loads(JsonFormatter("svc").format(record))
    assert data["level"] == "WARNING"
    assert data["logger"] == "app.x"
    assert data["message"] == "hi there"
    assert data["service"] == "svc"
    assert data["event"] == "x.y"
    assert data["ts"].endswith("+00:00")


def test_json_formatter_handles_unserializable_extras():
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "m", None, None)
    record.thing = object()
    assert "object object" in JsonFormatter().format(record)


def test_text_formatter_is_one_readable_line():
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "done", None, None)
    record.event = "job.ok"
    record.rows = 3
    line = TextFormatter().format(record)
    assert "INFO" in line and "done" in line and "event=job.ok" in line and "rows=3" in line
    assert "\n" not in line


def test_uvicorn_color_message_is_dropped(capture):
    log.info("Started", extra={"color_message": "\x1b[36mStarted\x1b[0m"})
    assert "extra" not in capture.records[-1]


# -- log_step -----------------------------------------------------------------


def test_log_step_ok(capture):
    with log_step(log, "demo.step", thing="a") as step:
        step["result"] = 3
    ok = capture.find(event="demo.step.ok")
    assert ok["extra"]["thing"] == "a"
    assert ok["extra"]["result"] == 3
    assert ok["extra"]["duration_ms"] >= 0
    assert "demo.step.start" in capture.events()


def test_log_step_failure_reraises(capture):
    with pytest.raises(ValueError):
        with log_step(log, "demo.fail"):
            raise ValueError("bad input")
    failed = capture.find(event="demo.fail.failed")
    assert failed["level"] == "ERROR"
    assert failed["exc_type"] == "ValueError"
    assert failed["extra"]["error_type"] == "ValueError"


def test_nested_steps_log_the_traceback_once(capture):
    with pytest.raises(RuntimeError):
        with log_step(log, "outer"):
            with log_step(log, "inner"):
                raise RuntimeError("deep")
    failed = [r for r in capture.records if str(r["event"]).endswith(".failed")]
    assert [r["event"] for r in failed] == ["inner.failed", "outer.failed"]
    assert "traceback" in failed[0]
    assert "traceback" not in failed[1]


async def test_log_step_cancellation_is_not_a_failure(capture):
    async def slow():
        with log_step(log, "demo.slow"):
            await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "demo.slow.cancelled" in capture.events()
    assert "demo.slow.failed" not in capture.events()


def test_log_step_tolerates_reserved_field_names(capture):
    with log_step(log, "demo.reserved", name="x", message="y"):
        pass
    assert capture.find(event="demo.reserved.ok")["extra"]["field_name"] == "x"
