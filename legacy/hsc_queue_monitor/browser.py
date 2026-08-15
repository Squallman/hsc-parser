"""Persistent Chromium session management.

A single ``launch_persistent_context`` profile owns everything session related:
authentication, cookies, anti-bot state and their rotation. We only observe
whether an authenticated session exists — cookie *values* are never read,
logged or copied anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import TracebackType
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .config import Settings

logger = logging.getLogger(__name__)

#: Presence (never the value) of one of these cookies means "signed in".
AUTH_COOKIE_NAMES: tuple[str, ...] = (
    "__Secure-auth.access-token",
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)

#: URL fragments that indicate the user is on a login / verification screen.
AUTH_URL_MARKERS: tuple[str, ...] = ("/auth", "/login", "/signin", "id.gov.ua", "/sign-in")

AUTH_POLL_SECONDS = 3.0


class BrowserSession:
    """Owns the Playwright driver, the persistent context and the main page."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # -- lifecycle ----------------------------------------------------------
    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserSession is not started")
        return self._context

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession is not started")
        return self._page

    async def start(self) -> Page:
        """Launch (or reuse) the persistent Chromium profile."""
        self.settings.ensure_directories()
        profile: Path = self.settings.profile_dir
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": self.settings.headless,
            "locale": self.settings.locale,
            "timezone_id": self.settings.timezone,
            "accept_downloads": False,
        }
        if self.settings.headless:
            launch_kwargs["viewport"] = {"width": 1440, "height": 900}
        else:
            launch_kwargs["viewport"] = None
            launch_kwargs["args"] = ["--window-size=1440,960"]

        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._context.set_default_timeout(self.settings.navigation_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        logger.info("Browser started (headless=%s, profile=%s)", self.settings.headless, profile)
        return self._page

    async def close(self) -> None:
        """Close the context and driver, leaving the profile on disk intact."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:  # pragma: no cover - shutdown races
                logger.debug("Context close raised: %s", exc)
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("Playwright stop raised: %s", exc)
            self._playwright = None
        logger.info("Browser closed (profile preserved)")

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- navigation / auth --------------------------------------------------
    async def open_queue_page(self) -> None:
        """Navigate to the configured queue page."""
        logger.info("Opening %s", self.settings.queue_url)
        try:
            await self.page.goto(self.settings.queue_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Navigation problem: %s", exc)
        await self._settle()

    async def _settle(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("networkidle", timeout=15_000)

    async def has_auth_cookie(self) -> bool:
        """True when the profile carries a session cookie (name check only)."""
        cookies = await self.context.cookies()
        names = {str(cookie.get("name", "")) for cookie in cookies}
        return any(name in names for name in AUTH_COOKIE_NAMES)

    def on_auth_page(self) -> bool:
        url = (self._page.url if self._page else "") or ""
        return any(marker in url for marker in AUTH_URL_MARKERS)

    async def is_authenticated(self) -> bool:
        """Best-effort authentication check: session cookie + not on a login page."""
        if self.on_auth_page():
            return False
        return await self.has_auth_cookie()

    async def ensure_authenticated(self, *, timeout_seconds: float | None = None) -> bool:
        """Open the queue page and, if needed, wait for a manual login.

        CAPTCHA solving and credential entry are always the user's job; this
        only watches for the session to appear.
        """
        await self.open_queue_page()
        if await self.is_authenticated():
            logger.info("Authenticated session detected")
            return True

        limit = self.settings.auth_timeout_seconds if timeout_seconds is None else timeout_seconds
        if self.settings.headless:
            logger.error(
                "No authenticated session and the browser is headless. Re-run with --headed "
                "(e.g. `python -m hsc_queue_monitor.cli login`) and sign in manually."
            )
            return False

        _print_auth_banner(self.settings.queue_url, limit)
        deadline = asyncio.get_running_loop().time() + limit
        announced = False
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(AUTH_POLL_SECONDS)
            if self._context is None:
                logger.warning("Browser was closed before authentication completed")
                return False
            try:
                authenticated = await self.is_authenticated()
            except Exception as exc:  # pragma: no cover - page may be navigating
                logger.debug("Auth check failed transiently: %s", exc)
                continue
            if authenticated:
                await self._settle()
                logger.info("Authenticated session detected")
                return True
            if not announced and self.on_auth_page():
                announced = True
                logger.info("Login page detected, waiting for you to finish signing in…")

        logger.error("Timed out after %.0fs waiting for manual authentication", limit)
        return False

    async def wait_until_closed(self, stop: asyncio.Event | None = None) -> None:
        """Block until the browser window is closed or ``stop`` is set."""
        closed = asyncio.Event()
        if self._context is not None:
            self._context.on("close", lambda _: closed.set())

        waiters = [asyncio.ensure_future(closed.wait())]
        if stop is not None:
            waiters.append(asyncio.ensure_future(stop.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()


def _print_auth_banner(url: str, timeout: float) -> None:
    print(
        "\n"
        "==================================================================\n"
        " MANUAL AUTHENTICATION REQUIRED\n"
        "------------------------------------------------------------------\n"
        f" 1. Use the Chromium window that just opened ({url}).\n"
        " 2. Sign in yourself (Diia / id.gov.ua / bank ID / whatever applies)\n"
        "    and complete any browser verification the site shows.\n"
        " 3. Leave the window open — the session is stored in the persistent\n"
        "    profile and reused by later runs.\n"
        f" Waiting up to {timeout:.0f}s; monitoring continues automatically.\n"
        "==================================================================\n",
        flush=True,
    )


__all__ = ["AUTH_COOKIE_NAMES", "BrowserSession"]
