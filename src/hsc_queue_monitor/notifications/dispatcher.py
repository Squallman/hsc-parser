"""What is worth telling someone about, and when.

The decision lives here rather than in the transport, so "should this be sent"
and "how is a message delivered" can be wrong independently — and tested
independently.

Two rules, both inherited rather than invented:

* **only persisted events are announced.** A scan hands over an
  :class:`~..api.availability_snapshot.AvailabilityDiff` only after the new
  snapshot was written, and a
  :class:`~..api.monitor_state.MonitorStateTransition` only after the state
  document was written. Anything that failed to persist arrives here as
  ``None``, which is exactly the right amount of news;
* **only a change is news.** A second consecutive AUTH_REQUIRED produces no
  transition at all — the gated run never even reaches the API — so nobody's
  phone repeats itself every five minutes.

Delivery failures stop here too. By the time anything is sent, the session, the
state and the snapshot are all stored; a message that did not arrive changes
none of them.
"""

from __future__ import annotations

import logging

from ..api.headless_monitor import HeadlessScan
from ..api.monitor_state import MonitorStatus
from .base import Notifier
from .templates_uk import (
    render_auth_required,
    render_availability,
    render_persistence_error,
    render_rate_limited,
    render_service_unavailable,
    render_unexpected_error,
)

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Turns a finished scan into zero or more messages."""

    def __init__(self, notifier: Notifier | None = None) -> None:
        self.notifier = notifier
        #: Counted for the log line and for tests; not control flow.
        self.sent = 0

    @property
    def enabled(self) -> bool:
        return self.notifier is not None

    def notify_scan(self, scan: HeadlessScan) -> int:
        """Send what this scan warrants. Returns the number of messages sent."""
        if self.notifier is None:
            return 0

        messages: list[str] = []

        diff = scan.availability
        if diff is not None and diff.changed:
            messages += render_availability(diff)

        transition = scan.transition
        if transition is not None and transition.changed:
            match transition.current:
                case MonitorStatus.AUTH_REQUIRED:
                    messages += render_auth_required(transition.reason)
                case MonitorStatus.RATE_LIMITED:
                    messages += render_rate_limited(transition.reason)
                case MonitorStatus.SERVICE_UNAVAILABLE:
                    messages += render_service_unavailable(transition.reason)
                case MonitorStatus.READY:
                    pass

        for message in messages:
            # A failed delivery is logged inside the transport and never raised
            # here: the run has already done everything that matters.
            self.notifier.send(message)

        if messages:
            self.sent += len(messages)
            logger.info("Telegram notification sent (%d message(s))", len(messages))
        return len(messages)

    def notify_unexpected_error(self) -> int:
        """Send a generic unexpected error notification. Returns messages sent."""
        if self.notifier is None:
            return 0

        try:
            messages = render_unexpected_error()
            for message in messages:
                self.notifier.send(message)

            if messages:
                self.sent += len(messages)
                logger.info("Error notification sent (%d message(s))", len(messages))
            return len(messages)
        except Exception as exc:
            logger.exception("Failed to send unexpected error notification: %s", exc)
            return 0

    def notify_persistence_error(self) -> int:
        """Send a persistence failure notification. Returns messages sent."""
        if self.notifier is None:
            return 0

        try:
            messages = render_persistence_error()
            for message in messages:
                self.notifier.send(message)

            if messages:
                self.sent += len(messages)
                logger.info("Persistence error notification sent (%d message(s))", len(messages))
            return len(messages)
        except Exception as exc:
            logger.exception("Failed to send persistence error notification: %s", exc)
            return 0
