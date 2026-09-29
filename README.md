# boxlog

Structured JSON logging for Python, with request and job tracing, secret redaction,
pluggable storage and a built-in web log viewer.

```bash
pip install boxfusion-log              # core: stdlib only, no dependencies
pip install "boxfusion-log[redis]"     # + Redis storage
pip install "boxfusion-log[web]"       # + the standalone `boxlog serve` viewer
pip install "boxfusion-log[all]"
```

```python
import boxlog   # the package installs as boxfusion-log but imports as boxlog
```

boxlog builds on the standard `logging` module, so it needs no new logger API. Your existing
`logging.getLogger(__name__)` calls, and every third-party library's logs, flow through it
unchanged.

## Quick start

```python
import logging
import boxlog

boxlog.setup(service="billing", backend="sqlite:///logs/billing.db")
log = logging.getLogger(__name__)

log.info("Service started", extra={"event": "app.startup", "version": "1.4.2"})
```

Output on stdout, one JSON object per line:

```json
{"ts": "2026-09-29T09:16:21.180+00:00", "level": "INFO", "logger": "__main__",
 "message": "Service started", "event": "app.startup", "service": "billing",
 "extra": {"version": "1.4.2"}}
```

Use `stdout="text"` for readable lines during development.

## Logging a unit of work: `log_step`

One `with` block logs the start of the work, then either its success (with duration and
any result fields) or its failure (with the traceback), and re-raises the error:

```python
with boxlog.log_step(log, "payment.charge", customer="c-42", amount=1999) as step:
    receipt = gateway.charge(...)
    step["receipt_id"] = receipt.id
```

| outcome | event                   | level | extras                                                  |
|---------|-------------------------|-------|---------------------------------------------------------|
| start   | `payment.charge.start`  | DEBUG | `customer`, `amount`                                    |
| success | `payment.charge.ok`     | INFO  | `customer`, `amount`, `receipt_id`, `duration_ms`       |
| failure | `payment.charge.failed` | ERROR | the fields, `duration_ms`, `error_type` and a traceback |

It works around `await` too. Nested steps log each traceback only once, in the innermost
step; outer steps still log `.failed`, so you see the chain of work that broke without
reading the same traceback five times. In your own `except` blocks, check
`boxlog.already_logged(exc)` for the same effect.

## Tracing: `bind`

Fields you bind are added to every record logged inside the block, including records
from awaited code and from asyncio tasks started inside it:

```python
with boxlog.bind(job_id=job.id, tenant="acme"):
    process(job)          # every record in here carries job_id and tenant
```

Field names must be lower_snake_case. The web middleware binds `request_id` for you.

## Web frameworks

**FastAPI / Starlette (ASGI):**

```python
import os
from fastapi import FastAPI
import boxlog

boxlog.setup(service="api", backend="redis://localhost:6379/0?key=api:logs")

app = FastAPI()
app.add_middleware(boxlog.RequestIdMiddleware, quiet_paths=("/health", "/logs"))
app.mount("/logs", boxlog.create_viewer(
    boxlog.backend_from_url("redis://localhost:6379/0?key=api:logs"),
    token=os.environ["LOGS_TOKEN"],
    title="API logs",
))
```

**Flask / Django (WSGI):**

```python
app.wsgi_app = boxlog.WSGIRequestIdMiddleware(app.wsgi_app)            # Flask
application = boxlog.WSGIRequestIdMiddleware(get_wsgi_application())    # Django
```

Then view logs with the standalone viewer (below).

The middleware:
- reuses an incoming `X-Request-ID` if it's well-formed, and otherwise generates one;
- echoes the id back on the response;
- logs one `http.request` record per request (method, path, status and duration);
- logs any unhandled exception with its traceback.

## The web viewer

The viewer has:
- a level filter (including "Failures only");
- service and source filters, populated from your data;
- an event-prefix filter and text search;
- click-to-trace on any `request_id` or `job_id`;
- live tail;
- an expandable view of every record and its traceback.

