"""Monitor the Ukrainian HSC electronic queue for appointment availability.

The package never forges cookies, never solves CAPTCHAs and never books an
appointment. It drives a real Chromium instance with a persistent profile and
issues same-origin ``fetch`` calls from inside the authenticated page, so the
browser itself owns every piece of session/anti-bot state.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
