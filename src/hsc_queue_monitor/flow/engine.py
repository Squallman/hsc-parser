"""Step-by-step flow runner.

Interactive by default: it prints what it is about to do, which selector it will
use and the exact Playwright call, then waits for ENTER. That is what makes it
possible to determine unknown selectors one screen at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import AppConfig
from ..models import FlowError, HscMonitorError, LocatorSpec, SelectorNotConfigured
from .steps import FlowContext, Step, get_step

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """One successfully executed prerequisite."""

    key: str
    url: str


class FlowEngine:
    def __init__(
        self,
        ctx: FlowContext,
        *,
        auto: bool = False,
        pause_after_step: bool | None = None,
    ) -> None:
        self.ctx = ctx
        self.config: AppConfig = ctx.config
        self.auto = auto
        debug = self.config.flow.debug
        self.pause_after_step = (
            debug.pause_after_step if pause_after_step is None else pause_after_step
        )

    # ------------------------------------------------------------ planning --

    def plan(self, *, from_step: str | None = None, include_login: bool = True) -> list[Step]:
        """Build the ordered step list described by flow.yaml."""
        names: list[str] = ["open_queue"]
        if include_login and self.config.flow.login_enabled:
            names.append("login")
        names.extend(self.config.flow.queue_steps)

        steps = [get_step(name) for name in names]
        if from_step is None:
            return steps

        index = self._find_step(steps, from_step)
        skipped = [s.name for s in steps[:index]]
        if skipped:
            logger.info("Starting from %s (skipping: %s)", steps[index].name,
                        ", ".join(skipped))
        return steps[index:]

    @staticmethod
    def _find_step(steps: list[Step], wanted: str) -> int:
        """Accept a step name (``category_a``) or a selector key
        (``category.category_a``)."""
        wanted = wanted.strip()
        for index, step in enumerate(steps):
            if step.name == wanted or step.selector_key == wanted:
                return index
        # Fall back to the trailing component: `category.category_a` -> `category_a`
        tail = wanted.rsplit(".", 1)[-1]
        for index, step in enumerate(steps):
            if step.name == tail:
                return index
        raise HscMonitorError(
            f"--from {wanted!r} does not match any step in the configured flow. "
            f"Steps: {', '.join(s.name for s in steps)}"
        )

    # ----------------------------------------------------------- execution --

    async def run(self, *, from_step: str | None = None, include_login: bool = True) -> None:
        steps = self.plan(from_step=from_step, include_login=include_login)
        total = len(steps)

        # The guard runs before the first step rather than inside each one, so
        # `--from category.category_a` still starts from a live session.
        if include_login:
            await self.ctx.auth.ensure_authenticated()

        for number, step in enumerate(steps, start=1):
            if not await self._confirm(number, total, step):
                logger.info("Skipped %s", step.name)
                continue
            await self.execute(step, number)

        print(f"\nFlow finished ({total} step(s)).")

    async def execute(self, step: Step, number: int = 0) -> None:
        started = time.monotonic()
        url_before = self.ctx.page.url
        try:
            await step.action(self.ctx)
        except SelectorNotConfigured as exc:
            print(f"\nSTEP {number} ({step.name}) cannot run yet:\n{exc}\n")
            raise
        finally:
            duration = int((time.monotonic() - started) * 1000)
            logger.debug("%s took %dms", step.name, duration)

        await self._after_step(step, number, url_before)

    async def _after_step(self, step: Step, number: int, url_before: str) -> None:
        debug = self.config.flow.debug
        page = self.ctx.page

        print(f"  URL: {page.url}")
        if url_before != page.url:
            print(f"  (was: {url_before})")

        if self.ctx.diagnostics is not None:
            if debug.screenshots:
                # Diagnostics adds the sequence number itself.
                await self.ctx.diagnostics.screenshot(page, step.name)
            if debug.dump_elements:
                await self.ctx.diagnostics.dump_elements(page, f"elements-{step.name}")

        if self.pause_after_step and not self.auto:
            await prompt_async("  Press ENTER to continue... ")

    # --------------------------------------------------------- preparation --

    async def goto(self, url: str) -> None:
        """Navigate and settle, using the configured navigation timeout."""
        logger.info("Opening %s", url)
        await self.ctx.page.goto(
            url, wait_until="domcontentloaded", timeout=self.config.flow.timeouts.navigation
        )
        await self.ctx.queue.wait_stable()

    async def _open_start_url(self, url: str, *, authenticate: bool = True) -> None:
        """Reach *url* with a session that is known to work.

        For a cabinet URL the authentication guard has already navigated there
        and verified the marker, so navigating a second time would only cost a
        page load. Anything else is a plain navigation.
        """
        if authenticate and self.ctx.auth.protects(url):
            await self.ctx.auth.ensure_authenticated()
            if self.ctx.page.url == url:
                return
        await self.goto(url)

    async def prepare(
        self,
        prerequisites: Sequence[str],
        *,
        start_url: str | None = None,
        authenticate: bool = True,
        announce: bool = True,
    ) -> list[PrepareResult]:
        """Click a chain of selectors to reach the screen holding a target.

        Every journey that starts inside the cabinet is guarded first, so a
        prerequisite chain can never fail with "queue.start_registration is
        missing" when the real problem is an expired session.

        Each prerequisite goes through the normal page-object click path, so it
        inherits unique-match validation, event journalling and automatic
        failure screenshots. The caller's target is never touched here.

        ``announce`` prints the chain as it runs, which is what makes
        ``test-step`` and ``check-center`` readable. A caller whose own output
        *is* the report — the availability scan — turns it off and gets the
        same progress in the log instead.
        """
        if start_url:
            await self._open_start_url(start_url, authenticate=authenticate)

        results: list[PrepareResult] = []
        total = len(prerequisites)

        for number, key in enumerate(prerequisites, start=1):
            spec = self.config.selectors.require(key)
            value = self._runtime_value(spec)

            if announce:
                print(f"\nPREPARE {number}/{total}: {key}")
                print(f"  Locator: {spec.resolved(value).describe()}")
            else:
                logger.info("Prepare %d/%d: %s", number, total, key)

            page_object = self.ctx.page_object_for(key)
            try:
                await page_object.click(spec, value, step=f"prepare:{key}")
            except BaseException:
                if announce:
                    print("  Result: FAILED")
                    print(f"  URL: {self.ctx.page.url}")
                    print(
                        "  The target was never reached. Fix this prerequisite first — "
                        f"`test-step {key}` validates it on its own.\n"
                        "  If the session expired, run `flow` to sign in again."
                    )
                logger.warning("Prerequisite %s failed on %s", key, self.ctx.page.url)
                raise

            if announce:
                print("  Result: OK")
                print(f"  URL: {self.ctx.page.url}")
            results.append(PrepareResult(key=key, url=self.ctx.page.url))

            if self.ctx.diagnostics is not None and self.config.flow.debug.screenshots:
                await self.ctx.diagnostics.screenshot(self.ctx.page, f"prepare-{key}")

        return results

    def _runtime_value(self, spec: LocatorSpec) -> str | None:
        """Supply the value a DYNAMIC prerequisite needs, or explain what is missing."""
        if not spec.is_dynamic:
            return None
        if self.ctx.current_service_center is not None:
            return self.ctx.current_service_center.search_term
        raise FlowError(
            f"{spec.key} is DYNAMIC, so preparation needs a value for it. "
            "Pass --service-center 3242 (the service centre ID)."
        )

    # -------------------------------------------------------------- prompt --

    async def _confirm(self, number: int, total: int, step: Step) -> bool:
        """Print the step preview and, unless --auto, wait for ENTER."""
        spec_line = "Selector: (none — no element involved)"
        locator_line = ""

        if step.selector_key:
            spec_line = f"Selector: {step.selector_key}"
            spec = self.config.selectors.optional(step.selector_key)
            if spec is None:
                locator_line = "Locator:  *** NOT CONFIGURED (TODO) ***"
            elif spec.is_dynamic:
                center = self.ctx.current_service_center
                shown = (
                    spec.resolved(center.search_term).describe()
                    if center is not None
                    else "<runtime value>"
                )
                locator_line = f"Locator:  {shown}"
            else:
                locator_line = f"Locator:  {spec.describe()}"

        print()
        print(f"STEP {number}/{total}: {step.description}")
        print(f"  {spec_line}")
        if locator_line:
            print(f"  {locator_line}")

        if self.auto:
            return True

        answer = (await prompt_async('  Press ENTER to execute or type "s" to skip: ')).strip()
        return answer.lower() not in {"s", "skip"}


async def prompt_async(message: str) -> str:
    """``input()`` without blocking the event loop."""
    try:
        return await asyncio.to_thread(input, message)
    except EOFError:
        # Non-interactive stdin (piped/CI): behave like --auto.
        print("(no interactive stdin — continuing)")
        return ""
