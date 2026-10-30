"""Logging for stdio MCP servers: everything goes to stderr, stdout belongs to the protocol."""

from __future__ import annotations

import logging
import sys


def configure(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
