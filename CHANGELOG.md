# Changelog

## 0.1.1

- Fixed: `quiet_paths=("/", ...)` in `RequestIdMiddleware`/`WSGIRequestIdMiddleware` no longer
  silences the whole app's access log. `"/"` is a prefix of every path, so it previously matched
  (and quieted) every request; it now matches only the root path itself. Other entries in
  `quiet_paths` are unaffected and still match by prefix.

## 0.1.0

First release, extracted from the RSL service-automation app.

- One-call setup: `boxlog.setup(service=...)`, with JSON or text output to stdout.
- Correlation context via `bind(...)`, carried across `await` and into asyncio tasks.
- `log_step()`, which logs start, ok (with `duration_ms`) or failed (with traceback) for a unit of
  work. Each traceback is logged once, even when steps are nested.
- Secret redaction for bearer and basic auth, credentials embedded in URLs, and password, token,
  key and cookie fields. You can add your own keys and patterns.
- A non-blocking handler that ships records to a backend off-thread in batches.
- Memory, SQLite and Redis backends, selectable with `backend_from_url()`.
- ASGI and WSGI request-id middleware.
- A web viewer as a dependency-free ASGI app you can mount anywhere, plus `boxlog serve`.