- **Mounted** inside an ASGI app: `create_viewer(backend, token=...)`, as above.
- **Standalone**, for any app, including non-web workers and scripts:

  ```bash
  export BOXLOG_TOKEN="$(boxlog token)"
  boxlog serve sqlite:///logs/billing.db          # http://127.0.0.1:8765/
  boxlog serve "redis://localhost:6379/0?key=api:logs" --port 9000 --title "API logs"
  ```

**Security.** Logs contain whatever your users send you, so treat the viewer like an admin
console:
- It requires a bearer token of **at least 32 characters**. Generate one with `boxlog token`.
- If the token is unset, the mounted viewer answers 404 everywhere.
- The page renders log content only as text, never as HTML, and ships under a strict
  Content-Security-Policy with no inline script.
- Serve it over HTTPS. The standalone server binds to `127.0.0.1` unless you pass `--host`.

`create_viewer` also takes callables for `backend` and `token`. That's useful when they're
only known at startup (for example, inside a lifespan).

## Storage backends

| backend         | URL                                                  | good for                                                                   |
|-----------------|------------------------------------------------------|----------------------------------------------------------------------------|
| `MemoryBackend` | `memory://?max=5000`                                 | tests and scripts; lost on restart                                         |
| `SQLiteBackend` | `sqlite:///logs/app.db?max=100000`                   | one machine, no server; indexed queries; processes on one host can share the file |
| `RedisBackend`  | `redis://host:6379/0?key=app:logs&max=10000`         | several processes or machines; survives restarts                           |

```python
boxlog.setup(service="worker", backend=boxlog.SQLiteBackend("logs/worker.db"))
# or attach later, once settings are known:
boxlog.attach_backend("redis://cache:6379/0?key=worker:logs")
```

Records reach the backend from a background thread, in batches, so logging never blocks
your code or an event loop. If the backend is down, records are dropped (with one warning
on stderr) and your app carries on; stdout logging is unaffected. Several services can
share one backend, and the viewer shows a service filter when it finds more than one.

A custom backend needs three methods, `append(entries)`, `query(query)` and `close()`. See
`boxlog.backends.base.LogBackend`.

## Redaction

Before any output, boxlog masks:
- values of sensitive keys in `extra=` and bound fields: `password`, `token`, `api_key`,
  `secret`, `authorization`, `cookie`, … (as whole names or `_` suffixes, so `access_token`
  is masked but `total_tokens` isn't);
- `Bearer …` and `Basic …` credentials in text;
- `password=…` and `"token": "…"` pairs in text;
- passwords inside URLs (`redis://user:secret@host`);
- all of the above in exception messages and tracebacks.

You can add your own keys and patterns:

```python
boxlog.setup(redact_keys=["national_id"], redact_patterns=[r"\b\d{13}\b"])
```

Redaction is a safety net. Don't log secrets or whole request bodies on purpose.

## `setup()` reference

```python
boxlog.setup(
    service=None,              # tagged on every record
    level="INFO",
    stdout="json",             # "json", "text" or None
    backend=None,              # LogBackend or URL
    redact_keys=(), redact_patterns=(),
    promoted_fields=("request_id", "job_id", "source"),  # extra= keys lifted to top level
    quiet_loggers=("httpx", "httpcore", "urllib3", "openai", "botocore", "redis", "asyncio"),
    capture_uvicorn=True,      # route uvicorn's logs through boxlog, drop its access log
)
```

To quiet more libraries while keeping the defaults, pass
`quiet_loggers=(*boxlog.DEFAULT_QUIET_LOGGERS, "pinecone")`.

You can call it again to change settings. It only replaces the handlers it installed
itself. `boxlog.shutdown()` (also registered with `atexit`) flushes queued records.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m build && .venv/bin/python -m twine check dist/*
```

## License

MIT
