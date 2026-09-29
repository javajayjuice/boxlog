"""Helpers for telling a LogRecord's own attributes apart from `extra=` fields."""

from __future__ import annotations

import logging
from typing import Any

STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}

# Attributes boxlog's own filters attach to a record.
CONTEXT_ATTR = "boxlog_context"
EXC_ATTR = "boxlog_exc"
INTERNAL_ATTRS = frozenset({CONTEXT_ATTR, EXC_ATTR})

# Extras other libraries attach that carry no information (uvicorn adds an ANSI-coloured
# copy of every message).
IGNORED_EXTRA_KEYS = frozenset({"color_message"})


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in STANDARD_RECORD_ATTRS
        and key not in INTERNAL_ATTRS
        and key not in IGNORED_EXTRA_KEYS
        and not key.startswith("_")
    }


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """`extra=` keys that collide with LogRecord attributes make logging raise KeyError -
    prefix them instead of crashing the caller."""
    return {
        (f"field_{key}" if key in STANDARD_RECORD_ATTRS else key): value
        for key, value in fields.items()
    }
