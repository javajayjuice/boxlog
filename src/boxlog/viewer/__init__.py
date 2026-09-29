"""The web log viewer: a dependency-free ASGI app.

    viewer = create_viewer(backend, token=os.environ["LOGS_TOKEN"])

    app.mount("/logs", viewer)            # FastAPI / Starlette
    # or run it on its own:  boxlog serve sqlite:///logs.db

Routes (relative to wherever it is mounted):
    /                  the page shell - holds no data
    /static/logs.js    /static/logs.css
    /api/logs          records as JSON - requires `Authorization: Bearer <token>`

Log content is attacker-controlled (request bodies, emails, chat messages end up in
logs), so the page renders it with textContent only, under a CSP that forbids inline
script.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
from importlib import resources
from typing import Any, Callable, Iterable, Optional, Union
from urllib.parse import parse_qsl

from boxlog.backends.base import LogBackend
from boxlog.query import LogQuery

logger = logging.getLogger("boxlog.viewer")

MIN_TOKEN_LENGTH = 32

SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        b"img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
]

_STATIC = {
    "/static/logs.js": ("logs.js", b"text/javascript; charset=utf-8"),
    "/static/logs.css": ("logs.css", b"text/css; charset=utf-8"),
}

BackendSource = Union[LogBackend, Callable[[], Optional[LogBackend]]]
TokenSource = Union[str, Callable[[], Optional[str]], None]


def _read_static(name: str) -> bytes:
    return resources.files("boxlog.viewer").joinpath("static", name).read_bytes()


def check_token(token: Optional[str], min_length: int = MIN_TOKEN_LENGTH) -> Optional[str]:
    """Returns the token if it is usable, None if it is empty or too short (logged)."""
    token = (token or "").strip()
    if not token:
        return None
    if len(token) < min_length:
        logger.error(
            "Log viewer token is shorter than %s characters - the viewer stays disabled. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"',
            min_length,
            extra={"event": "viewer.token_rejected"},
        )
        return None
    return token


class LogViewer:
    def __init__(
        self,
        backend: BackendSource,
        *,
        token: TokenSource,
        title: str = "Logs",
        trace_fields: Iterable[str] = ("request_id", "job_id"),
        min_token_length: int = MIN_TOKEN_LENGTH,
    ) -> None:
        if isinstance(token, str) and check_token(token, min_token_length) is None:
            raise ValueError(
                f"The viewer token must be at least {min_token_length} characters"
            )
        self._backend = backend
        self._token = token
        self._min_token_length = min_token_length
        self._title = title
        self._trace_fields = list(trace_fields)
        self._index: Optional[bytes] = None

    # -- resolution (backend and token may be provided lazily) ---------------

    def _resolve_backend(self) -> Optional[LogBackend]:
        if callable(self._backend) and not hasattr(self._backend, "query"):
            return self._backend()
        return self._backend  # type: ignore[return-value]

    def _resolve_token(self) -> Optional[str]:
        token = self._token() if callable(self._token) else self._token
        return check_token(token, self._min_token_length) if callable(self._token) else token

    def _page(self) -> bytes:
        if self._index is None:
            template = _read_static("index.html").decode("utf-8")
            self._index = (
                template.replace("{{title}}", html.escape(self._title))
                .replace("{{trace_fields}}", html.escape(json.dumps(self._trace_fields), quote=True))
                .encode("utf-8")
            )
        return self._index

    # -- ASGI -------------------------------------------------------------------

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        root_path = scope.get("root_path", "") or ""
        path = scope.get("path", "") or ""
        # Newer Starlette passes the full path plus root_path; older versions strip it.
        rel = path[len(root_path):] if root_path and path.startswith(root_path) else path

        if scope.get("method") not in ("GET", "HEAD"):
            await _respond(send, 405, b"Method Not Allowed", b"text/plain", [(b"allow", b"GET")])
            return

        token = self._resolve_token()
        if token is None:
            await _respond(send, 404, b"Not Found", b"text/plain")
            return

        if rel == "":
            # Relative asset URLs need the trailing slash.
            await _respond(send, 307, b"", b"text/plain", [(b"location", (root_path + "/").encode("latin-1") or b"/")])
            return
        if rel == "/":
            await _respond(send, 200, self._page(), b"text/html; charset=utf-8")
            return
        if rel in _STATIC:
            name, content_type = _STATIC[rel]
            await _respond(send, 200, _read_static(name), content_type)
            return
        if rel == "/api/logs":
            await self._api(scope, send, token)
            return
        await _respond(send, 404, b"Not Found", b"text/plain")

    async def _api(self, scope: dict[str, Any], send: Any, token: str) -> None:
        supplied = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                supplied = value.decode("latin-1")
                break
        scheme, _, credential = supplied.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credential.strip().encode(), token.encode()
        ):
            client = scope.get("client")
            logger.warning(
                "Rejected log viewer API request with a missing or wrong token",
                extra={"event": "viewer.auth_failed", "client_ip": client[0] if client else None},
            )
            await _json(send, 401, {"detail": "Invalid or missing token"}, [(b"www-authenticate", b"Bearer")])
            return

        backend = self._resolve_backend()
        if backend is None:
            await _json(send, 503, {"detail": "Log storage is not available"})
            return

        params = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        try:
            query = LogQuery.from_params(params)
        except ValueError as exc:
            await _json(send, 400, {"detail": str(exc)})
            return

        try:
            result = await asyncio.to_thread(backend.query, query)
        except Exception:  # noqa: BLE001
            logger.exception("Log viewer query failed", extra={"event": "viewer.query_failed"})
            await _json(send, 502, {"detail": "Log storage query failed"})
            return
        await _json(send, 200, result)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _respond(
    send: Any,
    status: int,
    body: bytes,
    content_type: bytes,
    extra_headers: Optional[list] = None,
) -> None:
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
    headers += SECURITY_HEADERS + (extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send: Any, status: int, payload: Any, extra_headers: Optional[list] = None) -> None:
    body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
    await _respond(send, status, body, b"application/json", extra_headers)


def create_viewer(
    backend: BackendSource,
    *,
    token: TokenSource,
    title: str = "Logs",
    trace_fields: Iterable[str] = ("request_id", "job_id"),
    min_token_length: int = MIN_TOKEN_LENGTH,
) -> LogViewer:
    """Build the viewer ASGI app.

    backend  a LogBackend, or a zero-argument callable returning one (or None -> 503) -
             handy when the backend is only created at startup
    token    the bearer token (at least `min_token_length` chars), or a callable
             returning it; if it resolves to None the viewer answers 404 everywhere
    """
    return LogViewer(
        backend,
        token=token,
        title=title,
        trace_fields=trace_fields,
        min_token_length=min_token_length,
    )
