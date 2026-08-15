"""The «Час» step: which times a chosen date offers.

**This page object is read-only, deliberately and permanently.** It can wait for
the step and report what it sees, and that is the entire API — there is no
method here that selects a time, confirms one, or moves on to «Контакти». The
scanner this project is built around answers "is anything free?"; booking is a
person's decision, made in the browser, and nothing in this codebase is allowed
to make it. A test asserts that no such method appears.
"""

from __future__ import annotations

import logging

from ..models import TimeSlot
from .base_page import BasePage, build_locator
from .ui_text import collapse, parse_slot_time

logger = logging.getLogger(__name__)


class TimePage(BasePage):
    READY = "time.ready_marker"
    SLOT = "time.slot"

    async def wait_until_ready(self, *, timeout: int | None = None) -> None:
        """Block until the time step is really up, after picking a date."""
        await self.wait_for_screen(
            self.READY,
            screen="time screen",
            after="selecting a date",
            timeout=timeout,
            artifact="time-screen-timeout",
        )

    async def available_slots(self) -> list[TimeSlot]:
        """Every selectable time on screen, in the order the site lists them.

        Two filters, both read off the page rather than guessed:

        * the control's whole label is a time — a button saying «Записатись на
          10:40» is a booking control, not a slot, and must not be treated as
          one on a screen nothing here is allowed to act on;
        * the control is enabled — the site renders a taken time as a disabled
          button, so "disabled" is the site's own answer to "is it free?".

        Returning an empty list is a normal observation, not an error: a date
        can be open and every time on it taken.
        """
        spec = self.spec(self.SLOT)
        controls = build_locator(self.page, spec)

        slots: list[TimeSlot] = []
        seen: set[str] = set()
        skipped_disabled = 0

        for index in range(await controls.count()):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                label = collapse(await control.inner_text())
                enabled = await control.is_enabled()
            except Exception:  # pragma: no cover - re-render mid-read
                continue

            moment = parse_slot_time(label)
            if moment is None:
                continue
            if not enabled:
                skipped_disabled += 1
                continue
            if label in seen:
                continue
            seen.add(label)
            slots.append(TimeSlot(time=moment, text=label))

        logger.info(
            "Time step offers %d available slot(s)%s: %s",
            len(slots),
            f" ({skipped_disabled} taken)" if skipped_disabled else "",
            ", ".join(slot.display for slot in slots) or "(none)",
        )
        return slots
