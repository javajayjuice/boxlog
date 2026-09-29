"""log_step(): one line of code per unit of work, covering success and failure.

    with log_step(logger, "db.query", table="users") as step:
        rows = await db.fetch(...)
        step["rows"] = len(rows)

logs `db.query.start` (DEBUG), then either `db.query.ok` (INFO) with `duration_ms` and
every field in `step`, or `db.query.failed` (ERROR) with the traceback - and re-raises.
It is a plain context manager, which works fine around `await` inside a coroutine.

Nested steps would otherwise log the same traceback once per level, so the first step
to log an exception marks it; outer steps still log `.failed` (so the chain of work is
visible) but without repeating the traceback. Code that catches an exception and wants
to log it can check `already_logged(exc)` the same way.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any, Optional

from boxlog._record import safe_extra

_LOGGED_MARK = "__boxlog_logged__"


def already_logged(exc: BaseException) -> bool:
    return bool(getattr(exc, _LOGGED_MARK, False))


def mark_logged(exc: BaseException) -> None:
    try:
        setattr(exc, _LOGGED_MARK, True)
    except Exception:  # noqa: BLE001 - some exception types reject attributes
        pass


class log_step:  # noqa: N801 - reads as a function at call sites
    def __init__(
        self,
        logger: logging.Logger,
        event: str,
        *,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        self._logger = logger
        self._event = event
        self._level = level
        self.fields: dict[str, Any] = dict(fields)
        self._start = 0.0

    def __enter__(self) -> dict[str, Any]:
        self._start = time.perf_counter()
        self._logger.debug(
            "%s started",
            self._event,
            extra=safe_extra({"event": f"{self._event}.start", **self.fields}),
        )
        return self.fields

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        duration_ms = round((time.perf_counter() - self._start) * 1000, 1)
        fields = {**self.fields, "duration_ms": duration_ms}

        if exc_type is None:
            self._logger.log(
                self._level,
                "%s ok (%.1f ms)",
                self._event,
                duration_ms,
                extra=safe_extra({"event": f"{self._event}.ok", **fields}),
            )
        elif not issubclass(exc_type, Exception):
            # CancelledError / KeyboardInterrupt / SystemExit: not a failure of the step.
            self._logger.warning(
                "%s cancelled after %.1f ms",
                self._event,
                duration_ms,
                extra=safe_extra({"event": f"{self._event}.cancelled", **fields}),
            )
        else:
            first = exc is None or not already_logged(exc)
            self._logger.error(
                "%s failed after %.1f ms: %s: %s",
                self._event,
                duration_ms,
                exc_type.__name__,
                exc,
                exc_info=(exc_type, exc, tb) if first else None,
                extra=safe_extra(
                    {"event": f"{self._event}.failed", "error_type": exc_type.__name__, **fields}
                ),
            )
            if exc is not None:
                mark_logged(exc)
        return False
