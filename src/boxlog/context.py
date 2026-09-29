"""Correlation fields carried across awaits (and into asyncio tasks) via contextvars.

    with bind(request_id="abc", tenant="acme"):
        ...   # every record logged in here carries request_id and tenant

Fields are free-form, but must be lower_snake_case identifiers and must not collide
with the keys boxlog itself writes on a record.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Iterator, Mapping

# Top-level keys boxlog writes itself - a bound field of the same name would clobber them.
RESERVED_FIELDS = frozenset(
    {
        "id", "ts", "level", "logger", "message", "event", "service",
        "exc_type", "exc_message", "traceback", "extra",
    }
)
FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_context: ContextVar[Mapping[str, Any]] = ContextVar("boxlog_context", default=_EMPTY)


def validate_field_name(name: str) -> None:
    if not FIELD_NAME.match(name):
        raise ValueError(
            f"Invalid log context field {name!r}: use lower_snake_case, max 64 chars"
        )
    if name in RESERVED_FIELDS:
        raise ValueError(f"Log context field {name!r} is reserved by boxlog")


def current() -> dict[str, Any]:
    """A copy of the fields bound in the current context."""
    return dict(_context.get())


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Bind fields for the duration of the block; the previous values come back on exit.
    Binding a field to None removes it for the block."""
    for name in fields:
        validate_field_name(name)
    merged = dict(_context.get())
    for name, value in fields.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    token = _context.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _context.reset(token)
