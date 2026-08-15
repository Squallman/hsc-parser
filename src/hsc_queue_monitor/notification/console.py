"""Console notifier — also the dry-run backend."""

from __future__ import annotations

import logging

from .base import Notification, Notifier

logger = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    async def send(self, notification: Notification) -> None:
        header = "WOULD SEND (dry run)" if self.dry_run else "NOTIFICATION"
        border = "─" * 56
        print(f"\n{border}\n{header}\n{border}\n{notification.render()}\n{border}\n")
