"""The calendar step: which days are free, and picking one.

Two months are rendered side by side, and their day numbers overlap — there is a
"1" in each of them. A day is therefore only ever addressed *inside* its month
container, whose caption («Серпень 2026») says which month that is. Nothing here
assumes which container comes first, or how many there are.

All UI-text parsing lives in :mod:`.ui_text`, so the month names have exactly
one home.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from playwright.async_api import Locator

from ..models import AvailableDate, AvailableSlot, FlowError
from .base_page import BasePage, build_locator
from .ui_text import (
    collapse,
    parse_date_text,
    parse_day_number,
    parse_month_caption,
    parse_times,
)

logger = logging.getLogger(__name__)

_ELEMENT_JS = """
(el) => ({
  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
  aria_label: el.getAttribute('aria-label'),
  title: el.getAttribute('title'),
  data_date: el.getAttribute('data-date') || el.getAttribute('data-day')
          || el.getAttribute('datetime') || el.getAttribute('data-value'),
  data_time: el.getAttribute('data-time') || el.getAttribute('data-slot'),
})
"""


class CalendarPage(BasePage):
    LOADED = "calendar.loaded_marker"
    READY = "calendar.ready_marker"
    MONTH = "calendar.month"
    DAY = "calendar.day"
    AVAILABLE_DAY = "calendar.available_day"
    AVAILABLE_SLOT = "calendar.available_slot"
    NO_SLOTS = "calendar.no_slots"

    # ------------------------------------------------------------ loading ---

    async def wait_until_ready(self, *, timeout: int | None = None) -> None:
        """Block until the date step is really up, after selecting a centre.

        Semantic, not structural: ``calendar.ready_marker`` is the «Оберіть
        дату» heading of the wizard step, so this says "the right step is on
        screen", which a container class name would not.
        """
        await self.wait_for_screen(
            self.READY,
            screen="calendar screen",
            after="selecting the service centre",
            timeout=timeout,
            artifact="calendar-screen-timeout",
        )

    async def wait_until_loaded(self, timeout: int | None = None) -> None:
        """Wait for the calendar to render.

        Falls back to a generic settle when ``calendar.loaded_marker`` has not
        been configured.
        """
        marker = self.optional_spec(self.LOADED)
        if marker is None:
            logger.debug("%s not configured; falling back to network settle", self.LOADED)
            await self.wait_stable()
            return
        await self.resolve(marker, timeout=timeout)

    # ------------------------------------------------------------- months ---

    async def _month_containers(self) -> list[tuple[Locator, int, int, str]]:
        """Every month on screen as ``(container, year, month, caption)``.

        Read from the containers themselves rather than counted off: the site
        shows two months today and is free to show one or three tomorrow.
        """
        found: list[tuple[Locator, int, int, str]] = []
        for container in await self.resolve_many(self.MONTH, required=False):
            caption = collapse(await self._safe_text(container))
            parsed = parse_month_caption(caption)
            if parsed is None:
                logger.warning(
                    "A calendar month container has no recognisable «Місяць РІК» "
                    "caption; its days are not dated and are skipped."
                )
                continue
            year, month = parsed
            found.append((container, year, month, caption))
        return found

    async def _days_in(self, container: Locator) -> list[tuple[Locator, int, bool, str]]:
        """``(button, day number, enabled, label)`` for each day in one month."""
        spec = self.spec(self.DAY)
        days = build_locator(container, spec)
        out: list[tuple[Locator, int, bool, str]] = []
        for index in range(await days.count()):
            cell = days.nth(index)
            try:
                if not await cell.is_visible():
                    continue
                label = collapse(await cell.inner_text())
                enabled = await cell.is_enabled()
            except Exception:  # pragma: no cover - re-render mid-read
                continue
            number = parse_day_number(label)
            if number is None:
                # Month arrows, weekday headers, anything that is not a day.
                continue
            out.append((cell, number, enabled, label))
        return out

    @staticmethod
    async def _safe_text(locator: Locator) -> str:
        try:
            return str(await locator.inner_text())
        except Exception:  # pragma: no cover - detached mid-read
            return ""

    # -------------------------------------------------------------- dates ---

    async def available_dates(self) -> list[AvailableDate]:
        """Every enabled day button, dated by the month it sits in.

        Enabled is the whole test: the site renders unavailable days as disabled
        buttons, and this reads that state rather than inferring anything from
        styling. Days that cannot be dated are skipped loudly, never guessed at.
        """
        dates: list[AvailableDate] = []
        seen: set[date] = set()

        for container, year, month, caption in await self._month_containers():
            for _cell, number, enabled, label in await self._days_in(container):
                if not enabled:
                    continue
                day = self._compose(year, month, number, caption)
                if day is None or day in seen:
                    continue
                seen.add(day)
                dates.append(AvailableDate(date=day, label=label))

        dates.sort(key=lambda found: found.date)
        logger.info(
            "Calendar offers %d available date(s): %s",
            len(dates),
            ", ".join(found.date.isoformat() for found in dates) or "(none)",
        )
        return dates

    @staticmethod
    def _compose(year: int, month: int, day: int, caption: str) -> date | None:
        composed = date(year, month, 1)
        try:
            return composed.replace(day=day)
        except ValueError:
            logger.warning("Day %d is not a real date in %r; skipped.", day, caption)
            return None

    async def select_date(self, wanted: date) -> None:
        """Click the day button for *wanted*, inside its own month container.

        Never a page-wide lookup for the day number: with two months on screen
        «1» matches twice, and picking either of them by position is how a
        scanner ends up reading the wrong month's times.
        """
        containers = await self._month_containers()
        matches = [
            (container, caption)
            for container, year, month, caption in containers
            if (year, month) == (wanted.year, wanted.month)
        ]

        if not matches:
            shown = ", ".join(caption for _c, _y, _m, caption in containers) or "(none)"
            raise FlowError(
                f"{wanted.isoformat()} is not in any month the calendar is "
                f"showing.\nOn screen: {shown}\n"
                "Nothing was clicked."
            )
        if len(matches) > 1:
            raise FlowError(
                f"The calendar shows {len(matches)} containers for "
                f"{wanted.strftime('%Y-%m')}, so it is not safe to pick one.\n"
                "Nothing was clicked."
            )

        container, caption = matches[0]
        await self._click_day(container, caption, wanted)

    async def _click_day(self, container: Locator, caption: str, wanted: date) -> None:
        days = await self._days_in(container)
        candidates = [entry for entry in days if entry[1] == wanted.day]

        if not candidates:
            offered = ", ".join(str(n) for _c, n, enabled, _l in days if enabled) or "(none)"
            raise FlowError(
                f"{caption} has no day {wanted.day} button.\n"
                f"Days it offers: {offered}\nNothing was clicked."
            )
        if len(candidates) > 1:
            raise FlowError(
                f"{caption} has {len(candidates)} buttons for day {wanted.day}, so "
                "it is not safe to pick one.\nNothing was clicked."
            )

        cell, _number, enabled, label = candidates[0]
        if not enabled:
            raise FlowError(
                f"{wanted.isoformat()} is disabled in {caption}, so it has no "
                "free times.\nA disabled day is never force-clicked."
            )

        async def _do() -> None:
            logger.info("Selecting date %s (%s, «%s»)", wanted.isoformat(), caption, label)
            await cell.click()

        await self._instrumented(f"calendar.select_date[{wanted.isoformat()}]",
                                 self.DAY, _do)
        await self.wait_stable()

    # ------------------------------------------------------------ reading ---

    async def _read_element(self, locator: Locator) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await locator.evaluate(_ELEMENT_JS)
        except Exception:  # pragma: no cover - element detached mid-read
            return {}
        return data

    async def get_available_dates(self) -> list[date]:
        """Dates whose day cell is marked as having capacity."""
        dates: list[date] = []
        for locator in await self.resolve_many(self.AVAILABLE_DAY, required=False):
            info = await self._read_element(locator)
            parsed = (
                parse_date_text(info.get("data_date"))
                or parse_date_text(info.get("aria_label"))
                or parse_date_text(info.get("title"))
                or parse_date_text(info.get("text"))
            )
            if parsed is None:
                logger.debug("Could not parse a date from day cell: %r", info)
                continue
            if parsed not in dates:
                dates.append(parsed)
        return sorted(dates)

    async def get_available_slots(
        self, service_center: str, *, on_date: date | None = None
    ) -> list[AvailableSlot]:
        """Bookable times currently shown, as :class:`AvailableSlot` records."""
        slots: list[AvailableSlot] = []
        for locator in await self.resolve_many(self.AVAILABLE_SLOT, required=False):
            info = await self._read_element(locator)
            slot_date = (
                parse_date_text(info.get("data_date"))
                or parse_date_text(info.get("aria_label"))
                or parse_date_text(info.get("text"))
                or on_date
            )
            times = parse_times(info.get("data_time")) or parse_times(
                info.get("text")
            ) or parse_times(info.get("aria_label"))

            if not times:
                logger.debug("Slot element without a recognisable time: %r", info)
                continue

            for value in times:
                slots.append(
                    AvailableSlot(
                        service_center=service_center,
                        time=value,
                        date=slot_date,
                        metadata={
                            k: v
                            for k, v in info.items()
                            if k in {"text", "aria_label"} and v
                        },
                    )
                )
        return slots

    async def has_available_slots(self) -> bool:
        """True when the screen shows capacity.

        An explicit "no slots" message wins over element counting, because a
        stale slot node can linger in the DOM after a re-render.
        """
        no_slots = self.optional_spec(self.NO_SLOTS)
        if no_slots is not None and await self.is_present(no_slots, timeout=2_000):
            logger.info("Calendar reports no available slots.")
            return False

        slot_count = await self.count_visible(self.AVAILABLE_SLOT)
        if slot_count:
            return True

        day_count = await self.count_visible(self.AVAILABLE_DAY)
        return day_count > 0
