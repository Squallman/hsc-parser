"""Polling loop and change detection.

One loop per browser session, sequential per-department requests, jittered
interval, and notifications only for things that are actually new.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import date as date_cls
from typing import Any

from .api import (
    ApiError,
    AuthenticationRequiredError,
    EndpointNotDiscoveredError,
    HscApiClient,
    RateLimitedError,
)
from .config import Settings
from .models import AvailableDate, AvailableSlot, Department, MonitorState
from .notifier import NotificationEvent, Notifier

logger = logging.getLogger(__name__)


class QueueMonitor:
    """Periodically checks availability and notifies about new findings."""

    def __init__(
        self,
        api: HscApiClient,
        notifier: Notifier,
        settings: Settings,
        state: MonitorState,
        *,
        stop_event: asyncio.Event | None = None,
        reauthenticate: Callable[[], Awaitable[bool]] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        today: Callable[[], date_cls] = date_cls.today,
    ) -> None:
        self.api = api
        self.notifier = notifier
        self.settings = settings
        self.state = state
        self.stop_event = stop_event or asyncio.Event()
        self._reauthenticate = reauthenticate
        self._sleep = sleep
        self._today = today
        self.departments: dict[int, Department] = {}
        self.cycles = 0

    # -- setup --------------------------------------------------------------
    async def resolve_departments(self) -> list[int]:
        """Fetch the department catalogue and apply the configured filter."""
        departments = await self.api.get_departments(self.settings.service_id)
        self.departments = {d.id: d for d in departments if d.id is not None}

        configured = self.settings.department_ids
        if not configured:
            selected = sorted(self.departments)
            logger.info("Watching all %d department(s) for the configured service", len(selected))
            return selected

        selected = []
        for dep_id in configured:
            if dep_id not in self.departments:
                logger.warning(
                    "Department id %s is not offered for serviceId=%s; watching it anyway",
                    dep_id,
                    self.settings.service_id,
                )
            selected.append(dep_id)
        logger.info("Watching department(s): %s", ", ".join(str(i) for i in selected))
        return selected

    # -- one cycle ----------------------------------------------------------
    def _in_range(self, value: str) -> bool:
        if self.settings.date_from and value < self.settings.date_from:
            return False
        return not (self.settings.date_to and value > self.settings.date_to)

    async def check_department(self, department_id: int) -> list[NotificationEvent]:
        """Check one department, returning the events that should be emitted."""
        department = self.departments.get(department_id)
        service_id = self.settings.service_id

        dates: list[AvailableDate] = await self.api.get_available_dates(
            department_id,
            service_id=service_id,
            date_from=self.settings.date_from,
            date_to=self.settings.date_to,
        )
        dates = [d for d in dates if self._in_range(d.date)]

        events: list[NotificationEvent] = []
        for item in self.state.newly_available_dates(dates):
            events.append(
                NotificationEvent.from_date(item, service_id=service_id, department=department)
            )
        self.state.mark_dates_seen(dates)

        for item in [d for d in dates if d.available]:
            if self.settings.request_delay_seconds:
                await self._sleep(self.settings.request_delay_seconds)
            slots: list[AvailableSlot] = await self.api.get_available_slots(
                department_id, item.date, service_id=service_id
            )
            fresh = self.state.new_slots(slots)
            for slot in fresh:
                events.append(
                    NotificationEvent.from_slot(slot, service_id=service_id, department=department)
                )
            self.state.mark_slots_seen(slots)

        return events

    async def check_once(self, department_ids: list[int]) -> list[NotificationEvent]:
        """Run a single polling cycle over every watched department."""
        events: list[NotificationEvent] = []
        for index, department_id in enumerate(department_ids):
            if self.stop_event.is_set():
                break
            if index and self.settings.request_delay_seconds:
                await self._sleep(self.settings.request_delay_seconds)
            try:
                events.extend(await self.check_department(department_id))
            except EndpointNotDiscoveredError:
                raise
            except AuthenticationRequiredError as exc:
                logger.warning("Session lost while checking department %s: %s", department_id, exc)
                if not await self._try_reauthenticate():
                    raise
            except RateLimitedError as exc:
                logger.warning("Rate limited on department %s: %s", department_id, exc)
            except ApiError as exc:
                logger.warning("Department %s check failed: %s", department_id, exc)

        self.state.touch()
        removed = self.state.prune(self._today())
        if removed:
            logger.debug("Pruned %d stale state entr(ies)", removed)
        self._save_state()
        self.cycles += 1

        if events:
            logger.info("Found %d new availability event(s)", len(events))
        else:
            logger.info("No new availability")
        return events

    # -- loop ---------------------------------------------------------------
    async def run(self) -> None:
        """Poll until stopped, Ctrl+C'd, or an endpoint turns out to be missing."""
        try:
            department_ids = await self.resolve_departments()
        except EndpointNotDiscoveredError as exc:
            logger.error("%s", exc)
            return
        except ApiError as exc:
            logger.error("Cannot load departments: %s", exc)
            return

        if not department_ids:
            logger.error("No departments to watch; set HSC_DEPARTMENT_IDS or check the service id")
            return

        logger.info(
            "Monitoring started: interval %.0fs (+/-%.0fs jitter)",
            self.settings.effective_poll_interval,
            self.settings.poll_jitter_seconds,
        )
        while not self.stop_event.is_set():
            try:
                events = await self.check_once(department_ids)
            except EndpointNotDiscoveredError as exc:
                logger.error("%s", exc)
                logger.error("Monitoring stopped: availability endpoints are not implemented yet.")
                return
            except AuthenticationRequiredError as exc:
                logger.error("Authentication required, stopping monitor: %s", exc)
                return

            for event in events:
                await self.notifier.notify(event)

            if self.stop_event.is_set():
                break
            await self._wait_next_cycle()

        logger.info("Monitoring loop finished after %d cycle(s)", self.cycles)

    def next_delay(self) -> float:
        """Interval clamped to the safe minimum, plus symmetric random jitter."""
        base = self.settings.effective_poll_interval
        jitter = self.settings.poll_jitter_seconds
        delay = base + random.uniform(-jitter, jitter) if jitter else base
        return max(delay, self.settings.min_poll_interval_seconds)

    async def _wait_next_cycle(self) -> None:
        delay = self.next_delay()
        logger.debug("Sleeping %.1fs until the next check", delay)
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except TimeoutError:
            return

    async def _try_reauthenticate(self) -> bool:
        if self._reauthenticate is None:
            return False
        logger.info("Attempting to recover the browser session…")
        try:
            return await self._reauthenticate()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Re-authentication failed: %s", exc)
            return False

    def _save_state(self) -> None:
        try:
            self.state.save(self.settings.state_file)
        except OSError as exc:
            logger.warning("Could not persist state to %s: %s", self.settings.state_file, exc)

    def shutdown(self) -> None:
        """Request the loop to stop and flush state (safe to call from a signal)."""
        self.stop_event.set()
        self._save_state()
