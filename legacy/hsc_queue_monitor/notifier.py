"""Notification sinks.

``ConsoleNotifier`` is always active. ``TelegramNotifier`` is optional and uses
the stdlib only; the bot token is never logged, printed or written to state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .models import AvailableDate, AvailableSlot, Department

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """Something worth telling the user about."""

    kind: str  # "new_slot" | "date_available"
    service_id: int
    department: str
    date: str
    time: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_slot(
        cls, slot: AvailableSlot, *, service_id: int, department: Department | None = None
    ) -> NotificationEvent:
        return cls(
            kind="new_slot",
            service_id=service_id,
            department=_department_label(department, slot.department_id),
            date=slot.date,
            time=slot.time,
        )

    @classmethod
    def from_date(
        cls, item: AvailableDate, *, service_id: int, department: Department | None = None
    ) -> NotificationEvent:
        return cls(
            kind="date_available",
            service_id=service_id,
            department=_department_label(department, item.department_id),
            date=item.date,
            extra={"free_count": item.free_count} if item.free_count is not None else {},
        )

    def render(self) -> str:
        header = (
            "NEW HSC APPOINTMENT AVAILABLE"
            if self.kind == "new_slot"
            else "HSC DATE BECAME AVAILABLE"
        )
        lines = [
            "",
            header,
            "",
            f"Service: {self.service_id}",
            f"Department: {self.department}",
            f"Date: {self.date}",
        ]
        if self.time:
            lines.append(f"Time: {self.time}")
        for key, value in self.extra.items():
            lines.append(f"{key.replace('_', ' ').title()}: {value}")
        lines.append("")
        return "\n".join(lines)


def _department_label(department: Department | None, department_id: int | None) -> str:
    if department is not None:
        return department.describe()
    return str(department_id) if department_id is not None else "unknown"


class Notifier(ABC):
    """Interface every notification sink implements."""

    @abstractmethod
    async def notify(self, event: NotificationEvent) -> None: ...

    async def notify_many(self, events: list[NotificationEvent]) -> None:
        for event in events:
            await self.notify(event)

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class ConsoleNotifier(Notifier):
    """Prints notifications to stdout."""

    async def notify(self, event: NotificationEvent) -> None:
        print(event.render(), flush=True)


class TelegramNotifier(Notifier):
    """Optional Telegram sink (stdlib HTTP, executed off the event loop)."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier requires both a bot token and a chat id")
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def notify(self, event: NotificationEvent) -> None:
        text = event.render().strip()
        try:
            await asyncio.to_thread(self._send, text)
            logger.info("Telegram notification sent to chat %s", self._chat_id)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Never include the URL: it embeds the bot token.
            logger.warning("Telegram notification failed: %s", type(exc).__name__)

    def _send(self, text: str) -> None:
        url = f"{TELEGRAM_API}/bot{self._bot_token}/sendMessage"
        payload = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
        parsed = json.loads(body) if body else {}
        if not parsed.get("ok", False):
            raise ValueError(f"Telegram API rejected the message: {parsed.get('description')}")

    def __repr__(self) -> str:  # pragma: no cover - keeps the token out of logs
        return f"TelegramNotifier(chat_id={self._chat_id!r})"


class CompositeNotifier(Notifier):
    """Fans a notification out to several sinks; one failure never blocks others."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    async def notify(self, event: NotificationEvent) -> None:
        for notifier in self.notifiers:
            try:
                await notifier.notify(event)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("%s failed: %s", type(notifier).__name__, exc)

    async def close(self) -> None:
        for notifier in self.notifiers:
            await notifier.close()


def build_notifier(settings: Settings) -> Notifier:
    """Console always; Telegram too when both env values are present."""
    sinks: list[Notifier] = [ConsoleNotifier()]
    if settings.telegram_enabled:
        assert settings.telegram_bot_token and settings.telegram_chat_id
        sinks.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
        logger.info("Telegram notifications enabled (chat %s)", settings.telegram_chat_id)
    else:
        logger.debug("Telegram notifications disabled")
    return sinks[0] if len(sinks) == 1 else CompositeNotifier(sinks)
