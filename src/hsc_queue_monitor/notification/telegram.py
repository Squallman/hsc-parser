"""Telegram notifier.

Uses ``urllib`` on a worker thread rather than pulling in an HTTP dependency.
The bot token is never logged: it is registered as a redaction secret and only
ever appears inside the request URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from .base import Notification, Notifier

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 15


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier requires both a bot token and a chat id")
        self._token = bot_token
        self._chat_id = chat_id

    async def send(self, notification: Notification) -> None:
        text = notification.render()
        try:
            await asyncio.to_thread(self._post, text)
        except urllib.error.HTTPError as exc:
            logger.error("Telegram rejected the message (HTTP %s)", exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("Could not reach Telegram: %s", exc.reason if
                         hasattr(exc, "reason") else exc)
        else:
            logger.info("Telegram notification sent for %s", notification.service_center)

    def _post(self, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            API_TEMPLATE.format(token=self._token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            response.read()
