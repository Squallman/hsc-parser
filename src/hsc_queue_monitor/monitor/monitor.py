"""The polling loop.

One browser context, one service centre at a time, never faster than the
enforced minimum interval. This is a monitor, not a load generator.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime

from ..config import AppConfig, enabled_service_centers
from ..flow.engine import FlowEngine
from ..flow.steps import FlowContext, get_step
from ..models import AvailableSlot, HscMonitorError, ServiceCenter
from ..notification.base import Notification, Notifier
from .state import StateStore

logger = logging.getLogger(__name__)

#: Steps replayed for every service centre, after returning to the queue page.
PER_CENTER_STEPS = (
    "start_registration",
    "practical_exam",
    "category_a",
    "select_department",
    "continue_to_calendar",
    "read_slots",
)


class Monitor:
    def __init__(
        self,
        ctx: FlowContext,
        notifiers: list[Notifier],
        state: StateStore,
        *,
        dry_run: bool = False,
    ) -> None:
        self.ctx = ctx
        self.config: AppConfig = ctx.config
        self.notifiers = notifiers
        self.state = state
        self.dry_run = dry_run
        self.engine = FlowEngine(ctx, auto=True, pause_after_step=False)

    # ------------------------------------------------------------ lifecycle -

    async def ensure_logged_in(self) -> None:
        """Open the cabinet with a session that works, recovering it if needed."""
        if not self.config.flow.login_enabled:
            logger.info("Login is disabled in flow.yaml; assuming the profile is signed in.")
            await get_step("open_queue").action(self.ctx)
            return
        await self.ctx.auth.ensure_authenticated()

    async def run(self, *, once: bool = False) -> None:
        centers = enabled_service_centers(self.config.service_centers)
        logger.info(
            "Monitoring %d service centre(s): %s",
            len(centers),
            ", ".join(c.name for c in centers),
        )
        await self.ensure_logged_in()

        while True:
            try:
                await self.check_all(centers)
            except HscMonitorError as exc:
                logger.error("Cycle failed: %s", exc)
            except Exception:
                logger.exception("Unexpected error during monitoring cycle")

            if once:
                return

            delay = self._next_delay()
            logger.info("Sleeping %.1fs until the next cycle", delay)
            await asyncio.sleep(delay)

    def _next_delay(self) -> float:
        pacing = self.config.app.browser_monitor
        jitter = random.uniform(-pacing.poll_jitter_seconds, pacing.poll_jitter_seconds)
        return max(float(pacing.poll_interval_seconds + jitter), 30.0)

    # ---------------------------------------------------------- one cycle ---

    async def check_all(self, centers: list[ServiceCenter]) -> None:
        for center in centers:
            try:
                slots = await self.check_center(center)
            except HscMonitorError as exc:
                logger.error("Could not check %s: %s", center.name, exc)
                continue
            await self.report(center, slots)

    async def check_center(self, center: ServiceCenter) -> list[AvailableSlot]:
        """Walk one centre's booking path and read whatever is on the calendar."""
        logger.info("Checking %s", center.name)
        self.ctx.current_service_center = center
        self.ctx.last_slots = []

        # Returning to the cabinet is the reliable way back to a known state,
        # and it is also where an expired session shows itself — so the guard
        # runs every cycle, not just once at start-up.
        await self.ensure_logged_in()

        for name in PER_CENTER_STEPS:
            step = get_step(name)
            logger.debug("Step: %s", step.name)
            await step.action(self.ctx)

        return list(self.ctx.last_slots)

    async def report(self, center: ServiceCenter, slots: list[AvailableSlot]) -> None:
        if not slots:
            logger.info("%s: nothing available", center.name)
            return

        now = datetime.now(UTC)
        fresh = self.state.select_new(slots, now=now)
        if not fresh:
            logger.info(
                "%s: %d slot(s) available, all already reported", center.name, len(slots)
            )
            return

        logger.info("%s: %d NEW slot(s)", center.name, len(fresh))
        notification = Notification(service_center=center.name, slots=tuple(fresh))
        for notifier in self.notifiers:
            try:
                await notifier.send(notification)
            except Exception:
                logger.exception("Notifier %s failed", notifier.name)

        if self.dry_run:
            logger.info("Dry run: state not updated, nothing was sent to Telegram.")
            return

        self.state.mark_notified(fresh, now=now)
        self.state.prune(now=now)
        self.state.save()
