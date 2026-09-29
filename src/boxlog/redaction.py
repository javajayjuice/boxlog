"""Secret redaction, applied to every record before any handler formats it.

Masks, by default:
  * values of sensitive-looking keys in `extra=` and bound context (password, token,
    api_key, secret, authorization, cookie, ...), matched as a whole name or `_` suffix -
    so `access_token` is masked but `total_tokens` is not;
  * `Bearer ...` and `Basic ...` credentials inside free text;
  * `key=value` / `"key": "value"` pairs for sensitive keys inside free text;
  * passwords embedded in URLs (`redis://user:secret@host`).

Redaction is a backstop, not a licence to log secrets.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, Iterable, Pattern, Union

from boxlog._record import CONTEXT_ATTR, EXC_ATTR, extra_fields

REDACTED = "[redacted]"

DEFAULT_SENSITIVE_KEYS = (
    "authorization", "password", "passwd", "pwd", "secret", "token",
    "access_token", "accesstoken", "refresh_token", "id_token", "api_key", "apikey",
    "access_key", "private_key", "client_secret", "bearer", "cookie", "set_cookie",
)

_TEXT_KEYS = r"password|passwd|pwd|secret|token|access_?token|refresh_?token|api_?key|client_?secret"

_DEFAULT_TEXT_PATTERNS: tuple[tuple[Pattern[str], str], ...] = (
    (re.compile(r"(\b(?:Bearer|Basic)\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1" + REDACTED),
    (
        re.compile(
            r"""(["']?\b(?:""" + _TEXT_KEYS + r""")["']?\s*[:=]\s*["']?)[^"',\s&}]+""",
            re.IGNORECASE,
        ),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(://[^:/@\s]*:)[^@/\s]+(@)"), r"\1" + REDACTED + r"\2"),
)

PatternLike = Union[str, Pattern[str]]
_MAX_DEPTH = 6


class Redactor:
    def __init__(
        self,
        extra_keys: Iterable[str] = (),
        extra_patterns: Iterable[PatternLike] = (),
    ) -> None:
        names = [*DEFAULT_SENSITIVE_KEYS, *(k.lower() for k in extra_keys)]
        alternatives = "|".join(
            re.escape(n).replace("_", "_?") for n in sorted(set(names), key=len, reverse=True)
        )
        self._key_pattern = re.compile(rf"(^|[_\-.])(?:{alternatives})$", re.IGNORECASE)
        self._text_patterns = list(_DEFAULT_TEXT_PATTERNS) + [
            (re.compile(p) if isinstance(p, str) else p, REDACTED) for p in extra_patterns
        ]

    def is_sensitive_key(self, key: str) -> bool:
        return bool(self._key_pattern.search(key))

    def text(self, value: str) -> str:
        for pattern, replacement in self._text_patterns:
            value = pattern.sub(replacement, value)
        return value

    def value(self, key: str, value: Any, _depth: int = 0) -> Any:
        if self.is_sensitive_key(key):
            return REDACTED
        if _depth >= _MAX_DEPTH:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(str(k), v, _depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(key, v, _depth + 1) for v in value]
        return value

    def exception(self, exc_info: Any) -> dict[str, str]:
        exc_type, exc_value, exc_tb = exc_info
        return {
            "exc_type": exc_type.__name__,
            "exc_message": self.text(str(exc_value)),
            "traceback": self.text(
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            ),
        }


class RedactionFilter(logging.Filter):
    """Redacts the message, `extra=` fields, bound context and exception text in place.
    Must run after ContextFilter, so the context it copies is redacted too."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        r = self.redactor
        try:
            record.msg = r.text(record.getMessage())
        except Exception:  # noqa: BLE001 - a bad format string must not drop the record
            record.msg = r.text(str(record.msg))
        record.args = None

        for key, value in extra_fields(record).items():
            setattr(record, key, r.value(key, value))

        context = getattr(record, CONTEXT_ATTR, None)
        if context:
            setattr(record, CONTEXT_ATTR, {k: r.value(k, v) for k, v in context.items()})

        if record.exc_info and record.exc_info[0] is not None and not hasattr(record, EXC_ATTR):
            setattr(record, EXC_ATTR, r.exception(record.exc_info))
        return True
