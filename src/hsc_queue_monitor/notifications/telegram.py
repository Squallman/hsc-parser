"""Telegram, outbound only.

One endpoint — ``POST /sendMessage`` — once per recipient, and nothing else.
There is no ``getUpdates``, no ``setWebhook``, no ``deleteWebhook`` and no HTTP
server: this bot cannot receive a message, and a test reads the source to keep
it that way. A recipient has to press Start in Telegram once, because a bot may
not open a conversation on its own; after that, their numeric id is all this
needs.

Two boundaries worth stating plainly.

**The GET-only rule is about HSC, not about Telegram.** Nothing may POST to
``eqn.hsc.gov.ua`` — that guard is untouched and still enforced over
:mod:`~..api`. Telegram is a different host with a different purpose, and
sending a message there is the whole point.

**The token is in the URL.** ``https://api.telegram.org/bot<TOKEN>/sendMessage``
carries the secret in its path, so the URL is never logged, never put in an
error message and never handed to a traceback. Recipients are masked too: an
id is not a secret, but it is somebody's account.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Protocol

import requests

from ..logging_config import sanitize
from .base import NotificationError

logger = logging.getLogger(__name__)

API_ROOT: Final = "https://api.telegram.org"
SEND_MESSAGE: Final = "sendMessage"

#: Telegram's own limit is 4096 characters. Staying under it leaves room for the
#: continuation markers chunking adds, and for a name longer than expected.
MAX_TELEGRAM_TEXT: Final = 3900

#: (connect, read). One attempt per recipient — see the module docstring of
#: :mod:`..api.retry` for why this deliberately does not reuse the HSC policy: a
#: notification that failed is not worth a second request at HSC's expense.
DEFAULT_TIMEOUT: Final[tuple[float, float]] = (5.0, 15.0)

#: What each status means for the *recipient*, not for the run.
_REASONS: Final[dict[int, str]] = {
    400: "the request was rejected — the chat id may be wrong, or the recipient "
    "has never pressed Start in the bot",
    401: "the bot token was not accepted (check TELEGRAM_BOT_TOKEN)",
    403: "the bot cannot write to this chat — it may have been blocked",
    429: "Telegram is rate limiting this bot",
}


class Response(Protocol):
    """The slice of ``requests.Response`` this module reads. Never ``.text``."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


Post = Callable[[str, Mapping[str, Any], tuple[float, float]], Response]


def http_post(
    url: str, payload: Mapping[str, Any], timeout: tuple[float, float]
) -> Response:
    """The one real network call. POST, and only to Telegram."""
    return requests.post(url, json=dict(payload), timeout=timeout)


def mask(chat_id: int) -> str:
    """``123456789`` -> ``***6789``. Enough to tell recipients apart, no more."""
    text = str(chat_id)
    return f"***{text[-4:]}" if len(text) > 4 else "***"


class TelegramNotifier:
    """Sends one text to each configured recipient. Independently.

    A failure for one recipient is logged and the rest still receive the
    message: one person blocking the bot is not a reason for everybody else to
    hear nothing.
    """

    def __init__(
        self,
        token: str,
        recipients: Sequence[int],
        *,
        post: Post | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        if not token:
            raise NotificationError("A Telegram bot token is required to send anything.")
        self._token = token
        self.recipients = tuple(recipients)
        # Resolved here rather than as a default argument, so the module-level
        # function is looked up when the notifier is built.
        self._post: Post = post if post is not None else http_post
        self.timeout = timeout
        #: Counted for the log line, not for control flow.
        self.delivered = 0
        self.failed = 0

    @property
    def _url(self) -> str:
        """Built here and used immediately. Never logged, never returned."""
        return f"{API_ROOT}/bot{self._token}/{SEND_MESSAGE}"

    def send(self, message: str) -> None:
        """Deliver one message to every recipient, one request each."""
        for chat_id in self.recipients:
            self._send_one(chat_id, message)

    def _send_one(self, chat_id: int, message: str) -> None:
        try:
            response = self._post(
                self._url, {"chat_id": chat_id, "text": message}, self.timeout
            )
        except requests.RequestException as exc:
            # str(exc) can contain the request URL, and the URL contains the
            # token, so only the class is safe to say.
            self.failed += 1
            logger.warning(
                "Telegram sendMessage -> recipient %s -> failed (%s)",
                mask(chat_id),
                type(exc).__name__,
            )
            return

        status = int(response.status_code)
        if status == 200:
            self.delivered += 1
            logger.info("Telegram sendMessage -> recipient %s -> 200", mask(chat_id))
            return

        self.failed += 1
        detail = _REASONS.get(status, "delivery failed")
        if status == 429:
            # Logged, not slept on: the next scheduled run is minutes away, and
            # holding a GitHub runner open helps nobody.
            retry_after = str(response.headers.get("Retry-After", "")).strip()
            if retry_after:
                detail = f"{detail} (Retry-After: {sanitize(retry_after)})"
        logger.warning(
            "Telegram sendMessage -> recipient %s -> %d: %s",
            mask(chat_id),
            status,
            detail,
        )
