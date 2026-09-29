"""Command line:

    boxlog serve BACKEND_URL [--token-env BOXLOG_TOKEN] [--host 127.0.0.1] [--port 8765]
    boxlog token            print a new random viewer token

`serve` runs the viewer on its own, for apps that aren't ASGI (Flask, Django, scripts,
workers) or when you'd rather not expose logs from the app itself. Needs
`pip install boxfusion-log[web]`.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import Optional, Sequence


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("boxlog serve needs uvicorn: pip install 'boxfusion-log[web]'", file=sys.stderr)
        return 2

    from boxlog.backends import backend_from_url
    from boxlog.viewer import MIN_TOKEN_LENGTH, create_viewer

    token = os.environ.get(args.token_env, "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        print(
            f"Set {args.token_env} to a token of at least {MIN_TOKEN_LENGTH} characters "
            "(generate one with: boxlog token)",
            file=sys.stderr,
        )
        return 2

    try:
        backend = backend_from_url(args.backend)
    except (ValueError, ImportError) as exc:
        print(f"Invalid backend: {exc}", file=sys.stderr)
        return 2

    viewer = create_viewer(backend, token=token, title=args.title)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: serving logs on {args.host} - put it behind HTTPS; the token is "
            "sent on every request.",
            file=sys.stderr,
        )
    print(f"boxlog viewer on http://{args.host}:{args.port}/  (backend: {args.backend.split('?')[0]})")
    uvicorn.run(viewer, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="boxlog", description="boxlog tools")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web log viewer")
    serve.add_argument("backend", help="backend URL, e.g. sqlite:///logs.db or redis://host/0?key=app:logs")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--title", default="Logs")
    serve.add_argument(
        "--token-env",
        default="BOXLOG_TOKEN",
        help="environment variable holding the viewer token (default BOXLOG_TOKEN)",
    )
    serve.set_defaults(func=_serve)

    token = sub.add_parser("token", help="print a new random viewer token")
    token.set_defaults(func=lambda _args: print(secrets.token_urlsafe(32)) or 0)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
