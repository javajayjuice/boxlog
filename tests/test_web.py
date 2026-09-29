"""Middleware (ASGI + WSGI) and the viewer."""

from __future__ import annotations

import json
import logging
from wsgiref.util import setup_testing_defaults

import httpx
import pytest

from boxlog import MemoryBackend, RequestIdMiddleware, WSGIRequestIdMiddleware, create_viewer
from boxlog.backends import LogEntry
from boxlog.context import current

TOKEN = "t" * 43


# -- ASGI middleware ------------------------------------------------------------


async def app(scope, receive, send):
    if scope["path"] == "/boom":
        raise RuntimeError("route exploded")
    logging.getLogger("tests.route").info("inside route")
    body = json.dumps({"request_id": current().get("request_id")}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def client(asgi):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False), base_url="http://test")


async def test_asgi_generates_and_echoes_request_id(capture):
    async with client(RequestIdMiddleware(app)) as c:
        response = await c.get("/ping")
    rid = response.headers["x-request-id"]
    assert len(rid) == 32
    assert response.json()["request_id"] == rid
    assert capture.find(message="inside route")["request_id"] == rid
    access = capture.find(event="http.request")
    assert access["request_id"] == rid
    assert access["extra"]["status"] == 200


async def test_asgi_honours_valid_and_replaces_malformed_ids():
    async with client(RequestIdMiddleware(app)) as c:
        ok = await c.get("/ping", headers={"X-Request-ID": "upstream-1.2_3"})
        bad = await c.get("/ping", headers={"X-Request-ID": "has space"})
    assert ok.headers["x-request-id"] == "upstream-1.2_3"
    assert bad.headers["x-request-id"] != "has space"


async def test_asgi_logs_unhandled_errors(capture):
    async with client(RequestIdMiddleware(app)) as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert capture.find(event="http.unhandled_error")["exc_type"] == "RuntimeError"
    assert capture.find(event="http.request")["level"] == "ERROR"


async def test_asgi_quiet_paths_log_at_debug(capture):
    async with client(RequestIdMiddleware(app, quiet_paths=("/health",))) as c:
        await c.get("/health")
    assert capture.find(event="http.request")["level"] == "DEBUG"


async def test_root_in_quiet_paths_matches_only_the_root_path(capture):
    """"/" is a prefix of every path - a naive startswith() would silence the whole
    app's access log, not just the root probe."""
    async with client(RequestIdMiddleware(app, quiet_paths=("/",))) as c:
        await c.get("/")
        await c.get("/api/whatsapp")
    records = [r for r in capture.records if r["event"] == "http.request"]
    assert records[0]["level"] == "DEBUG"
    assert records[1]["level"] == "INFO"


# -- WSGI middleware ------------------------------------------------------------


def wsgi_app(environ, start_response):
    logging.getLogger("tests.wsgi").info("in wsgi")
    if environ["PATH_INFO"] == "/boom":
        raise RuntimeError("wsgi exploded")
    start_response("201 Created", [("Content-Type", "text/plain")])
    return [current().get("request_id", "").encode()]


def call_wsgi(middleware, path="/", headers=None):
    environ = {}
    setup_testing_defaults(environ)
    environ["PATH_INFO"] = path
    environ.update(headers or {})
    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    body = b"".join(middleware(environ, start_response))
    return captured, body


def test_wsgi_binds_and_echoes_request_id(capture):
    captured, body = call_wsgi(WSGIRequestIdMiddleware(wsgi_app), headers={"HTTP_X_REQUEST_ID": "abc-1"})
    assert captured["headers"]["X-Request-ID"] == "abc-1"
    assert body == b"abc-1"
    assert capture.find(message="in wsgi")["request_id"] == "abc-1"
    assert capture.find(event="http.request")["extra"]["status"] == 201


def test_wsgi_logs_unhandled_errors(capture):
    with pytest.raises(RuntimeError):
        call_wsgi(WSGIRequestIdMiddleware(wsgi_app), path="/boom")
    assert capture.find(event="http.unhandled_error")["exc_type"] == "RuntimeError"


# -- viewer ------------------------------------------------------------------------


