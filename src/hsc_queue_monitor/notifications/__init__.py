"""Outbound-only notifications.

Three pieces, deliberately separate:

* :mod:`.base`          what a notifier is — one method, and nothing else;
* :mod:`.telegram`      the transport: HTTPS ``sendMessage``, and no receiving;
* :mod:`.templates_uk`  what the messages say, in Ukrainian, with chunking;
* :mod:`.dispatcher`    what is worth saying at all.

The bot **sends**. There is no ``getUpdates``, no webhook, no HTTP server and no
command handling anywhere in this package, and a test asserts it.
"""

from __future__ import annotations

from .base import NotificationError, Notifier
from .dispatcher import NotificationDispatcher
from .telegram import MAX_TELEGRAM_TEXT, TelegramNotifier
from .templates_uk import render_auth_required, render_availability

__all__ = [
    "MAX_TELEGRAM_TEXT",
    "NotificationDispatcher",
    "NotificationError",
    "Notifier",
    "TelegramNotifier",
    "render_auth_required",
    "render_availability",
]
