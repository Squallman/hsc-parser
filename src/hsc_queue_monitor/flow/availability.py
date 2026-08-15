"""Availability scanning: what is free, for 1–5 configured service centres.

    centre → every enabled date → every enabled time on that date

**This module reads. It does not book, and it must not learn how.** The scan
walks the wizard as far as the «Час» step, reads the times it offers and goes
back. It never selects a time, never presses on towards «Контакти», never fills
a contact detail in and never submits anything. That boundary is the reason the
project can run unattended at all, and it is enforced by tests — including one
that reads this module looking for the calls it is not allowed to contain.

Navigation is deliberately frugal. Authentication happens once per scan, through
the one :class:`~..flow.auth.AuthManager` path, and is not repeated between
centres; the wizard is re-walked only when the browser is not already where the
next step needs it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ..models import (
    CentreAvailability,
    DateAvailability,
    HscMonitorError,
    SelectorNotConfigured,
    ServiceCenter,
)
from ..pages.department_page import DepartmentPage
from .engine import FlowEngine
from .steps import FlowContext

logger = logging.getLogger(__name__)

#: The site's own "one step back" control. Optional: when it is not configured,
#: every return trip replays the wizard instead, which is slower but correct.
WIZARD_BACK = "wizard.back"

#: The chain that reaches the service-centre screen. Read from flow.yaml — no
#: step order and no service-centre ID is written into this module.
DEPARTMENT_SCREEN = DepartmentPage.SEARCH


class AvailabilityScanner:
    """Walks the wizard for each centre and reports what it found.

    One instance per run. It owns no state about the site beyond "where the
    browser currently is", which it always re-checks rather than assumes.
    """

    def __init__(self, ctx: FlowContext, *, start_url: str | None = None) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.start_url = start_url
        self.engine = FlowEngine(ctx, auto=True, pause_after_step=False)

    # ----------------------------------------------------------- scanning ---

    async def scan(self, centers: Sequence[ServiceCenter]) -> list[CentreAvailability]:
        """Scan every centre in order, and keep going if one of them fails.

        A centre that cannot be read is one bad result among good ones, not the
        end of the run: the point of a scan is the picture it produces.
        """
        # Once, for the whole scan. Idempotent, so a live session costs one
        # marker check — and an expired one is recovered here rather than
        # rediscovered as a puzzling failure three screens in.
        await self.ctx.auth.ensure_authenticated()

        results: list[CentreAvailability] = []
        for number, center in enumerate(centers, start=1):
            logger.info(
                "Scanning service centre %s (%s) — %d/%d",
                center.id, center.name, number, len(centers),
            )
            results.append(await self.scan_centre(center))
        return results

    async def scan_centre(self, center: ServiceCenter) -> CentreAvailability:
        """One centre, end to end.

        A site failure becomes a result rather than an exception, so one bad
        centre does not cost the others. The single exception is a selector that
        has not been configured, which is not about this centre at all.
        """
        try:
            card_state = await self._open_centre(center)
            if card_state is not None:
                return card_state

            await self.ctx.calendar.wait_until_ready()
            dates = await self.ctx.calendar.available_dates()
            days = await self._scan_dates(center, [found.date for found in dates])
            return CentreAvailability(
                centre_id=center.id,
                centre_name=center.name,
                dates=tuple(days),
            )
        except SelectorNotConfigured:
            # Not this centre's problem: an unconfigured selector fails the same
            # way for every one of them, so it stops the run and says how to fix
            # it instead of being copied into five identical results.
            raise
        except HscMonitorError as exc:
            await self._capture(center, None, exc)
            return CentreAvailability(
                centre_id=center.id, centre_name=center.name, error=str(exc)
            )
        finally:
            await self._leave_centre()

    async def _open_centre(self, center: ServiceCenter) -> CentreAvailability | None:
        """Select the centre, or explain why it was not opened.

        Returns a finished result when there is nothing to open — and ``None``
        when the calendar is now on its way. The card state is a cheap filter,
        never the answer: an enabled card only earns the centre a click.
        """
        await self._reach_service_centre_screen()
        await self.ctx.department.search_department(center.search_term)
        card = await self.ctx.department.get_department_availability(
            center.id, name=center.name
        )

        if not card.found:
            logger.info("%s is not on the service-centre screen", center.id)
            return CentreAvailability.missing(
                centre_id=center.id, centre_name=center.name
            )
        if not card.available:
            logger.info(
                "%s is on screen but its card is disabled; it is not opened", center.id
            )
            return CentreAvailability.unavailable(
                centre_id=center.id, centre_name=center.name
            )

        await self.ctx.department.select_department(center.id)
        return None

    async def _scan_dates(
        self, center: ServiceCenter, dates: Sequence[date]
    ) -> list[DateAvailability]:
        """Read every date's times, returning to the calendar between them."""
        collected: list[DateAvailability] = []
        for number, day in enumerate(dates, start=1):
            logger.info(
                "Reading times for %s on %s (%d/%d)",
                center.id, day.isoformat(), number, len(dates),
            )
            found = await self._scan_date(center, day)
            collected.append(found)

            try:
                await self._return_to_calendar(center)
            except HscMonitorError as exc:
                # The dates already read are kept: partial evidence beats none.
                await self._capture(center, day, exc)
                collected[-1] = replace(
                    found, error=f"could not get back to the calendar: {exc}"
                )
                break
        return collected

    async def _scan_date(self, center: ServiceCenter, day: date) -> DateAvailability:
        """One date's free times. An empty list is a result, not a failure."""
        try:
            await self.ctx.calendar.select_date(day)
            await self.ctx.time.wait_until_ready()
            slots = await self.ctx.time.available_slots()
        except HscMonitorError as exc:
            await self._capture(center, day, exc)
            return DateAvailability(date=day, error=str(exc))
        return DateAvailability(date=day, slots=tuple(slots))

    # --------------------------------------------------------- navigation ---

    async def _reach_service_centre_screen(self) -> None:
        """Be on the service-centre screen, replaying the wizard only if needed."""
        if await self.ctx.department.is_present(DepartmentPage.SEARCH, timeout=0):
            logger.info("Service-centre screen is already open; reusing it")
            return
        await self._replay_wizard()

    async def _replay_wizard(self) -> None:
        """Walk the configured chain from the cabinet back to the centres.

        ``prepare`` opens the start URL through the authentication guard, so an
        expired session is restored here — and a live one is not disturbed.
        """
        logger.info("Opening the wizard from the cabinet")
        await self.engine.prepare(
            self.config.flow.plan_for(DEPARTMENT_SCREEN).prerequisites,
            start_url=self.start_url or self.config.flow.start_url_for(DEPARTMENT_SCREEN),
            announce=False,
        )
        await self.ctx.department.wait_until_ready()

    async def _return_to_calendar(self, center: ServiceCenter) -> None:
        """Back exactly one step, to the calendar this date came from.

        The site's own Back is preferred — it keeps the chosen centre, so the
        calendar comes back as it was. When it is not configured, or it does not
        land where it should, the wizard is replayed for this centre instead.
        Either way the calendar is *waited for* before anything else happens.
        """
        if await self._click_back():
            try:
                await self.ctx.calendar.wait_until_ready()
                return
            except HscMonitorError as exc:
                logger.warning(
                    "Back did not return to the calendar (%s); replaying the wizard",
                    exc,
                )

        await self._replay_to_calendar(center)

    async def _replay_to_calendar(self, center: ServiceCenter) -> None:
        await self._replay_wizard()
        await self.ctx.department.search_department(center.search_term)
        await self.ctx.department.select_department(center.id)
        await self.ctx.calendar.wait_until_ready()

    async def _leave_centre(self) -> None:
        """Get back to the service-centre screen for the next centre.

        Best effort by design: if it does not work, the next centre replays the
        wizard from the cabinet, which always works. Nothing here is allowed to
        turn a finished centre's results into a failure.
        """
        try:
            if await self.ctx.department.is_present(DepartmentPage.SEARCH, timeout=0):
                return
            if await self._click_back():
                await self.ctx.department.wait_until_ready()
        except HscMonitorError as exc:
            # Never from a `finally`: this must not replace the failure that is
            # already on its way up with a navigation detail.
            logger.info(
                "Could not step back to the service-centre screen (%s); the next "
                "centre will start from the cabinet",
                exc,
            )

    async def _click_back(self) -> bool:
        """Press the site's Back control. False when there is none configured."""
        back = self.ctx.calendar.optional_spec(WIZARD_BACK)
        if back is None:
            logger.debug("%s is not configured; navigating by replay", WIZARD_BACK)
            return False
        try:
            await self.ctx.calendar.click(back, step=WIZARD_BACK)
        except HscMonitorError as exc:
            logger.warning("The Back control could not be clicked (%s)", exc)
            return False
        return True

    # -------------------------------------------------------- diagnostics ---

    async def _capture(
        self, center: ServiceCenter, day: date | None, error: BaseException
    ) -> None:
        """Save the screen, labelled with the centre and date being scanned.

        Only for the unexpected. "No free dates" and "no free times" are normal
        answers and never come through here.
        """
        where = f"{center.id}" + (f" on {day.isoformat()}" if day else "")
        logger.warning("Scanning %s failed: %s", where, error)
        if self.ctx.diagnostics is None:
            return
        label = f"availability-{center.id}" + (f"-{day.isoformat()}" if day else "")
        await self.ctx.diagnostics.capture_snapshot(self.ctx.page, label)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

