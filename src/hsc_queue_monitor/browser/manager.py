"""Persistent Chromium context.

The profile in ``data/browser-profile/`` is what makes this work: it keeps the
normal browser cookies, the authenticated HSC session and whatever anti-bot
state the browser generates on its own. We never read or forge any of it — the
browser is simply allowed to behave like a browser across runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ViewportSize,
    async_playwright,
)

logger = logging.getLogger(__name__)

VIEWPORT: ViewportSize = {"width": 1440, "height": 1000}

#: A real, current desktop Chrome UA string is *not* set here on purpose:
#: launch_persistent_context already reports a genuine Chromium identity.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]


class BrowserManager:
    """Async context manager around ``chromium.launch_persistent_context``."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        slow_mo: int = 0,
        locale: str = "uk-UA",
        timezone: str = "Europe/Kyiv",
        default_timeout: int = 15_000,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.slow_mo = slow_mo
        self.locale = locale
        self.timezone = timezone
        self.default_timeout = default_timeout

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    # --------------------------------------------------------- lifecycle ----

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> BrowserContext:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Launching persistent Chromium profile at %s", self.profile_dir)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport=VIEWPORT,
            locale=self.locale,
            timezone_id=self.timezone,
            args=LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        self._context.set_default_timeout(self.default_timeout)
        return self._context

    async def stop(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:  # pragma: no cover - best effort teardown
                logger.debug("Error while closing browser context: %s", exc)
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.debug("Browser stopped; profile preserved at %s", self.profile_dir)

    # ------------------------------------------------------------ access ----

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserManager.start() has not been called")
        return self._context

    @property
    def browser(self) -> Browser | None:  # pragma: no cover - passthrough
        return self.context.browser

    async def page(self) -> Page:
        """The persistent context's first page, creating one if needed."""
        context = self.context
        if context.pages:
            return context.pages[0]
        return await context.new_page()

    async def goto(self, url: str, *, timeout: int = 30_000) -> Page:
        page = await self.page()
        logger.info("Navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return page
