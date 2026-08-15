"""Logging configuration shared by every CLI command."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with a compact single-line format."""
    resolved = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)

    # Playwright is chatty at DEBUG and can echo headers; keep it quiet.
    logging.getLogger("asyncio").setLevel(max(resolved, logging.INFO))
