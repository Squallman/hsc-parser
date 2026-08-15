"""Allow ``python -m hsc_queue_monitor`` as a shortcut for the CLI."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
