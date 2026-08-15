"""The one authoritative authentication path.

Every operation that starts inside the cabinet goes through
:meth:`AuthManager.ensure_authenticated`. Nothing else navigates to the cabinet
in order to "check whether we are logged in", and nothing else knows how to log
in — there is exactly one implementation, and it is idempotent.

What it is *not*:

* it never reads, writes or forges an HSC cookie or token;
* it never emulates the ID.GOV.UA signing component;
* it never retries. One recovery attempt per call, then a clear failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NoReturn
from urllib.parse import urlsplit

from playwright.async_api import Page

from ..browser.diagnostics import Diagnostics
from ..config import AppConfig
from ..models import AuthenticationFailed
from ..pages.login_page import LoginPage, Prompt, host_of, is_idgov_url
from ..pages.queue_page import QueuePage

logger = logging.getLogger(__name__)


class AuthManager:
    """Detects, and if necessary restores, an authenticated HSC session."""

    def __init__(
        self,
        config: AppConfig,
        *,
        login: LoginPage,
        queue: QueuePage,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        self.config = config
        self.login = login
        self.queue = queue
        self.diagnostics = diagnostics
        # One authentication attempt at a time per browser context. The monitor
        # is sequential today; this keeps it correct if it ever stops being.
        self._lock = asyncio.Lock()
        self._warned_about_marker = False

    # --------------------------------------------------------------- urls ---

    @property
    def page(self) -> Page:
        """The one page every page object shares."""
        return self.login.page

    @property
    def cabinet_url(self) -> str:
        return self.config.flow.cabinet_url

    @property
    def site_host(self) -> str:
        return host_of(self.config.flow.base_url) or host_of(self.cabinet_url)

    def protects(self, url: str) -> bool:
        """Whether reaching *url* requires an authenticated session.

        Used by the flow engine to decide when to insert the guard, so a
        ``--url`` override pointing somewhere else does not trigger a login.
        """
        return self._is_cabinet_url(url)

    def _is_cabinet_url(self, url: str) -> bool:
        """A URL on the HSC host, under the cabinet path.

        Compared by host + path prefix, never as a whole string: the site adds
        query parameters and sub-paths that are not ours to predict.
        """
        cabinet = urlsplit(self.cabinet_url)
        current = urlsplit(url)
        if host_of(url) != self.site_host:
            return False
        prefix = cabinet.path.rstrip("/") or "/cabinet"
        return current.path == prefix or current.path.startswith(f"{prefix}/")

    def _is_signed_out_page(self, url: str) -> bool:
        """The HSC page the site redirects to when the session has expired."""
        return host_of(url) == self.site_host and not self._is_cabinet_url(url)

    # -------------------------------------------------------------- state ---

    async def is_authenticated(self) -> bool:
        """Whether the browser is *right now* inside an authenticated cabinet.

        Both halves are required: the URL must be under ``/cabinet`` and the
        cabinet-only marker must be visible. Neither alone is evidence — the
        marker can be configured wrongly, and the URL can be a cabinet page
        that is mid-redirect back to the login screen.

        Returns ``False`` rather than raising when the marker is not there.
        """
        url = self.page.url
        if not self._is_cabinet_url(url):
            logger.debug("Not authenticated: %s is not a cabinet URL", url)
            return False

        if not self.login.has_authenticated_marker:
            # Degraded but honest: the site itself throws an expired session
            # out of /cabinet, so staying here is real evidence — just weaker
            # than evidence plus a marker.
            if not self._warned_about_marker:
                self._warned_about_marker = True
                logger.warning(
                    "%s is not configured, so a cabinet URL is the only available "
                    "evidence that the session is alive.",
                    LoginPage.AUTHENTICATED,
                )
            return True

        # The full locator timeout, not a short probe: this branch is only
        # reached while the browser is sitting on a cabinet URL, where a slow
        # render must not be mistaken for an expired session. When the session
        # really has expired the URL check above has already answered.
        return await self.login.authenticated_marker_visible(
            timeout=self.config.flow.timeouts.default_locator
        )

    # ------------------------------------------------------------ recovery --

    async def ensure_authenticated(
        self,
        *,
        manual_provider: Prompt | None = None,
        manual_password: Prompt | None = None,
    ) -> None:
        """Leave the browser on an authenticated ``/cabinet``, logging in if needed.

        Idempotent: when the session is already live this opens the cabinet,
        sees the marker and returns without touching a single login control.

        The two ``manual_*`` hooks belong to the ``ensure-auth-debug-*``
        experiments and are off unless one of those commands asks for them;
        every other caller gets the production path unchanged.
        """
        async with self._lock:
            await self._ensure_authenticated(manual_provider, manual_password)

    async def _ensure_authenticated(
        self,
        manual_provider: Prompt | None = None,
        manual_password: Prompt | None = None,
    ) -> None:
        await self.queue.open()

        if await self.is_authenticated():
            logger.info("HSC authenticated session is active")
            return

        logger.info("Authentication session is not active")

        url = self.page.url
        if is_idgov_url(url):
            # An interrupted attempt can leave the OIDC hand-over in flight, so
            # the cabinet redirects straight into an authorization request.
            # That is an authentication-required state, not a broken one.
            await self._restart_from_entry_page(url)
        elif not self._is_signed_out_page(url):
            # Neither an authenticated cabinet nor the sign-in page: something
            # unexpected is on screen and guessing would only make it worse.
            await self._fail(
                "auth-unexpected-page",
                f"Opening {self.cabinet_url} landed on {url}, which is neither the "
                "authenticated cabinet nor the HSC sign-in page.\n"
                "Check the artifacts in data/debug/errors/ before re-running.",
            )

        logger.info("Starting ID.GOV.UA authentication")

        # Everything that can be decided without touching the browser is
        # decided here, before the journey opens ID.GOV.UA.
        secrets = self.config.secrets
        key_path = secrets.require_key_path()
        password = secrets.require_key_password()
        # Configuration, not a secret: the КНЕДП that issued the key comes from
        # flow.yaml. LoginPage is handed the value and never reads YAML itself.
        provider = self.config.flow.authentication.require_key_provider()

        await self.login.authenticate_with_master_key(
            key_path,
            password,
            provider,
            returns_to=self.site_host,
            challenge_timeout_ms=self.config.flow.timeouts.manual_challenge,
            callback_timeout_ms=self.config.flow.timeouts.authentication,
            key_load_timeout_ms=self.config.flow.timeouts.authentication,
            manual_provider=manual_provider,
            manual_password=manual_password,
        )

        await self._verify_cabinet()

    async def _restart_from_entry_page(self, url: str) -> None:
        """Recover from a cabinet URL that redirected into ID.GOV.UA.

        The stale authorization screen is deliberately *not* used: whatever
        form state a dead attempt left there belongs to a request we cannot
        vouch for. The browser is sent back to the HSC entry page so the normal
        journey starts from a known screen instead.

        Exactly once. If the entry page bounces back to ID.GOV.UA too, that is
        reported — re-driving it would be the retry loop this class exists to
        avoid.
        """
        entry = self.config.flow.base_url
        logger.info(
            "Opening the cabinet redirected to ID.GOV.UA (%s) — a previous "
            "authentication was interrupted",
            url,
        )
        logger.info("Restarting the authentication journey from %s", entry)
        await self.login.open_entry_page(entry)

        current = self.page.url
        if self._is_signed_out_page(current):
            return

        await self._fail(
            "auth-idgov-redirect-loop",
            f"Opening {self.cabinet_url} redirected to ID.GOV.UA, and going back "
            f"to {entry} landed on {current} instead of the HSC sign-in page.\n"
            "No second restart was attempted. The browser profile is most "
            "likely holding a half-finished ID.GOV.UA session: sign in once by "
            "hand in the persistent profile, or clear it, then re-run.",
        )

    async def _verify_cabinet(self) -> None:
        """Confirm the fresh session actually works, exactly once.

        Being bounced back to the sign-in page here means the login did not
        take. Trying again from inside this method is how a login loop starts,
        so it deliberately fails instead.
        """
        await self.queue.open()
        if await self.is_authenticated():
            logger.info("HSC authenticated session established")
            return

        await self._fail(
            "auth-verification-failed",
            "ID.GOV.UA authentication finished, but "
            f"{self.cabinet_url} still does not show an authenticated cabinet "
            f"(the browser is on {self.page.url}).\n"
            "No second login was attempted. Check the artifacts in "
            "data/debug/errors/, then sign in once by hand in the persistent "
            "profile to see what the site is asking for.",
        )

    async def _fail(self, label: str, message: str) -> NoReturn:
        """Save sanitized diagnostics, then raise. Never returns."""
        error = AuthenticationFailed(message)
        if self.diagnostics is not None:
            await self.diagnostics.capture_failure(self.page, label, error)
        raise error