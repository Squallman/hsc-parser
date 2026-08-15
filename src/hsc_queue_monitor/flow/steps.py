"""The step registry.

A *step* is one named unit of navigation. ``config/flow.yaml`` lists step names
in order; this module says what each name actually does. Adding a screen means
adding a page object plus one entry here — no changes to the engine or the CLI.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from playwright.async_api import Page

from ..browser.diagnostics import Diagnostics
from ..browser.native_files import NativeFileSelector, native_file_selector
from ..config import AppConfig
from ..models import AvailableSlot, FlowError, ServiceCenter
from ..pages.base_page import BasePage
from ..pages.calendar_page import CalendarPage
from ..pages.category_page import CategoryPage
from ..pages.department_page import DepartmentPage
from ..pages.exam_page import ExamPage
from ..pages.login_page import LoginPage
from ..pages.queue_page import QueuePage
from ..pages.time_page import TimePage
from .auth import AuthManager

logger = logging.getLogger(__name__)


@dataclass
class FlowContext:
    """Everything a step needs: page objects, config and per-run state."""

    config: AppConfig
    page: Page
    diagnostics: Diagnostics | None = None
    #: Answers the operating system's file dialog. Left unset in production,
    #: where it is built from the platform and the configured mode; supplied
    #: directly by tests, which have no OS dialog to drive.
    file_selector: NativeFileSelector | None = None

    login: LoginPage = field(init=False)
    queue: QueuePage = field(init=False)
    exam: ExamPage = field(init=False)
    category: CategoryPage = field(init=False)
    department: DepartmentPage = field(init=False)
    calendar: CalendarPage = field(init=False)
    #: The «Час» step. Read-only: it reports free times and cannot book one.
    time: TimePage = field(init=False)

    #: The single authentication path. Everything that needs a signed-in
    #: cabinet calls ``ctx.auth.ensure_authenticated()`` — no command, page
    #: object or step implements login of its own.
    auth: AuthManager = field(init=False)

    #: Set before running the department step. The whole centre travels
    #: together so the ID drives the UI while the name stays available for
    #: display — no code has to pass a human-readable address around.
    current_service_center: ServiceCenter | None = None
    #: Filled in by ``read_slots``.
    last_slots: list[AvailableSlot] = field(default_factory=list)

    def page_object_for(self, selector_key: str) -> BasePage:
        """The page object that owns a selector, chosen by its section prefix.

        Any page object can resolve any key — they all share the registry — but
        routing by section keeps per-screen behaviour and timeouts intact.
        """
        section = selector_key.split(".", 1)[0]
        return {
            "login": self.login,
            "queue": self.queue,
            "exam": self.exam,
            "category": self.category,
            "department": self.department,
            "calendar": self.calendar,
            "time": self.time,
        }.get(section, self.queue)

    def __post_init__(self) -> None:
        selectors = self.config.selectors
        timeouts = self.config.flow.timeouts
        page, diag, timeout = self.page, self.diagnostics, timeouts.default_locator

        authentication = self.config.flow.authentication
        self.login = LoginPage(
            page,
            selectors,
            diagnostics=diag,
            default_timeout=timeout,
            navigation_timeout=timeouts.navigation,
            # Built here, not inside the page object: which operating system
            # this is running on is not a page's business.
            file_selector=self.file_selector
            or native_file_selector(
                mode=authentication.file_selection,
                process_name=authentication.browser_process,
                appear_timeout_ms=timeouts.navigation,
            ),
        )
        self.queue = QueuePage(
            page,
            selectors,
            cabinet_url=self.config.flow.cabinet_url,
            navigation_timeout=timeouts.navigation,
            diagnostics=diag,
            default_timeout=timeout,
        )
        self.exam = ExamPage(page, selectors, diagnostics=diag, default_timeout=timeout)
        self.category = CategoryPage(page, selectors, diagnostics=diag, default_timeout=timeout)
        # Every wizard step swaps screens behind a spinner, so the three page
        # objects that wait for a destination get the navigation budget rather
        # than the per-locator one.
        self.department = DepartmentPage(
            page,
            selectors,
            diagnostics=diag,
            default_timeout=timeout,
            transition_timeout=timeouts.navigation,
        )
        self.calendar = CalendarPage(
            page,
            selectors,
            diagnostics=diag,
            default_timeout=timeout,
            transition_timeout=timeouts.navigation,
        )
        self.time = TimePage(
            page,
            selectors,
            diagnostics=diag,
            default_timeout=timeout,
            transition_timeout=timeouts.navigation,
        )
        self.auth = AuthManager(
            self.config, login=self.login, queue=self.queue, diagnostics=diag
        )


StepAction = Callable[[FlowContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Step:
    """One executable navigation step."""

    name: str
    description: str
    action: StepAction
    #: Dotted selector key this step depends on, for the interactive preview.
    selector_key: str | None = None


# --------------------------------------------------------------------------- #
# Step implementations
# --------------------------------------------------------------------------- #


async def _open_queue(ctx: FlowContext) -> None:
    await ctx.queue.open()


async def _login(ctx: FlowContext) -> None:
    """Delegates to the single authentication path — no second implementation.

    Idempotent, so leaving this step in a flow that already ran the guard costs
    one marker check and nothing else.
    """
    await ctx.auth.ensure_authenticated()


async def _start_registration(ctx: FlowContext) -> None:
    await ctx.queue.start_registration()


async def _practical_exam(ctx: FlowContext) -> None:
    await ctx.exam.select_practical_exam()


async def _category_a(ctx: FlowContext) -> None:
    """Select category A, then wait for the screen that click leads to.

    The click is unchanged; what follows it is. HSC renders the service-centre
    screen asynchronously and keeps the category buttons up behind a spinner
    meanwhile, so without this the next step goes looking for a search box that
    the site has not drawn yet and reports it as a missing selector.
    """
    await ctx.category.select_category_a()
    await ctx.department.wait_until_ready()


async def _list_departments(ctx: FlowContext) -> None:
    names = await ctx.department.list_visible_departments()
    if names:
        logger.info("Service centres on screen:\n  - %s", "\n  - ".join(names))
    else:
        logger.warning(
            "No service centres matched department.department_list_item — either "
            "the selector is wrong or this is not the department screen."
        )


async def _select_department(ctx: FlowContext) -> None:
    if ctx.current_service_center is None:
        enabled = [c for c in ctx.config.service_centers if c.enabled]
        if not enabled:
            raise FlowError(
                "No service centre selected and none enabled in "
                "config/service_centers.yaml."
            )
        ctx.current_service_center = enabled[0]
        logger.info("No centre set for this run; using %s", ctx.current_service_center.name)
    await ctx.department.search_department(ctx.current_service_center.search_term)
    await ctx.department.select_department(ctx.current_service_center.id)


async def _continue_to_calendar(ctx: FlowContext) -> None:
    await ctx.department.continue_to_calendar()


async def _read_slots(ctx: FlowContext) -> None:
    await ctx.calendar.wait_until_loaded()
    center = ctx.current_service_center.name if ctx.current_service_center else "unknown"
    ctx.last_slots = await ctx.calendar.get_available_slots(center)
    if ctx.last_slots:
        logger.info(
            "Found %d slot(s): %s",
            len(ctx.last_slots),
            ", ".join(s.key for s in ctx.last_slots[:10]),
        )
    else:
        logger.info("No available slots for %s", center)


#: name -> (description, selector key, action)
STEP_REGISTRY: dict[str, Step] = {
    step.name: step
    for step in (
        Step("open_queue", "open the cabinet (queue entry point)", _open_queue, None),
        Step(
            "login",
            "ensure an authenticated cabinet (MasterKey login if needed)",
            _login,
            "login.authenticated_marker",
        ),
        Step(
            "start_registration",
            "start a new queue registration",
            _start_registration,
            "queue.start_registration",
        ),
        Step(
            "practical_exam",
            "select practical exam",
            _practical_exam,
            "exam.practical_exam",
        ),
        Step("category_a", "select category A", _category_a, "category.category_a"),
        Step(
            "list_departments",
            "list the service centres on screen",
            _list_departments,
            "department.department_list_item",
        ),
        Step(
            "select_department",
            "select the configured service centre",
            _select_department,
            "department.department_card",
        ),
        Step(
            "continue_to_calendar",
            "continue to the calendar",
            _continue_to_calendar,
            "department.continue",
        ),
        Step("read_slots", "read available dates and times", _read_slots,
             "calendar.available_slot"),
    )
}


def get_step(name: str) -> Step:
    try:
        return STEP_REGISTRY[name]
    except KeyError:
        raise FlowError(
            f"Unknown flow step {name!r}. Available steps: "
            f"{', '.join(sorted(STEP_REGISTRY))}"
        ) from None
