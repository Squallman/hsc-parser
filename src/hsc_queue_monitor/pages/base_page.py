"""BasePage: turns a :class:`LocatorSpec` into a validated Playwright locator.

No page object below this one may contain a raw selector string. Everything
comes from ``config/selectors.yaml`` via :class:`SelectorRegistry`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

from ..browser.diagnostics import Diagnostics
from ..config import SelectorRegistry
from ..models import (
    FlowError,
    LocatorAmbiguous,
    LocatorNotFound,
    LocatorSpec,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 250

#: Enough to describe a candidate without dumping page content wholesale.
_DESCRIBE_JS = """
(el) => ({
  tag: el.tagName.toLowerCase(),
  role: el.getAttribute('role'),
  text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
  aria_label: el.getAttribute('aria-label'),
  id: el.getAttribute('id'),
  data_testid: el.getAttribute('data-testid'),
})
"""


def build_locator(root: Page | Locator, spec: LocatorSpec) -> Locator:
    """Map a spec onto the corresponding Playwright locator API.

    ``root`` is normally the page. Passing a :class:`Locator` instead resolves
    the spec *inside* that element, which is what the calendar needs: two months
    are on screen at once and their day numbers repeat, so a day means nothing
    except relative to the month container it sits in. Playwright's ``Page`` and
    ``Locator`` expose the same lookup API, so one function covers both.

    Pure and side-effect free, so it can be unit-tested against a stub page.
    """
    exact_kwargs: dict[str, Any] = {} if spec.exact is None else {"exact": spec.exact}

    match spec.strategy:
        case "role":
            locator = root.get_by_role(
                spec.role,  # type: ignore[arg-type]
                name=spec.name,
                **exact_kwargs,
            )
        case "text":
            locator = root.get_by_text(spec.value, **exact_kwargs)  # type: ignore[arg-type]
        case "label":
            locator = root.get_by_label(spec.value, **exact_kwargs)  # type: ignore[arg-type]
        case "placeholder":
            locator = root.get_by_placeholder(
                spec.value,  # type: ignore[arg-type]
                **exact_kwargs,
            )
        case "test_id":
            locator = root.get_by_test_id(spec.value)  # type: ignore[arg-type]
        case "css":
            locator = root.locator(spec.value)  # type: ignore[arg-type]
        case _:  # pragma: no cover - guarded by LocatorSpec.from_dict
            raise ValueError(f"Unsupported strategy: {spec.strategy}")

    return locator


class BasePage:
    """Shared locator resolution, validation and instrumented actions."""

    def __init__(
        self,
        page: Page,
        selectors: SelectorRegistry,
        *,
        diagnostics: Diagnostics | None = None,
        default_timeout: int = 15_000,
        transition_timeout: int = 30_000,
    ) -> None:
        self.page = page
        self.selectors = selectors
        self.diagnostics = diagnostics
        self.default_timeout = default_timeout
        #: How long one screen may take to replace another. Wired from
        #: ``timeouts.navigation``: a wizard step swapping screens behind a
        #: spinner is a navigation, not a slow element, so it does not share the
        #: per-locator budget.
        self.transition_timeout = transition_timeout

    # ------------------------------------------------------------- specs ----

    def spec(self, name_or_spec: str | LocatorSpec, value: str | None = None, **params: str
             ) -> LocatorSpec:
        """Look up a selector by dotted name and fill in any runtime values."""
        base = (
            name_or_spec
            if isinstance(name_or_spec, LocatorSpec)
            else self.selectors.require(name_or_spec)
        )
        if value is not None or params or base.is_dynamic:
            return base.resolved(value, **params)
        return base

    def locator(self, name_or_spec: str | LocatorSpec, value: str | None = None,
                **params: str) -> Locator:
        """Public spec -> Locator conversion (no waiting, no validation)."""
        return build_locator(self.page, self.spec(name_or_spec, value, **params))

    def optional_spec(self, key: str) -> LocatorSpec | None:
        return self.selectors.optional(key)

    # -------------------------------------------------------- validation ----

    async def _matching_indices(self, locator: Locator, spec: LocatorSpec) -> list[int]:
        """Indices of matches that satisfy the spec's visibility requirement."""
        total = await locator.count()
        if not spec.visible:
            return list(range(total))
        indices = []
        for index in range(total):
            if await locator.nth(index).is_visible():
                indices.append(index)
        return indices

    async def _wait_for_matches(self, locator: Locator, spec: LocatorSpec, timeout: int
                                ) -> list[int]:
        """Poll until at least one match satisfies the spec, or time out.

        Polling rather than ``locator.first.wait_for()`` on purpose: the first
        DOM match is not necessarily the visible one, and we must never let
        ``.first`` quietly decide which element an action targets.
        """
        deadline = time.monotonic() + timeout / 1000
        indices: list[int] = []
        while True:
            indices = await self._matching_indices(locator, spec)
            if indices:
                return indices
            if time.monotonic() >= deadline:
                return []
            await asyncio.sleep(_POLL_INTERVAL_MS / 1000)

    async def describe_candidates(self, locator: Locator, indices: list[int]) -> list[str]:
        descriptions: list[str] = []
        for index in indices[:10]:
            try:
                info = await locator.nth(index).evaluate(_DESCRIBE_JS)
            except Exception:  # pragma: no cover - element detached
                descriptions.append(f"[{index}] <could not describe>")
                continue
            bits = [f"<{info['tag']}>"]
            for key in ("role", "text", "aria_label", "id", "data_testid"):
                if info.get(key):
                    bits.append(f"{key}={info[key]!r}")
            descriptions.append(f"[{index}] " + " ".join(bits))
        return descriptions

    async def resolve(
        self,
        name_or_spec: str | LocatorSpec,
        value: str | None = None,
        *,
        timeout: int | None = None,
        **params: str,
    ) -> Locator:
        """Resolve to exactly one element, or fail with a useful diagnostic."""
        spec = self.spec(name_or_spec, value, **params)
        locator = build_locator(self.page, spec)
        wait_ms = timeout or spec.timeout or self.default_timeout

        indices = await self._wait_for_matches(locator, spec, wait_ms)
        if not indices:
            raise LocatorNotFound(spec.key, spec.describe())

        if spec.nth is not None:
            if spec.nth >= len(indices):
                raise LocatorNotFound(
                    spec.key,
                    f"{spec.describe()} — nth={spec.nth} requested but only "
                    f"{len(indices)} match(es) available",
                )
            return locator.nth(indices[spec.nth])

        if len(indices) > 1 and spec.expects_unique:
            raise LocatorAmbiguous(spec.key, await self.describe_candidates(locator, indices))

        return locator.nth(indices[0])

    async def resolve_many(
        self,
        name_or_spec: str | LocatorSpec,
        value: str | None = None,
        *,
        timeout: int | None = None,
        required: bool = True,
        **params: str,
    ) -> list[Locator]:
        """All matching elements — for lists (departments, day cells, slots)."""
        spec = self.spec(name_or_spec, value, **params)
        locator = build_locator(self.page, spec)
        wait_ms = timeout or spec.timeout or self.default_timeout

        indices = await self._wait_for_matches(locator, spec, wait_ms)
        if not indices and required:
            raise LocatorNotFound(spec.key, spec.describe())
        return [locator.nth(index) for index in indices]

    async def count_visible(
        self, name_or_spec: str | LocatorSpec, value: str | None = None, **params: str
    ) -> int:
        spec = self.spec(name_or_spec, value, **params)
        locator = build_locator(self.page, spec)
        return len(await self._matching_indices(locator, spec))

    async def is_present(
        self,
        name_or_spec: str | LocatorSpec,
        value: str | None = None,
        *,
        timeout: int = 2_000,
        **params: str,
    ) -> bool:
        """Non-throwing existence probe used for optional markers."""
        spec = self.spec(name_or_spec, value, **params)
        locator = build_locator(self.page, spec)
        return bool(await self._wait_for_matches(locator, spec, timeout))

    # ----------------------------------------------------------- actions ----

    async def _instrumented(
        self, step: str, selector_key: str | None, action: Any
    ) -> None:
        url_before = self.page.url
        started = time.monotonic()
        result = "ok"
        try:
            await action()
        except BaseException as exc:
            result = type(exc).__name__
            if self.diagnostics is not None:
                await self.diagnostics.capture_failure(self.page, step, exc)
            raise
        finally:
            duration = int((time.monotonic() - started) * 1000)
            if self.diagnostics is not None:
                self.diagnostics.record_event(
                    step=step,
                    url_before=url_before,
                    url_after=self.page.url,
                    selector_name=selector_key,
                    result=result,
                    duration_ms=duration,
                )

    async def click(
        self,
        name_or_spec: str | LocatorSpec,
        value: str | None = None,
        *,
        step: str | None = None,
        timeout: int | None = None,
        **params: str,
    ) -> None:
        spec = self.spec(name_or_spec, value, **params)
        step_name = step or spec.key

        async def _do() -> None:
            target = await self.resolve(spec, timeout=timeout)
            logger.info("Clicking %s -> %s", spec.key, spec.describe())
            await target.click()

        await self._instrumented(step_name, spec.key, _do)
        await self.wait_stable()

    async def fill(
        self,
        name_or_spec: str | LocatorSpec,
        text: str,
        *,
        secret: bool = False,
        step: str | None = None,
        timeout: int | None = None,
        **params: str,
    ) -> None:
        spec = self.spec(name_or_spec, **params)
        step_name = step or spec.key

        async def _do() -> None:
            target = await self.resolve(spec, timeout=timeout)
            shown = "<redacted>" if secret else repr(text)
            logger.info("Filling %s with %s", spec.key, shown)
            await target.fill(text)

        await self._instrumented(step_name, spec.key, _do)

    async def select_label(
        self,
        name_or_spec: str | LocatorSpec,
        label: str,
        *,
        step: str | None = None,
        timeout: int | None = None,
    ) -> list[str]:
        """Choose an ``<option>`` of a ``<select>`` by its visible label.

        Uses Playwright's ``select_option()``, never a click on the control and
        a second click on an option: a native dropdown does not render its
        options as clickable page content, and an index would silently follow
        the site whenever it reorders the list.

        Returns the values Playwright reports as selected.
        """
        spec = self.spec(name_or_spec)
        step_name = step or spec.key
        selected: list[str] = []

        async def _do() -> None:
            target = await self.resolve(spec, timeout=timeout)
            logger.debug("Selecting option %r in %s", label, spec.key)
            selected.extend(await target.select_option(label=label))

        await self._instrumented(step_name, spec.key, _do)
        return selected

    async def set_files(
        self,
        name_or_spec: str | LocatorSpec,
        path: Path,
        *,
        step: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """Upload a file. The path is logged; the contents are never read."""
        spec = self.spec(name_or_spec)
        step_name = step or spec.key

        async def _do() -> None:
            target = await self.resolve(spec, timeout=timeout)
            logger.info("Uploading key file to %s (%s)", spec.key, path.name)
            await target.set_input_files(str(path))

        await self._instrumented(step_name, spec.key, _do)

    async def texts(
        self,
        name_or_spec: str | LocatorSpec,
        value: str | None = None,
        *,
        required: bool = False,
        **params: str,
    ) -> list[str]:
        """Trimmed inner text of every visible match."""
        spec = self.spec(name_or_spec, value, **params)
        locators = await self.resolve_many(spec, required=required)
        out: list[str] = []
        for locator in locators:
            try:
                text = (await locator.inner_text()).strip()
            except Exception:  # pragma: no cover - detached during read
                continue
            if text:
                out.append(" ".join(text.split()))
        return out

    # ------------------------------------------------- screen transitions ---

    async def wait_for_screen(
        self,
        key: str,
        *,
        screen: str,
        after: str,
        timeout: int | None = None,
        artifact: str,
    ) -> None:
        """Block until the screen a click leads to is actually on screen.

        This site is a wizard: a click returns while the previous step is still
        mounted behind a loading spinner, so "the click returned" says nothing
        about which screen is up. Every step therefore waits for its own
        destination marker — a condition poll, never a sleep — and a step that
        never arrives fails as the transition it is, with the screen saved,
        instead of surfacing three lines later as a missing selector.

        ``screen`` names the destination ("service-centre screen") and ``after``
        the click that should have produced it ("selecting category A"); both
        end up in the log lines and in the failure message.
        """
        spec = self.spec(key)
        wait_ms = timeout or self.transition_timeout

        logger.info("Waiting for the %s", screen)
        started = time.monotonic()
        if await self._wait_for_matches(build_locator(self.page, spec), spec, wait_ms):
            logger.info(
                "%s ready (%dms)",
                screen[:1].upper() + screen[1:],
                int((time.monotonic() - started) * 1000),
            )
            return

        artifacts = await self.capture_snapshot(artifact)
        raise FlowError(
            f"Timed out waiting for the {screen} after {after} "
            f"({wait_ms // 1000}s).\n"
            f"{spec.key} never became visible, and the browser is on "
            f"{self.page.url}.\n"
            "This is a slow or stalled transition, not a wrong selector — the "
            "site keeps the previous screen up behind its loading spinner, so "
            "nothing was typed and nothing was clicked. Re-run, and raise "
            "timeouts.navigation in config/flow.yaml if the site is simply "
            "slow.\n"
            f"{artifacts}"
        )

    # -------------------------------------------------------- diagnostics ---

    async def capture_snapshot(self, label: str) -> str:
        """Save a screenshot + sanitized element dump; describe where they went.

        The returned text is meant to be pasted into a failure message: a path
        the user has to go looking for is a path they will not read.
        """
        if self.diagnostics is None:
            return (
                "No diagnostics were saved (debug.screenshots is off in "
                "config/flow.yaml, or this run has diagnostics disabled)."
            )
        shot, dump = await self.diagnostics.capture_snapshot(self.page, label)
        lines = ["Saved for inspection:"]
        lines.append(f"  elements:   {dump}" if dump else "  elements:   (dump failed)")
        lines.append(f"  screenshot: {shot}" if shot else "  screenshot: (capture failed)")
        return "\n".join(lines)

    # ------------------------------------------------------------ waiting ---

    async def wait_stable(self, timeout: int = 10_000) -> None:
        """Best-effort settle after an action.

        ``networkidle`` is intentionally lenient: this site polls in the
        background, so a timeout here is normal and not an error.
        """
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            logger.debug("Page did not reach networkidle within %dms (this is often fine)",
                         timeout)