def seeded_backend():
    backend = MemoryBackend()
    backend.append(
        [
            LogEntry({}, json.dumps({"id": 1, "level": "INFO", "message": "ok", "service": "a"})),
            LogEntry({}, json.dumps({"id": 2, "level": "ERROR", "message": "broke", "service": "b", "request_id": "r-9"})),
        ]
    )
    return backend


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_viewer_refuses_short_static_tokens():
    with pytest.raises(ValueError):
        create_viewer(MemoryBackend(), token="short")


async def test_viewer_is_404_everywhere_without_a_token():
    viewer = create_viewer(MemoryBackend(), token=lambda: None)
    async with client(viewer) as c:
        for path in ("/", "/static/logs.js", "/api/logs"):
            assert (await c.get(path, headers=auth())).status_code == 404


async def test_viewer_lazy_short_token_disables_it(capture):
    viewer = create_viewer(MemoryBackend(), token=lambda: "short")
    async with client(viewer) as c:
        assert (await c.get("/")).status_code == 404
    assert "viewer.token_rejected" in capture.events()


async def test_viewer_page_and_assets_have_strict_headers():
    viewer = create_viewer(seeded_backend(), token=TOKEN, title="Billing <logs>")
    async with client(viewer) as c:
        page = await c.get("/")
        js = await c.get("/static/logs.js")
        css = await c.get("/static/logs.css")
    assert page.status_code == js.status_code == css.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "Billing &lt;logs&gt;" in page.text  # title is escaped
    assert "<script>" not in page.text.replace('<script src="static/logs.js"></script>', "")


async def test_viewer_js_never_writes_html():
    async with client(create_viewer(MemoryBackend(), token=TOKEN)) as c:
        js = (await c.get("/static/logs.js")).text
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js


async def test_viewer_static_files_are_an_allow_list():
    async with client(create_viewer(MemoryBackend(), token=TOKEN)) as c:
        assert (await c.get("/static/index.html")).status_code == 404
        assert (await c.get("/static/../__init__.py")).status_code == 404


async def test_viewer_api_requires_the_token(capture):
    async with client(create_viewer(seeded_backend(), token=TOKEN)) as c:
        assert (await c.get("/api/logs")).status_code == 401
        assert (await c.get("/api/logs", headers=auth("x" * 43))).status_code == 401
        assert (await c.get("/api/logs", headers={"Authorization": TOKEN})).status_code == 401
    assert "viewer.auth_failed" in capture.events()


async def test_viewer_api_returns_filtered_records():
    async with client(create_viewer(seeded_backend(), token=TOKEN)) as c:
        everything = (await c.get("/api/logs", headers=auth())).json()
        errors = (await c.get("/api/logs?level=ERROR", headers=auth())).json()
        traced = (await c.get("/api/logs?request_id=r-9", headers=auth())).json()
    assert [r["id"] for r in everything["records"]] == [2, 1]
    assert everything["facets"]["services"] == ["a", "b"]
    assert [r["message"] for r in errors["records"]] == ["broke"]
    assert [r["id"] for r in traced["records"]] == [2]


@pytest.mark.parametrize("query", ["level=LOUD", "limit=0", "limit=abc", "Bad-Param=1", "after=x"])
async def test_viewer_api_rejects_bad_queries(query):
    async with client(create_viewer(seeded_backend(), token=TOKEN)) as c:
        assert (await c.get(f"/api/logs?{query}", headers=auth())).status_code == 400


async def test_viewer_api_503_when_backend_missing():
    async with client(create_viewer(lambda: None, token=TOKEN)) as c:
        assert (await c.get("/api/logs", headers=auth())).status_code == 503


async def test_viewer_only_allows_get():
    async with client(create_viewer(MemoryBackend(), token=TOKEN)) as c:
        assert (await c.post("/api/logs", headers=auth())).status_code == 405


async def test_viewer_redirects_to_trailing_slash_when_mounted():
    viewer = create_viewer(MemoryBackend(), token=TOKEN)
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/logs", "root_path": "/logs", "headers": [], "query_string": b""}
    await viewer(scope, None, send)
    assert sent[0]["status"] == 307
    assert (b"location", b"/logs/") in sent[0]["headers"]
