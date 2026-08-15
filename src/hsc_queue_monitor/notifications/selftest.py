"""One message to every recipient, to prove the transport works.

This is the answer to "is Telegram set up correctly?" without touching anything
else to find out. It opens no database, reads no session, calls no HSC endpoint
and starts no browser — a test that needed all of those to run would tell you
about all of those when it failed, and the one thing it is meant to isolate is
the bot.

Deliberately not importing :mod:`.templates_uk`: those templates are built from
availability types, and reaching them would drag the API package into a path
that has no business there. The message below is a literal for exactly that
reason, and a boundary test walks the import graph to keep it so.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .telegram import TelegramNotifier

logger = logging.getLogger(__name__)

#: Plain text, no parse mode. Nothing here needs escaping, and a formatting
#: error in a *connectivity test* would be a particularly annoying way to fail.
TEST_MESSAGE: Final = (
    "✅ Тестове повідомлення\n"
    "\n"
    "HSC Parser успішно підключений до Telegram."
)


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """How the one message fared, per recipient."""

    recipients: int
    sent: int
    failed: int

    @property
    def ok(self) -> bool:
        """Everyone configured received it. Anything less is a failure to report."""
        return self.failed == 0 and self.sent == self.recipients


def send_test_message(
    notifier: TelegramNotifier, *, emit: Callable[[str], None] = print
) -> DeliveryReport:
    """Send the test message to every recipient and describe what happened.

    Delivery is the transport's business, including its per-recipient error
    handling: one person who has never pressed Start does not stop the others,
    and the reason for each failure is logged there — masked, and without the
    URL that carries the token.
    """
    emit("\nTELEGRAM TEST\n")
    emit(f"Recipients: {len(notifier.recipients)}\n")

    notifier.send(TEST_MESSAGE)
    report = DeliveryReport(
        recipients=len(notifier.recipients),
        sent=notifier.delivered,
        failed=notifier.failed,
    )

    if report.ok:
        emit(
            f"Telegram test notification sent successfully to {report.sent} "
            "recipient(s).\n"
        )
        return report

    emit(f"Sent:   {report.sent}")
    emit(f"Failed: {report.failed}\n")
    emit(
        "At least one recipient did not receive the message. The log lines above\n"
        "say which and why — a 403 usually means that person has not opened the\n"
        "bot and pressed Start, or has blocked it.\n"
    )
    return report
