"""What a notifier is, and what it is not allowed to know.

One method, one direction. A notifier takes finished text and delivers it. It
does not scan, does not read a session, does not decide what is worth saying and
does not receive anything — deciding belongs to
:class:`~.dispatcher.NotificationDispatcher`, and receiving belongs to nobody
here at all.

Keeping the transport this thin is what lets the templates be tested without a
network and the dispatcher without a bot.
"""

from __future__ import annotations

from typing import Protocol

from ..models import HscMonitorError


class NotificationError(HscMonitorError):
    """Delivery failed.

    Never fatal to a monitoring run: by the time anything is sent, the session,
    the monitor state and the availability snapshot are already persisted. A
    message that did not arrive is a message, not a corrupted scan.
    """


class Notifier(Protocol):
    """Delivers one finished message to whoever it was configured for."""

    def send(self, message: str) -> None: ...
