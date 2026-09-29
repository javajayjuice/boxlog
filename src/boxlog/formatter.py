"""Record -> dict -> JSON line (or a readable text line for local development)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from boxlog._record import CONTEXT_ATTR, EXC_ATTR, extra_fields
from boxlog.context import current
from boxlog.redaction import Redactor

# `extra=` keys lifted to the top level of a record even when they weren't bound, so a
# one-off `logger.info(..., extra={"request_id": rid})` is still traceable.
DEFAULT_PROMOTED_FIELDS = ("request_id", "job_id", "source")


class ContextFilter(logging.Filter):
    """Snapshots the bound context onto the record, in the thread that logged it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, CONTEXT_ATTR):
            setattr(record, CONTEXT_ATTR, current())
        return True


class JsonFormatter(logging.Formatter):
    def __init__(
        self,
        service: Optional[str] = None,
        *,
        redactor: Optional[Redactor] = None,
        promoted_fields: Iterable[str] = DEFAULT_PROMOTED_FIELDS,
    ) -> None:
        super().__init__()
        self.service = service
        self.redactor = redactor or Redactor()
        self.promoted_fields = frozenset(promoted_fields)

    def to_dict(self, record: logging.LogRecord) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "event": getattr(record, "event", None),
        }
        if self.service:
            data["service"] = self.service

        context = getattr(record, CONTEXT_ATTR, None)
        if context is None:
            context = current()
        data.update(context)

        extras = extra_fields(record)
        extras.pop("event", None)
        for key in list(extras):
            if key in context or key in self.promoted_fields:
                data[key] = extras.pop(key)

        if record.exc_info and record.exc_info[0] is not None:
            exc = getattr(record, EXC_ATTR, None) or self.redactor.exception(record.exc_info)
            data.update(exc)

        if extras:
            data["extra"] = extras
        return data

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.to_dict(record), default=str, ensure_ascii=False)


class TextFormatter(JsonFormatter):
    """Human-readable single line for local development:

        12:01:02.345 INFO  app.db  query ok (3.1 ms)  event=db.query.ok request_id=ab12 rows=4
    """

    def format(self, record: logging.LogRecord) -> str:
        data = self.to_dict(record)
        ts = data.pop("ts")[11:23]
        level = data.pop("level")
        logger = data.pop("logger")
        message = data.pop("message")
        tb = data.pop("traceback", None)
        data.pop("exc_message", None)
        data.pop("service", None)
        extras = data.pop("extra", {}) or {}
        fields = {k: v for k, v in {**data, **extras}.items() if v is not None}
        tail = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{ts} {level:<7} {logger}  {message}" + (f"  {tail}" if tail else "")
        return line + ("\n" + tb.rstrip() if tb else "")
