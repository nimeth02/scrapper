"""Run the web UI: ``python -m app.web [--host 127.0.0.1] [--port 8000]``.

When the requested port is busy the next free one is used (up to 20 ports up), and the URL that is
actually serving is printed.
"""

from __future__ import annotations

import argparse
import logging
import socket

import uvicorn


def free_port(host: str, wanted: int, attempts: int = 20) -> int:
    """``wanted`` if it can be bound, otherwise the first free port above it."""
    for port in range(wanted, wanted + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port between {wanted} and {wanted + attempts - 1} on {host}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web UI for the Instagram hashtag crawler")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="first port to try (default 8000)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    port = free_port(args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is in use; using {port} instead.", flush=True)
    print(f"Hashtag Scout: http://{args.host}:{port}", flush=True)
    uvicorn.run("app.web.server:app", host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
