"""One call to wire the root logger:

    import boxlog
    boxlog.setup(service="billing", backend="sqlite:///logs/billing.db")

Safe to call again (e.g. to change level or attach a backend later) - boxlog replaces
only the handlers it installed itself and leaves anyone else's alone.
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from typing import Iterable, Literal, Optional, Union

from boxlog.backends import LogBackend, backend_from_url
from boxlog.formatter import DEFAULT_PROMOTED_FIELDS, ContextFilter, JsonFormatter, TextFormatter
from boxlog.handler import BackendHandler
from boxlog.redaction import PatternLike, RedactionFilter, Redactor

# Chatty libraries: their per-request INFO/DEBUG noise is usually covered by your own
# log_step events. Only their warnings and errors are kept.
DEFAULT_QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "openai", "botocore", "redis", "asyncio")

_STDOUT_NAME = "boxlog-stdout"
_BACKEND_NAME = "boxlog-backend"

_lock = threading.Lock()
_state: dict = {"formatter": None, "redactor": None, "backend_handler": None, "atexit": False}

StdoutMode = Literal["json", "text"]


def _filters(handler: logging.Handler, redactor: Redactor) -> logging.Handler:
    # Filters go on handlers, not the root logger: logger-level filters don't run for
    # records propagated up from child loggers. Context first, so it gets redacted.
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter(redactor))
    return handler


def _remove_named(root: logging.Logger, name: str) -> None:
    for handler in list(root.handlers):
        if handler.get_name() == name:
            root.removeHandler(handler)
            handler.close()


def setup(
    service: Optional[str] = None,
    *,
    level: Union[str, int] = "INFO",
    stdout: Optional[StdoutMode] = "json",
    backend: Union[LogBackend, str, None] = None,
    redact_keys: Iterable[str] = (),
    redact_patterns: Iterable[PatternLike] = (),
    promoted_fields: Iterable[str] = DEFAULT_PROMOTED_FIELDS,
    quiet_loggers: Iterable[str] = DEFAULT_QUIET_LOGGERS,
    capture_uvicorn: bool = True,
) -> None:
    """Configure logging for the whole process.

    service          tagged on every record; lets one viewer hold several apps' logs
    level            root level (name or number)
    stdout           "json" (for log collectors), "text" (for humans), or None
    backend          a LogBackend, a backend URL (see backend_from_url), or None
    redact_keys      extra key names whose values are masked (on top of the defaults)
    redact_patterns  extra regexes whose matches are masked in free text
    promoted_fields  extra= keys lifted to the top level of a record
    quiet_loggers    third-party loggers limited to WARNING
    capture_uvicorn  route uvicorn's server logs through boxlog and drop its access log
                     (use RequestIdMiddleware for request logging instead)
    """
    redactor = Redactor(extra_keys=redact_keys, extra_patterns=redact_patterns)
    formatter = JsonFormatter(service, redactor=redactor, promoted_fields=promoted_fields)
    root = logging.getLogger()

    with _lock:
        _state["formatter"] = formatter
        _state["redactor"] = redactor

        _remove_named(root, _STDOUT_NAME)
        if stdout:
            handler = logging.StreamHandler(sys.stdout)
            handler.set_name(_STDOUT_NAME)
            handler.setFormatter(
                formatter
                if stdout == "json"
                else TextFormatter(service, redactor=redactor, promoted_fields=promoted_fields)
            )
            root.addHandler(_filters(handler, redactor))

        root.setLevel(level)
        for name in quiet_loggers:
            logging.getLogger(name).setLevel(logging.WARNING)

        if capture_uvicorn:
            for name in ("uvicorn", "uvicorn.error"):
                uv = logging.getLogger(name)
                uv.handlers = []
                uv.propagate = True
            logging.getLogger("uvicorn.access").disabled = True

        if not _state["atexit"]:
            atexit.register(shutdown)
            _state["atexit"] = True

        existing = _state["backend_handler"]
        if existing is not None and backend is None:
            # Re-running setup(): keep the attached backend, apply the new settings.
            existing.formatter_json = formatter
            existing.filters = []
            _filters(existing, redactor)

    if backend is not None:
        attach_backend(backend)


def attach_backend(backend: Union[LogBackend, str]) -> BackendHandler:
    """Start shipping records to `backend`, replacing any previously attached backend.
    Useful when the backend's settings are only known after startup (e.g. in an ASGI
    lifespan)."""
    if isinstance(backend, str):
        backend = backend_from_url(backend)
    with _lock:
        formatter = _state["formatter"] or JsonFormatter()
        redactor = _state["redactor"] or Redactor()
        previous = _state["backend_handler"]
        root = logging.getLogger()
        if previous is not None:
            root.removeHandler(previous)
            if previous.backend is not backend:
                previous.close()
        handler = BackendHandler(backend, formatter=formatter)
        handler.set_name(_BACKEND_NAME)
        root.addHandler(_filters(handler, redactor))
        _state["backend_handler"] = handler
    return handler


def detach_backend() -> None:
    """Flush queued records and close the backend. Call it last during shutdown, so
    your shutdown logs still make it to the backend."""
    with _lock:
        handler = _state["backend_handler"]
        _state["backend_handler"] = None
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def set_level(level: Union[str, int]) -> None:
    logging.getLogger().setLevel(level)


def shutdown() -> None:
    detach_backend()
