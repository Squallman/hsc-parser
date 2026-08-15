"""The cabinet entry point and the queue registration screen.

``/cabinet/queue`` is deliberately never opened by URL. It is reached only by
clicking ``queue.start_registration`` from the cabinet, the way a person gets
there — navigating straight to it skips whatever state the site sets up on the
way and is exactly the kind of shortcut this project avoids.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page

from ..browser.diagnostics import Diagnostics
from ..config import SelectorRegistry
from .base_page import BasePage

logger = logging.getLogger(__name__)


class QueuePage(BasePage):
    START_REGISTRATION = "queue.start_registration"

    def __init__(
        self,
        page: Page,
        selectors: SelectorRegistry,
        *,
        cabinet_url: str,
        diagnostics: Diagnostics | None = None,
        default_timeout: int = 15_000,
        navigation_timeout: int = 30_000,
    ) -> None:
        super().__init__(
            page, selectors, diagnostics=diagnostics, default_timeout=default_timeout
        )
        self.cabinet_url = cabinet_url
        self.navigation_timeout = navigation_timeout

    async def open(self) -> None:
        """Open the cabinet — the entry point of every booking journey."""
        logger.info("Opening %s", self.cabinet_url)
        await self.page.goto(
            self.cabinet_url, wait_until="domcontentloaded", timeout=self.navigation_timeout
        )
        await self.wait_stable()

    async def start_registration(self) -> None:
        """Click through to /cabinet/queue. The only way this project gets there."""
        await self.click(self.START_REGISTRATION, step="queue.start_registration")
