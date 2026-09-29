"""Request-id middleware for ASGI (FastAPI, Starlette, Django-ASGI, ...) and WSGI (Flask,
Django, ...) apps.

Each request gets a `request_id` bound for its duration - taken from an incoming
`X-Request-ID` header if it is well-formed, otherwise generated - echoed back on the
response, and one `http.request` record (method, path, status, duration). Unhandled
exceptions are logged with their traceback before being re-raised.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Iterable, Optional

from boxlog.context import bind
from boxlog.timing import already_logged, mark_logged

logger = logging.getLogger("boxlog.http")

DEFAULT_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def _choose_request_id(incoming: Optional[str]) -> str:
    """Honour an upstream id so a trace can span services - but only if it is safe to
    put in log records and response headers."""
    if incoming:
        candidate = incoming.strip()
        if _VALID_REQUEST_ID.match(candidate):
            return candidate
    return uuid.uuid4().hex


def _is_quiet(path: str, quiet: tuple[str, ...]) -> bool:
    """Prefix match, except "/" itself: since every path starts with "/", treating it
    as a prefix would silence the whole app's access log rather than just the root
    path. So "/" in `quiet` only matches the root path exactly."""
    for pattern in quiet:
        if pattern == "/":
            if path == "/":
                return True
        elif path.startswith(pattern):
            return True
    return False


def _level_for(status: int, path: str, quiet: tuple[str, ...]) -> int:
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    if quiet and _is_quiet(path, quiet):
        return logging.DEBUG
    return logging.INFO


def _log_request(method: str, path: str, status: int, start: float, quiet: tuple[str, ...], client: Any) -> None:
    logger.log(
        _level_for(status, path, quiet),
        "%s %s -> %s",
        method,
        path,
        status,
        extra={
            "event": "http.request",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "client_ip": client,
        },
    )


def _log_unhandled(method: str, path: str, exc: BaseException) -> None:
    logger.error(
        "Unhandled error on %s %s",
        method,
        path,
        exc_info=None if already_logged(exc) else exc,
        extra={"event": "http.unhandled_error", "method": method, "path": path},
    )
    mark_logged(exc)


class RequestIdMiddleware:
    """ASGI middleware.

        app.add_middleware(RequestIdMiddleware, quiet_paths=("/health", "/logs", "/"))

    `quiet_paths`: successful requests under these prefixes are logged at DEBUG
    (health probes, the log viewer's own polling), failures still at WARNING/ERROR.
    A bare "/" matches only the root path itself, not every path (see `_is_quiet`).
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        header: str = DEFAULT_HEADER,
        quiet_paths: Iterable[str] = ("/health",),
    ) -> None:
        self.app = app
        self._header = header.lower().encode("latin-1")
        self._quiet = tuple(quiet_paths)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == self._header:
                incoming = value.decode("latin-1")
                break
        request_id = _choose_request_id(incoming)
        method = scope.get("method", "")
        path = scope.get("path", "")
        status = {"code": 500}

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((self._header, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        start = time.perf_counter()
        client = scope.get("client")
        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception as exc:
                _log_unhandled(method, path, exc)
                raise
            finally:
                _log_request(method, path, status["code"], start, self._quiet, client[0] if client else None)


class WSGIRequestIdMiddleware:
    """WSGI middleware.

        app.wsgi_app = WSGIRequestIdMiddleware(app.wsgi_app)          # Flask
        application = WSGIRequestIdMiddleware(get_wsgi_application())  # Django

    The id is bound while the app is called. A streaming response body that's
    iterated after the app returns is outside that scope, so records logged while
    streaming won't carry it. `quiet_paths` behaves as in `RequestIdMiddleware`.
    """

    def __init__(
        self,
        app: Callable[..., Iterable[bytes]],
        *,
        header: str = DEFAULT_HEADER,
        quiet_paths: Iterable[str] = ("/health",),
    ) -> None:
        self.app = app
        self._header = header
        self._environ_key = "HTTP_" + header.upper().replace("-", "_")
        self._quiet = tuple(quiet_paths)

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        request_id = _choose_request_id(environ.get(self._environ_key))
        environ["boxlog.request_id"] = request_id
        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        status = {"code": 500}

        def start_with_request_id(status_line: str, headers: list, exc_info: Any = None) -> Any:
            try:
                status["code"] = int(status_line.split(" ", 1)[0])
            except ValueError:
                pass
            headers = list(headers) + [(self._header, request_id)]
            return start_response(status_line, headers, exc_info)

        start = time.perf_counter()
        with bind(request_id=request_id):
            try:
                return self.app(environ, start_with_request_id)
            except Exception as exc:
                _log_unhandled(method, path, exc)
                raise
            finally:
                _log_request(method, path, status["code"], start, self._quiet, environ.get("REMOTE_ADDR"))