#: Why a centre has nothing on offer, said in words rather than left to a blank.
_STATUS_LINES = {
    "not-found": "not on the service-centre screen",
    "centre-unavailable": "unavailable — the centre's button is disabled",
    "no-dates": "no available dates",
    "no-times": "available dates, but no free times on any of them",
}


def render_availability(results: Sequence[CentreAvailability]) -> str:
    """The scan as text: centre, date, times — and why, when there are none."""
    lines = ["", "AVAILABILITY", ""]

    for result in results:
        lines.append(result.centre_name or result.centre_id)
        if result.error:
            lines.append(f"  scan failed: {_first_line(result.error)}")
            lines.append("")
            continue

        note = _STATUS_LINES.get(result.status)
        if note is not None and result.status != "no-times":
            lines.append(f"  {note}")
            lines.append("")
            continue

        for day in result.dates:
            lines.append(f"  {day.date.isoformat()}")
            if day.error:
                lines.append(f"    could not be read: {_first_line(day.error)}")
            elif not day.slots:
                lines.append("    no available times")
            else:
                lines.extend(f"    {slot.display}" for slot in day.slots)
            lines.append("")

    bookable = [result for result in results if result.bookable]
    lines.append(
        f"{len(bookable)} of {len(results)} centre(s) have at least one free time."
        if results
        else "No centres were scanned."
    )
    return "\n".join(lines)


def _first_line(message: str) -> str:
    return message.splitlines()[0] if message else ""
