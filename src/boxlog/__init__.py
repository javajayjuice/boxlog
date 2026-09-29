"""boxlog - structured JSON logging with request tracing, secret redaction, pluggable
storage and a built-in web log viewer.

    import logging, boxlog

    boxlog.setup(service="billing", backend="sqlite:///logs/billing.db")
    log = logging.getLogger(__name__)

    with boxlog.bind(request_id="abc"):
        with boxlog.log_step(log, "invoice.create", customer="c-1") as step:
            step["invoice_id"] = create_invoice()

It builds on the standard `logging` module: existing `logging.getLogger(...)` calls,
and third-party libraries' logs, all flow through it unchanged.
"""

from boxlog.backends import (
    LogBackend,
    MemoryBackend,
    RedisBackend,
    SQLiteBackend,
    backend_from_url,
)
from boxlog.configure import (
    DEFAULT_QUIET_LOGGERS,
    attach_backend,
    detach_backend,
    set_level,
    setup,
    shutdown,
)
from boxlog.context import bind, current
from boxlog.formatter import JsonFormatter, TextFormatter
from boxlog.handler import BackendHandler
from boxlog.middleware import RequestIdMiddleware, WSGIRequestIdMiddleware
from boxlog.query import LogQuery
from boxlog.redaction import Redactor
from boxlog.timing import already_logged, log_step, mark_logged
from boxlog.viewer import create_viewer

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_QUIET_LOGGERS",
    "BackendHandler",
    "JsonFormatter",
    "LogBackend",
    "LogQuery",
    "MemoryBackend",
    "Redactor",
    "RedisBackend",
    "RequestIdMiddleware",
    "SQLiteBackend",
    "TextFormatter",
    "WSGIRequestIdMiddleware",
    "__version__",
    "already_logged",
    "attach_backend",
    "backend_from_url",
    "bind",
    "create_viewer",
    "current",
    "detach_backend",
    "log_step",
    "mark_logged",
    "set_level",
    "setup",
    "shutdown",
]
