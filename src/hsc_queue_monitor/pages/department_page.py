"""Service centre (територіальний сервісний центр) selection.

Identity is the service centre **ID** (``"3242"``). The visible label also
carries the address, which is treated as display text: it is reported for
diagnostics but never required to stay unchanged.
"""

from __future__ import annotations

import logging

from playwright.async_api import Locator

from ..models import (
    DepartmentAmbiguous,
    DepartmentAvailability,
    DepartmentNotFound,
    DepartmentUnavailable,
    identifies_service_center,
)
from .base_page import BasePage, build_locator

logger = logging.getLogger(__name__)


class DepartmentPage(BasePage):
    SEARCH = "department.search"
    DEPARTMENT_CARD = "department.department_card"
    DEPARTMENT_LIST_ITEM = "department.department_list_item"
    CONTINUE = "department.continue"

    # ------------------------------------------------------- arriving here ---

    async def wait_until_ready(self, *, timeout: int | None = None) -> None:
        """Block until the service-centre screen is actually on screen.

        Clicking «категорія А» returns long before HSC has swapped screens: it
        leaves the category buttons mounted under its loading spinner, so the
        click having returned says nothing about which screen is up.

        The condition is ``department.search`` resolved from the registry — the
        same entry :meth:`search_department` types into. Nothing here repeats
        the placeholder text: one selector, one definition.
        """
        await self.wait_for_screen(
            self.SEARCH,
            screen="service-centre screen",
            after="selecting category A",
            timeout=timeout,
            artifact="department-screen-timeout",
        )

    # ------------------------------------------------------------- search ----

    async def search_department(self, service_center_id: str) -> None:
        """Type an ID into the filter box and wait for the list to react."""
        spec = self.spec(self.SEARCH)
        # Explicitly clear first: the field keeps whatever a previous check
        # typed into it, and a stale filter silently hides the wanted centre.
        await self.fill(spec, "", step="department.search[clear]")
        await self.fill(spec, service_center_id, step=f"department.search[{service_center_id}]")
        await self._wait_for_results(service_center_id)

    async def _wait_for_results(self, service_center_id: str) -> None:
        """Poll until a matching button appears, or the timeout expires.

        No fixed sleep: this is the same visibility poll every other locator
        goes through. Coming back empty is not an error here — the caller
        decides what a missing centre means.
        """
        spec = self.spec(self.DEPARTMENT_CARD, service_center_id)
        locator = build_locator(self.page, spec)
        found = await self._wait_for_matches(
            locator, spec, spec.timeout or self.default_timeout
        )
        logger.debug("Search %r left %d candidate button(s) on screen", service_center_id,
                     len(found))

    # ------------------------------------------------------------ matching ---

    async def _candidates(
        self, service_center_id: str, *, wait: bool = True
    ) -> list[tuple[Locator, str]]:
        """Visible buttons whose label contains the term, with their text.

        ``wait=False`` reports what is on screen right now — used for
        diagnostics after the waiting lookup has already come back empty.
        """
        spec = self.spec(self.DEPARTMENT_CARD, service_center_id)
        locator = build_locator(self.page, spec)
        indices = (
            await self._wait_for_matches(locator, spec, spec.timeout or self.default_timeout)
            if wait
            else await self._matching_indices(locator, spec)
        )

        out: list[tuple[Locator, str]] = []
        for index in indices:
            match = locator.nth(index)
            try:
                text = " ".join((await match.inner_text()).split())
            except Exception:  # pragma: no cover - detached during read
                continue
            out.append((match, text))
        return out

    async def candidate_labels(self, service_center_id: str) -> list[str]:
        """Labels the search term left on screen. Diagnostic only; never waits."""
        return [text for _locator, text in await self._candidates(service_center_id, wait=False)]

    async def find_department_button(self, service_center_id: str) -> Locator:
        """The one button that identifies ``service_center_id``.

        A substring hit is not enough: ``3242`` must not resolve to ``13242``.
        Zero and several matches are both hard errors — ``.first`` is never used
        to paper over an ambiguous screen.
        """
        candidates = await self._candidates(service_center_id)
        matched = [
            (locator, text)
            for locator, text in candidates
            if identifies_service_center(text, service_center_id)
        ]

        if not matched:
            raise DepartmentNotFound(service_center_id, [text for _, text in candidates])
        if len(matched) > 1:
            raise DepartmentAmbiguous(service_center_id, [text for _, text in matched])

        locator, text = matched[0]
        logger.info("Service centre %s -> %s", service_center_id, text)
        return locator

    # -------------------------------------------------------- availability ---

    async def get_department_availability(
        self, service_center_id: str, *, name: str = ""
    ) -> DepartmentAvailability:
        """Read the centre's button. Never clicks anything.

        A centre that is not on screen is reported as ``found=False`` rather
        than raised, so a caller can print the whole picture in one go.
        """
        try:
            button = await self.find_department_button(service_center_id)
        except DepartmentNotFound:
            logger.info("Service centre %s is not on this screen", service_center_id)
            return DepartmentAvailability.missing(
                service_center_id=service_center_id, name=name
            )

        full_text = " ".join((await button.inner_text()).split())
        disabled = await button.is_disabled()
        return DepartmentAvailability.from_button(
            service_center_id=service_center_id,
            name=name or full_text,
            full_text=full_text,
            disabled=disabled,
        )

    # -------------------------------------------------------------- select ---

    async def select_department(self, service_center_id: str) -> None:
        """Click the centre's button, but only when it is enabled and unique."""
        button = await self.find_department_button(service_center_id)
        full_text = " ".join((await button.inner_text()).split())

        if await button.is_disabled():
            raise DepartmentUnavailable(service_center_id, full_text)

        step = f"department.select[{service_center_id}]"

        async def _do() -> None:
            logger.info("Clicking service centre %s (%s)", service_center_id, full_text)
            await button.click()

        await self._instrumented(step, self.DEPARTMENT_CARD, _do)
        await self.wait_stable()

    # --------------------------------------------------------- diagnostics ---

    async def list_visible_departments(self) -> list[str]:
        """Names of the service centres currently on screen, best effort.

        Returns an empty list while ``department.department_list_item`` is still
        a TODO — this is a diagnostic aid, not something the flow depends on.
        """
        spec = self.optional_spec(self.DEPARTMENT_LIST_ITEM)
        if spec is None:
            return []
        names = await self.texts(spec, required=False)
        logger.info("Found %d service centre(s) on screen", len(names))
        return names

    async def continue_to_calendar(self) -> None:
        await self.click(self.CONTINUE, step="department.continue")