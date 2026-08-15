"""Availability scanning: centre → free dates → free times.

The fake below is the whole booking wizard as a state machine — cabinet,
registration, exam, category, service centres, calendar, time — plus the two
screens the scanner is **forbidden** to reach. Nothing here touches a browser.

The safety boundary is tested as hard as the feature: a scan that read the times
and then clicked one would pass every functional test in this file and be a
serious bug, so several tests exist purely to prove it does not happen.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeElement, FakeLocator, FakePage

from hsc_queue_monitor.browser.diagnostics import Diagnostics
from hsc_queue_monitor.cli import run_check_availability
from hsc_queue_monitor.config import (
    MAX_SCANNED_CENTERS,
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SelectorRegistry,
    centers_to_scan,
    load_secrets,
)
from hsc_queue_monitor.flow import availability as availability_module
from hsc_queue_monitor.flow.availability import AvailabilityScanner, render_availability
from hsc_queue_monitor.flow.steps import FlowContext
from hsc_queue_monitor.models import (
    CentreAvailability,
    ConfigError,
    DateAvailability,
    FlowError,
    ServiceCenter,
    TimeSlot,
)
from hsc_queue_monitor.pages import time_page as time_page_module
from hsc_queue_monitor.pages.calendar_page import CalendarPage
from hsc_queue_monitor.pages.time_page import TimePage
from hsc_queue_monitor.pages.ui_text import (
    parse_day_number,
    parse_month_caption,
    parse_slot_time,
)

CABINET = "https://eqn.hsc.gov.ua/cabinet"

MARKER_TEXT = "Записатись у чергу"
SEARCH_PLACEHOLDER = "Пошук сервісного центру МВС"
CALENDAR_READY = "Оберіть дату"
TIME_READY = "Час"
BACK_TEXT = "Назад"

#: The wizard controls between the cabinet and the service-centre screen, in
#: the order the site presents them.
PREREQUISITE_NAMES = {
    MARKER_TEXT: "queue.start_registration",
    "Практичний іспит": "exam.practical_exam",
    "Практичний іспит на транспортному засобі Сервісного центру МВС":
        "exam.service_center_vehicle",
    "категорія А (механична КПП)": "category.category_a",
}

CENTRE_3242 = ServiceCenter(
    name="ТСЦ МВС № 3242", id="3242",
    full_name="ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20", enabled=True,
)
CENTRE_4641 = ServiceCenter(
    name="ТСЦ МВС № 4641", id="4641",
    full_name="ТСЦ МВС № 4641 м. Київ, вул. Лугова 19", enabled=True,
)

#: The measured shape of the live calendar: two months at once, with day
#: numbers that overlap between them.
TWO_MONTHS: dict[str, list[tuple[int, bool]]] = {
    "Серпень 2026": [(20, False), (21, True), (26, True), (27, True)],
    "Вересень 2026": [(1, True), (2, True), (3, False)],
}

SELECTORS = f"""
login:
  authenticated_marker:
    strategy: role
    role: link
    name: "{MARKER_TEXT}"
    exact: true
queue:
  start_registration:
    strategy: role
    role: link
    name: "{MARKER_TEXT}"
    exact: true
exam:
  practical_exam:
    strategy: role
    role: button
    name: "Практичний іспит"
    exact: true
  service_center_vehicle:
    strategy: role
    role: button
    name: "Практичний іспит на транспортному засобі Сервісного центру МВС"
    exact: true
category:
  category_a:
    strategy: role
    role: button
    name: "категорія А (механична КПП)"
    exact: true
department:
  search:
    strategy: placeholder
    value: "{SEARCH_PLACEHOLDER}"
    exact: true
  department_card:
    strategy: role
    role: button
    name: DYNAMIC
    exact: false
    multiple: true
calendar:
  ready_marker:
    strategy: text
    value: "{CALENDAR_READY}"
    exact: false
  month:
    strategy: css
    value: ".month"
    multiple: true
  day:
    strategy: css
    value: "button"
    multiple: true
time:
  ready_marker:
    strategy: text
    value: "{TIME_READY}"
    exact: true
  slot:
    strategy: css
    value: "button"
    multiple: true
wizard:
  back:
    strategy: role
    role: button
    name: "{{back}}"
    optional: true
"""

FLOW = f"""
site:
  cabinet_url: "{CABINET}"
timeouts:
  default_locator: 200
  navigation: 200
debug:
  screenshots: false
steps:
  department.search:
    start_url: "{CABINET}"
    prerequisites:
      - queue.start_registration
      - exam.practical_exam
      - exam.service_center_vehicle
      - category.category_a
"""


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def el(marker: str, **attrs: Any) -> FakeElement:
    return FakeElement(marker=marker, **attrs)


class WizardLocator(FakeLocator):
    """A locator whose clicks drive the wizard, and whose scoped lookups keep it."""

    def __init__(
        self, elements: list[FakeElement], page: Wizard, index: int | None = None
    ) -> None:
        super().__init__(elements, index)
        self._page = page

    def nth(self, index: int) -> WizardLocator:
        return WizardLocator(self._elements, self._page, index)

    def locator(self, value: str) -> WizardLocator:
        children = self._element.attrs.get("children") or {}
        return WizardLocator(list(children.get(value, [])), self._page)

    async def click(self) -> None:
        await super().click()
        marker = str(self._element.attrs.get("marker"))
        self._page.record(f"click:{marker}")
        self._page.advance(marker)


class Wizard(FakePage):
    """The HSC booking wizard, as far as the scanner is allowed to go.

    ``months`` maps a calendar caption to its ``(day, enabled)`` cells and
    ``slots`` maps a date to its ``(label, enabled)`` time controls, so a test
    states what the site offers and nothing else.
    """

    def __init__(
        self,
        *,
        centres: list[ServiceCenter] | None = None,
        disabled_centres: tuple[str, ...] = (),
        missing_centres: tuple[str, ...] = (),
        months: dict[str, list[tuple[int, bool]]] | None = None,
        slots: dict[date, list[tuple[str, bool]]] | None = None,
        back: bool = True,
        calendar_never_ready: bool = False,
    ) -> None:
        super().__init__(matches={})
        self.centres = centres or [CENTRE_3242]
        self.disabled_centres = disabled_centres
        self.missing_centres = missing_centres
        self.months = TWO_MONTHS if months is None else months
        self.slots = {} if slots is None else slots
        self.back = back
        self.calendar_never_ready = calendar_never_ready

        self.url = CABINET
        self.screen = "cabinet"
        self.actions: list[str] = []
        self.screens_visited: list[str] = ["cabinet"]
        #: The date whose times are on screen. Set by clicking a day.
        self.selected_date: date | None = None
        self.selected_centre: str | None = None

    # -- state machine ----------------------------------------------------

    def record(self, action: str) -> None:
        self.actions.append(action)

    def _enter(self, screen: str) -> None:
        self.screen = screen
        self.screens_visited.append(screen)

    def advance(self, marker: str) -> None:
        if marker in PREREQUISITE_NAMES.values():
            order = list(PREREQUISITE_NAMES.values())
            self._enter(("exam", "vehicle", "category", "departments")[order.index(marker)])
        elif marker.startswith("centre:"):
            self.selected_centre = marker.split(":", 1)[1]
            self._enter("calendar")
        elif marker.startswith("day:"):
            self.selected_date = date.fromisoformat(marker.split(":", 1)[1])
            self._enter("time")
        elif marker == "back":
            self._enter({"time": "calendar", "calendar": "departments"}.get(
                self.screen, self.screen))
        elif marker.startswith("slot:") or marker == "next":
            # Reachable only by a scanner that books. Nothing may end up here.
            self._enter("contacts")

    # -- screens ----------------------------------------------------------

    def _on_screen(self) -> list[FakeElement]:
        match self.screen:
            case "cabinet":
                return [el("queue.start_registration", tag="a", text=MARKER_TEXT)]
            case "exam" | "vehicle" | "category":
                name = {
                    "exam": "Практичний іспит",
                    "vehicle": "Практичний іспит на транспортному засобі "
                               "Сервісного центру МВС",
                    "category": "категорія А (механична КПП)",
                }[self.screen]
                return [el(PREREQUISITE_NAMES[name], tag="button", text=name)]
            case "departments":
                return self._department_screen()
            case "calendar":
                return self._calendar_screen()
            case "time":
                return self._time_screen()
            case "contacts":  # pragma: no cover - nothing may reach it
                return [el("contacts", tag="div", text="Контакти")]
        return []  # pragma: no cover - every screen is listed above

    def _department_screen(self) -> list[FakeElement]:
        elements = [
            el("search", tag="input", placeholder=SEARCH_PLACEHOLDER),
            *self._back_element(),
        ]
        for centre in self.centres:
            if centre.id in self.missing_centres:
                continue
            elements.append(
                el(
                    f"centre:{centre.id}",
                    tag="button",
                    text=centre.full_name or centre.name,
                    disabled=centre.id in self.disabled_centres,
                )
            )
        return elements

    def _calendar_screen(self) -> list[FakeElement]:
        if self.calendar_never_ready:
            return [el("spinner", tag="div", text="Зачекайте")]
        months = []
        for caption, days in self.months.items():
            parsed = parse_month_caption(caption)
            buttons = []
            for day, enabled in days:
                marker = "day:unknown"
                if parsed is not None:
                    marker = f"day:{date(parsed[0], parsed[1], day).isoformat()}"
                buttons.append(
                    el(marker, tag="button", text=str(day), css="button",
                       disabled=not enabled)
                )
            months.append(
                el(
                    f"month:{caption}",
                    tag="div",
                    css=".month",
                    # innerText of a month block is its caption plus every day
                    # in it — exactly what the real container reads back as.
                    text=f"{caption} " + " ".join(str(d) for d, _e in days),
                    children={"button": buttons},
                )
            )
        return [
            el("calendar_heading", tag="h2", text=CALENDAR_READY),
            *months,
            *self._back_element(),
        ]

    def _time_screen(self) -> list[FakeElement]:
        offered = self.slots.get(self.selected_date or date.min, [])
        elements = [
            el("time_heading", tag="h2", text=TIME_READY),
            *self._back_element(),
        ]
        elements.extend(
            el(f"slot:{label}", tag="button", css="button", text=label,
               disabled=not enabled)
            for label, enabled in offered
        )
        # A booking control, deliberately: it carries a time in its label and
        # must not be mistaken for a slot, and clicking it would be a booking.
        elements.append(el("next", tag="button", css="button", text="Записатись на 09:20"))
        return elements

    def _back_element(self) -> list[FakeElement]:
        if not self.back:
            return []
        # Also matched by `time.slot` (css: button) — proving the time filter
        # rejects a control whose label is not a time.
        return [el("back", tag="button", css="button", text=BACK_TEXT)]

    def element(self, marker: str) -> FakeElement:
        for element in self._on_screen():
            if element.attrs.get("marker") == marker:
                return element
        raise AssertionError(f"no element marked {marker!r} on {self.screen!r}")

    # -- queries ----------------------------------------------------------

    def _match(self, attribute: str, wanted: str | None, exact: bool | None
               ) -> WizardLocator:
        found = []
        for candidate in self._on_screen():
            value = candidate.attrs.get(attribute)
            if value is None or wanted is None:
                continue
            matched = str(value) == wanted if exact else wanted in str(value)
            if matched:
                found.append(candidate)
        return WizardLocator(found, self)

    def get_by_role(self, role: str, **kwargs: Any) -> WizardLocator:
        self.calls.append(("get_by_role", (role,), kwargs))
        return self._match("text", kwargs.get("name"), kwargs.get("exact"))

    def get_by_text(self, value: str, **kwargs: Any) -> WizardLocator:
        self.calls.append(("get_by_text", (value,), kwargs))
        return self._match("text", value, kwargs.get("exact"))

    def get_by_placeholder(self, value: str, **kwargs: Any) -> WizardLocator:
        self.calls.append(("get_by_placeholder", (value,), kwargs))
        return self._match("placeholder", value, kwargs.get("exact"))

    def locator(self, value: str) -> WizardLocator:
        self.calls.append(("locator", (value,), {}))
        return self._match("css", value, True)

    async def goto(self, url: str, **_k: Any) -> None:
        self.calls.append(("goto", (url,), {}))
        self.url = url
        if url.startswith(CABINET):
            self._enter("cabinet")


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def build_config(
    tmp_path: Path,
    centres: list[ServiceCenter] | None = None,
    *,
    back: bool = True,
    selectors_yaml: str | None = None,
) -> AppConfig:
    """``back=False`` leaves wizard.back a TODO, as it ships today."""
    text = selectors_yaml or SELECTORS
    text = text.format(back=BACK_TEXT if back else "TODO")
    return AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        app=AppSettings(),
        paths=Paths(data_dir=tmp_path),
        _selectors=SelectorRegistry.from_dict(yaml.safe_load(text)),
        _flow=FlowConfig.from_dict(yaml.safe_load(FLOW)),
        service_centers=[CENTRE_3242] if centres is None else centres,
    )


def build_context(config: AppConfig, page: FakePage, tmp_path: Path | None = None
                  ) -> FlowContext:
    diagnostics = (
        Diagnostics(tmp_path / "debug", enabled=True) if tmp_path is not None else None
    )
    return FlowContext(config=config, page=page, diagnostics=diagnostics)


def calendar_of(config: AppConfig, page: FakePage) -> CalendarPage:
    return build_context(config, page).calendar


def time_of(config: AppConfig, page: FakePage) -> TimePage:
    return build_context(config, page).time


def slots_for(*labels: str) -> list[tuple[str, bool]]:
    return [(label, True) for label in labels]


AUG_21 = date(2026, 8, 21)
AUG_26 = date(2026, 8, 26)
AUG_27 = date(2026, 8, 27)
SEP_1 = date(2026, 9, 1)
SEP_2 = date(2026, 9, 2)

#: Every date the two-month fixture offers, in order.
OPEN_DATES = [AUG_21, AUG_26, AUG_27, SEP_1, SEP_2]


def wizard(**kwargs: Any) -> Wizard:
    return Wizard(**kwargs)


# --------------------------------------------------------------------------- #
# Configuration: which centres a scan covers
# --------------------------------------------------------------------------- #


def centres(count: int) -> list[ServiceCenter]:
    return [
        ServiceCenter(name=f"ТСЦ МВС № {3240 + n}", id=str(3240 + n), enabled=True)
        for n in range(count)
    ]


def test_one_configured_centre_is_scanned():
    assert [c.id for c in centers_to_scan(centres(1))] == ["3240"]


def test_five_configured_centres_are_scanned():
    chosen = centers_to_scan(centres(MAX_SCANNED_CENTERS))
    assert len(chosen) == 5


def test_no_enabled_centre_is_rejected():
    disabled = [ServiceCenter(name="ТСЦ МВС № 3242", id="3242", enabled=False)]

    with pytest.raises(ConfigError) as exc:
        centers_to_scan(disabled)

    message = str(exc.value)
    assert "No service centre to scan" in message
    assert "service_centers.yaml" in message
    assert "--center" in message


def test_more_than_five_centres_is_rejected():
    with pytest.raises(ConfigError) as exc:
        centers_to_scan(centres(MAX_SCANNED_CENTERS + 1))

    message = str(exc.value)
    assert "at most 5" in message
    assert "6 service centres were requested" in message


def test_a_disabled_centre_is_skipped():
    configured = [
        CENTRE_3242,
        ServiceCenter(name="ТСЦ МВС № 4641", id="4641", enabled=False),
    ]
    assert [c.id for c in centers_to_scan(configured)] == ["3242"]


def test_center_overrides_replace_the_enabled_list():
    configured = [CENTRE_3242, CENTRE_4641, ServiceCenter(name="ТСЦ 8041", id="8041")]

    chosen = centers_to_scan(configured, ["4641", "8041"])

    assert [c.id for c in chosen] == ["4641", "8041"]


def test_a_repeated_override_names_one_centre():
    chosen = centers_to_scan([CENTRE_3242, CENTRE_4641], ["3242", "3242"])
    assert [c.id for c in chosen] == ["3242"]


def test_an_override_may_name_a_disabled_centre():
    """Asked for by ID, the way check-center already behaves."""
    off = ServiceCenter(name="ТСЦ МВС № 4641", id="4641", enabled=False)

    assert [c.id for c in centers_to_scan([CENTRE_3242, off], ["4641"])] == ["4641"]


def test_an_unknown_override_is_a_configuration_error():
    with pytest.raises(ConfigError, match="Unknown service centre"):
        centers_to_scan([CENTRE_3242], ["9999"])


def test_too_many_overrides_are_rejected():
    with pytest.raises(ConfigError, match="at most 5"):
        centers_to_scan(centres(6), [c.id for c in centres(6)])


def test_no_service_centre_id_is_written_into_the_source():
    """Centres come from configuration only — never from code."""
    for module in (availability_module, time_page_module):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        assert "3242" not in source
        assert "4641" not in source


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("Серпень 2026", (2026, 8)),
        ("Вересень 2026", (2026, 9)),
        ("вересень 2026", (2026, 9)),
        ("Січень 2027", (2027, 1)),
        ("Серпень 2026 1 2 3 4", (2026, 8)),
    ],
)
def test_month_captions_are_parsed(caption, expected):
    assert parse_month_caption(caption) == expected


@pytest.mark.parametrize("caption", [None, "", "Серпень", "2026", "Місяць 2026"])
def test_unparseable_month_captions_return_none(caption):
    assert parse_month_caption(caption) is None


@pytest.mark.parametrize(
    ("label", "expected"), [("1", 1), ("21", 21), ("31", 31), (" 7 ", 7)]
)
def test_day_numbers_are_parsed(label, expected):
    assert parse_day_number(label) == expected


@pytest.mark.parametrize("label", [None, "", "0", "32", "пн", "21 серпня", "2.1"])
def test_non_day_labels_are_not_days(label):
    assert parse_day_number(label) is None


@pytest.mark.parametrize(
    ("label", "expected"), [("09:20", time(9, 20)), ("9:05", time(9, 5))]
)
def test_slot_times_are_parsed(label, expected):
    assert parse_slot_time(label) == expected


@pytest.mark.parametrize(
    "label", [None, "", "Записатись на 09:20", "09:20 - 09:40", "Назад", "24:00"]
)
def test_a_label_that_is_not_exactly_a_time_is_not_a_slot(label):
    """A booking button carrying a time must never be read as a free slot."""
    assert parse_slot_time(label) is None


# --------------------------------------------------------------------------- #
# CalendarPage: two months, read independently
# --------------------------------------------------------------------------- #


async def test_both_visible_months_are_parsed(tmp_path):
    page = wizard()
    page.screen = "calendar"

    dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert [found.date for found in dates] == OPEN_DATES


async def test_dates_are_built_from_the_month_the_day_sits_in(tmp_path):
    """Full YYYY-MM-DD, from the container's caption — never the day alone."""
    page = wizard()
    page.screen = "calendar"

    dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert dates[0].date.isoformat() == "2026-08-21"
    assert dates[-1].date.isoformat() == "2026-09-02"
    assert {found.date.year for found in dates} == {2026}
    assert {found.date.month for found in dates} == {8, 9}


async def test_a_day_number_present_in_both_months_yields_two_dates(tmp_path):
    """«1» exists twice on screen; the two must not collapse into one date."""
    page = wizard(
        months={
            "Серпень 2026": [(1, True), (2, True)],
            "Вересень 2026": [(1, True), (2, True)],
        }
    )
    page.screen = "calendar"

    dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert [found.date for found in dates] == [
        date(2026, 8, 1), date(2026, 8, 2), date(2026, 9, 1), date(2026, 9, 2)
    ]


async def test_only_enabled_days_are_available(tmp_path):
    page = wizard(months={"Серпень 2026": [(20, False), (21, True), (22, False)]})
    page.screen = "calendar"

    dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert [found.date for found in dates] == [AUG_21]


async def test_a_calendar_with_nothing_free_is_empty_not_an_error(tmp_path):
    page = wizard(months={"Серпень 2026": [(20, False), (21, False)]})
    page.screen = "calendar"

    assert await calendar_of(build_config(tmp_path), page).available_dates() == []


async def test_a_month_without_a_caption_is_skipped_loudly(tmp_path, caplog):
    page = wizard(months={"Серпень 2026": [(21, True)], "???": [(9, True)]})
    page.screen = "calendar"

    with caplog.at_level(logging.WARNING):
        dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert [found.date for found in dates] == [AUG_21]
    assert "no recognisable" in caplog.text


async def test_the_order_of_the_month_containers_is_not_assumed(tmp_path):
    """September first in the DOM must not make it "the second month"."""
    page = wizard(
        months={
            "Вересень 2026": [(1, True)],
            "Серпень 2026": [(21, True)],
        }
    )
    page.screen = "calendar"

    dates = await calendar_of(build_config(tmp_path), page).available_dates()

    assert [found.date for found in dates] == [AUG_21, SEP_1]


# --------------------------------------------------------------------------- #
# CalendarPage: selecting a date
# --------------------------------------------------------------------------- #


async def test_select_date_clicks_the_day_inside_its_own_month(tmp_path):
    page = wizard(
        months={
            "Серпень 2026": [(1, True), (2, True)],
            "Вересень 2026": [(1, True), (2, True)],
        }
    )
    page.screen = "calendar"

    await calendar_of(build_config(tmp_path), page).select_date(date(2026, 9, 1))

    # The September «1», not the August one that renders first.
    assert page.actions == ["click:day:2026-09-01"]
    assert page.screen == "time"
    assert page.selected_date == date(2026, 9, 1)


async def test_select_date_refuses_a_disabled_day(tmp_path):
    page = wizard()
    page.screen = "calendar"

    with pytest.raises(FlowError) as exc:
        await calendar_of(build_config(tmp_path), page).select_date(date(2026, 8, 20))

    assert "disabled" in str(exc.value)
    assert "never force-clicked" in str(exc.value)
    assert page.actions == []
    assert page.screen == "calendar"


async def test_select_date_refuses_a_month_that_is_not_on_screen(tmp_path):
    page = wizard()
    page.screen = "calendar"

    with pytest.raises(FlowError) as exc:
        await calendar_of(build_config(tmp_path), page).select_date(date(2026, 10, 1))

    message = str(exc.value)
    assert "2026-10-01 is not in any month" in message
    assert "Серпень 2026" in message  # what it *is* showing
    assert page.actions == []


async def test_select_date_refuses_a_day_the_month_does_not_have(tmp_path):
    page = wizard(months={"Серпень 2026": [(21, True)]})
    page.screen = "calendar"

    with pytest.raises(FlowError) as exc:
        await calendar_of(build_config(tmp_path), page).select_date(AUG_26)

    assert "no day 26 button" in str(exc.value)
    assert page.actions == []


# --------------------------------------------------------------------------- #
# Calendar and time readiness
# --------------------------------------------------------------------------- #


async def test_the_calendar_is_waited_for_by_its_own_step_heading(tmp_path, caplog):
    page = wizard()
    page.screen = "calendar"

    with caplog.at_level(logging.INFO):
        await calendar_of(build_config(tmp_path), page).wait_until_ready()

    assert "Waiting for the calendar screen" in caplog.text
    assert "Calendar screen ready" in caplog.text
    assert ("get_by_text", (CALENDAR_READY,), {"exact": False}) in page.calls


async def test_a_calendar_that_never_renders_fails_with_diagnostics(tmp_path):
    page = wizard(calendar_never_ready=True)
    page.screen = "calendar"
    calendar = build_context(build_config(tmp_path), page, tmp_path).calendar

    with pytest.raises(FlowError) as exc:
        await calendar.wait_until_ready()

    message = str(exc.value)
    assert "Timed out waiting for the calendar screen" in message
    assert "selecting the service centre" in message
    assert "calendar.ready_marker never became visible" in message
    shots = list((tmp_path / "debug").glob("*calendar-screen-timeout.png"))
    dumps = list((tmp_path / "debug").glob("*calendar-screen-timeout-elements.json"))
    assert shots and dumps
    assert str(shots[0]) in message


async def test_the_time_screen_is_waited_for_by_its_step_heading(tmp_path, caplog):
    page = wizard(slots={AUG_21: slots_for("09:20")})
    page.screen, page.selected_date = "time", AUG_21

    with caplog.at_level(logging.INFO):
        await time_of(build_config(tmp_path), page).wait_until_ready()

    assert "Waiting for the time screen" in caplog.text
    assert "Time screen ready" in caplog.text


async def test_a_time_screen_that_never_arrives_fails_with_diagnostics(tmp_path):
    page = wizard()
    page.screen = "calendar"  # the date step never advanced
    time_page = build_context(build_config(tmp_path), page, tmp_path).time

    with pytest.raises(FlowError) as exc:
        await time_page.wait_until_ready()

    assert "Timed out waiting for the time screen" in str(exc.value)
    assert "selecting a date" in str(exc.value)
    assert list((tmp_path / "debug").glob("*time-screen-timeout.png"))


# --------------------------------------------------------------------------- #
# TimePage: reading, never acting
# --------------------------------------------------------------------------- #


async def test_enabled_times_are_returned_in_order(tmp_path):
    page = wizard(slots={AUG_21: [("09:20", True), ("10:40", True), ("11:00", True)]})
    page.screen, page.selected_date = "time", AUG_21

    slots = await time_of(build_config(tmp_path), page).available_slots()

    assert [slot.display for slot in slots] == ["09:20", "10:40", "11:00"]
    assert [slot.time for slot in slots] == [time(9, 20), time(10, 40), time(11, 0)]
    assert [slot.text for slot in slots] == ["09:20", "10:40", "11:00"]


async def test_taken_times_are_not_returned(tmp_path):
    page = wizard(slots={AUG_21: [("09:20", False), ("10:40", True), ("11:00", False)]})
    page.screen, page.selected_date = "time", AUG_21

    slots = await time_of(build_config(tmp_path), page).available_slots()

    assert [slot.display for slot in slots] == ["10:40"]


async def test_a_date_with_no_free_time_returns_an_empty_list(tmp_path):
    """A normal observation, not an exception."""
    page = wizard(slots={AUG_21: [("09:20", False)]})
    page.screen, page.selected_date = "time", AUG_21

    assert await time_of(build_config(tmp_path), page).available_slots() == []


async def test_controls_that_are_not_times_are_ignored(tmp_path):
    """«Назад» and «Записатись на 09:20» share the slot selector; neither is one."""
    page = wizard(slots={AUG_21: slots_for("09:20")})
    page.screen, page.selected_date = "time", AUG_21

    slots = await time_of(build_config(tmp_path), page).available_slots()

    assert [slot.text for slot in slots] == ["09:20"]


async def test_reading_the_times_clicks_nothing(tmp_path):
    page = wizard(slots={AUG_21: slots_for("09:20", "10:40")})
    page.screen, page.selected_date = "time", AUG_21

    await time_of(build_config(tmp_path), page).available_slots()

    assert page.actions == []
    assert page.screens_visited == ["cabinet"]


# --------------------------------------------------------------------------- #
# The scanner
# --------------------------------------------------------------------------- #


def scanner_for(config: AppConfig, page: FakePage, tmp_path: Path | None = None
                ) -> AvailabilityScanner:
    return AvailabilityScanner(build_context(config, page, tmp_path))


async def test_one_centre_is_scanned_end_to_end(tmp_path):
    page = wizard(
        months={"Серпень 2026": [(21, True), (26, True)]},
        slots={AUG_21: slots_for("09:20", "10:40"), AUG_26: slots_for("14:00")},
    )
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert len(results) == 1
    result = results[0]
    assert result.centre_id == "3242"
    assert result.status == "bookable"
    assert result.bookable is True
    assert result.slot_count == 3
    assert [day.date for day in result.dates] == [AUG_21, AUG_26]
    assert [slot.display for slot in result.dates[0].slots] == ["09:20", "10:40"]
    assert [slot.display for slot in result.dates[1].slots] == ["14:00"]


async def test_every_available_date_is_visited(tmp_path):
    page = wizard(slots={day: slots_for("09:20") for day in OPEN_DATES})
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert [day.date for day in results[0].dates] == OPEN_DATES
    clicked = [a for a in page.actions if a.startswith("click:day:")]
    assert clicked == [f"click:day:{day.isoformat()}" for day in OPEN_DATES]


async def test_the_scanner_returns_to_the_calendar_between_dates(tmp_path):
    page = wizard(
        months={"Серпень 2026": [(21, True), (26, True)]},
        slots={AUG_21: slots_for("09:20"), AUG_26: slots_for("14:00")},
    )
    config = build_config(tmp_path)

    await scanner_for(config, page).scan([CENTRE_3242])

    # time -> back -> calendar -> next day, and never a second wizard replay.
    assert page.actions.count("click:back") >= 2
    steps = [a for a in page.actions if a.startswith(("click:day:", "click:back"))]
    assert steps[:4] == [
        "click:day:2026-08-21", "click:back", "click:day:2026-08-26", "click:back",
    ]
    assert page.actions.count("click:queue.start_registration") == 1


async def test_the_wizard_is_replayed_when_there_is_no_back_control(tmp_path):
    """wizard.back is still TODO in the shipped config; the scan must still work."""
    page = wizard(
        back=False,
        months={"Серпень 2026": [(21, True), (26, True)]},
        slots={AUG_21: slots_for("09:20"), AUG_26: slots_for("14:00")},
    )
    config = build_config(tmp_path, back=False)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert [day.date for day in results[0].dates] == [AUG_21, AUG_26]
    assert [slot.display for slot in results[0].dates[1].slots] == ["14:00"]
    assert "click:back" not in page.actions
    # It walked the wizard again instead — from the cabinet, not from a login.
    assert page.actions.count("click:queue.start_registration") > 1


async def test_several_centres_are_scanned_in_order(tmp_path):
    page = wizard(
        centres=[CENTRE_3242, CENTRE_4641],
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
    )
    config = build_config(tmp_path, [CENTRE_3242, CENTRE_4641])

    results = await scanner_for(config, page).scan([CENTRE_3242, CENTRE_4641])

    assert [result.centre_id for result in results] == ["3242", "4641"]
    assert all(result.bookable for result in results)
    assert page.actions.count("click:centre:3242") == 1
    assert page.actions.count("click:centre:4641") == 1
    assert page.actions.index("click:centre:3242") < page.actions.index(
        "click:centre:4641"
    )


async def test_five_centres_are_scanned(tmp_path):
    five = centres(5)
    page = wizard(
        centres=five,
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
    )
    config = build_config(tmp_path, five)

    results = await scanner_for(config, page).scan(five)

    assert [result.centre_id for result in results] == [c.id for c in five]
    assert all(result.slot_count == 1 for result in results)


async def test_the_session_guard_runs_once_and_never_logs_in_again(tmp_path, caplog):
    page = wizard(
        centres=[CENTRE_3242, CENTRE_4641],
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
    )
    config = build_config(tmp_path, [CENTRE_3242, CENTRE_4641])

    with caplog.at_level(logging.INFO):
        await scanner_for(config, page).scan([CENTRE_3242, CENTRE_4641])

    assert "HSC authenticated session is active" in caplog.text
    # The live session was reused throughout: no journey was ever started.
    assert "Starting ID.GOV.UA authentication" not in caplog.text
    assert "Authentication session is not active" not in caplog.text


async def test_a_disabled_centre_card_is_never_opened(tmp_path):
    page = wizard(disabled_centres=("3242",))
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert results[0].status == "centre-unavailable"
    assert results[0].bookable is False
    assert "click:centre:3242" not in page.actions
    assert "calendar" not in page.screens_visited


async def test_a_centre_that_is_not_on_screen_is_reported(tmp_path):
    page = wizard(missing_centres=("3242",))
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert results[0].status == "not-found"
    assert results[0].found is False
    assert "calendar" not in page.screens_visited


async def test_a_centre_with_no_free_dates_is_distinguished(tmp_path):
    page = wizard(months={"Серпень 2026": [(20, False), (21, False)]})
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert results[0].status == "no-dates"
    assert results[0].dates == ()
    assert results[0].bookable is False


async def test_a_date_with_no_free_times_is_distinguished(tmp_path):
    page = wizard(
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: [("09:20", False)]},
    )
    config = build_config(tmp_path)

    results = await scanner_for(config, page).scan([CENTRE_3242])

    assert results[0].status == "no-times"
    assert results[0].bookable is False
    assert [day.date for day in results[0].dates] == [AUG_21]
    assert results[0].dates[0].slots == ()


async def test_an_unexpected_state_is_captured_and_does_not_stop_the_scan(tmp_path):
    """One broken centre must not cost the other centre's result."""
    page = wizard(
        centres=[CENTRE_3242, CENTRE_4641],
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
        calendar_never_ready=True,
    )
    config = build_config(tmp_path, [CENTRE_3242, CENTRE_4641])

    results = await scanner_for(config, page, tmp_path).scan([CENTRE_3242, CENTRE_4641])

    assert [result.status for result in results] == ["error", "error"]
    assert "Timed out waiting for the calendar screen" in results[0].error
    # The artifacts name the centre that was being scanned.
    assert list((tmp_path / "debug").glob("*availability-3242.png"))
    assert list((tmp_path / "debug").glob("*availability-4641.png"))


async def test_a_scan_failure_names_the_date_it_happened_on(tmp_path, caplog):
    page = wizard(
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
    )

    # The time step never arrives for this date.
    original = TimePage.wait_until_ready

    async def stuck(self: TimePage, **_kwargs: Any) -> None:
        raise FlowError("Timed out waiting for the time screen after selecting a date")

    TimePage.wait_until_ready = stuck  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.WARNING):
            results = await scanner_for(build_config(tmp_path), page, tmp_path).scan(
                [CENTRE_3242]
            )
    finally:
        TimePage.wait_until_ready = original  # type: ignore[method-assign]

    assert results[0].dates[0].error
    assert results[0].status == "no-times"
    assert "3242 on 2026-08-21" in caplog.text
    assert list((tmp_path / "debug").glob("*availability-3242-2026-08-21.png"))


# --------------------------------------------------------------------------- #
# The safety boundary
# --------------------------------------------------------------------------- #


async def test_no_time_slot_is_ever_clicked(tmp_path):
    page = wizard(
        slots={day: slots_for("09:20", "10:40") for day in OPEN_DATES},
    )
    config = build_config(tmp_path)

    await scanner_for(config, page).scan([CENTRE_3242])

    assert not [action for action in page.actions if action.startswith("click:slot:")]
    assert "click:next" not in page.actions


async def test_the_contacts_step_is_never_reached(tmp_path):
    page = wizard(slots={day: slots_for("09:20") for day in OPEN_DATES})
    config = build_config(tmp_path)

    await scanner_for(config, page).scan([CENTRE_3242])

    assert "contacts" not in page.screens_visited
    assert page.screen in {"calendar", "departments", "time"}


#: Words that would name a booking action. Matched whole, so `bookable` — a
#: question about availability — is not mistaken for booking anything.
FORBIDDEN = ("select_slot", "choose_slot", "book", "reserve", "submit",
             "confirm", "contacts", "phone", "email")


def executable_source(module: Any) -> str:
    """A module's code with every docstring removed.

    Docstrings *describe* the boundary and therefore name the very things the
    code must not do; only what actually runs is searched.
    """
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def test_the_time_page_has_no_way_to_choose_a_time():
    """The boundary as API: this page object offers reading, and nothing else."""
    own = [name for name in vars(TimePage) if not name.startswith("_")]

    assert "available_slots" in own
    assert sorted(own) == ["READY", "SLOT", "available_slots", "wait_until_ready"]
    for name in own:
        assert not any(re.search(rf"\b{word}\b", name.lower()) for word in FORBIDDEN)


def test_the_time_page_never_clicks():
    """It cannot click the wrong thing if it does not click at all."""
    assert ".click(" not in executable_source(time_page_module)


def test_the_scanner_contains_no_booking_call():
    """Read what actually runs: nothing in it can select a time or submit a form."""
    code = executable_source(availability_module).lower()

    for forbidden in FORBIDDEN:
        assert not re.search(rf"\b{forbidden}\b", code), forbidden
    # It reads the times and picks dates; that is the whole of its reach.
    assert "select_date" in code
    assert "available_slots" in code


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_the_report_lists_centres_dates_and_times():
    rendered = render_availability(
        [
            CentreAvailability(
                centre_id="3242",
                centre_name="ТСЦ МВС №3242",
                dates=(
                    DateAvailability(
                        AUG_21,
                        (TimeSlot(time(9, 20), "09:20"), TimeSlot(time(10, 40), "10:40")),
                    ),
                    DateAvailability(AUG_26, (TimeSlot(time(14, 0), "14:00"),)),
                ),
            ),
            CentreAvailability(centre_id="4641", centre_name="ТСЦ МВС №4641"),
        ]
    )

    assert "AVAILABILITY" in rendered
    assert "ТСЦ МВС №3242" in rendered
    assert "  2026-08-21" in rendered
    assert "    09:20" in rendered
    assert "    10:40" in rendered
    assert "  2026-08-26" in rendered
    assert "    14:00" in rendered
    assert "ТСЦ МВС №4641" in rendered
    assert "1 of 2 centre(s) have at least one free time." in rendered


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            CentreAvailability.missing(centre_id="3242", centre_name="ТСЦ"),
            "not on the service-centre screen",
        ),
        (
            CentreAvailability.unavailable(centre_id="3242", centre_name="ТСЦ"),
            "unavailable — the centre's button is disabled",
        ),
        (
            CentreAvailability(centre_id="3242", centre_name="ТСЦ"),
            "no available dates",
        ),
        (
            CentreAvailability(
                centre_id="3242",
                centre_name="ТСЦ",
                dates=(DateAvailability(AUG_21),),
            ),
            "no available times",
        ),
    ],
)
def test_the_report_distinguishes_every_kind_of_nothing(result, expected):
    assert expected in render_availability([result])


def test_a_failed_centre_says_so_without_a_traceback():
    rendered = render_availability(
        [CentreAvailability(centre_id="3242", centre_name="ТСЦ",
                            error="Timed out waiting for the calendar screen\nmore")]
    )
    assert "scan failed: Timed out waiting for the calendar screen" in rendered
    assert "more" not in rendered


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


async def test_check_availability_prints_the_report(tmp_path, capsys):
    page = wizard(
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20", "10:40")},
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_check_availability(config, ctx)

    assert code == 0
    out = capsys.readouterr().out
    assert "Scanning 1 service centre(s): 3242" in out
    assert "AVAILABILITY" in out
    assert "2026-08-21" in out
    assert "09:20" in out
    assert "Nothing was booked" in out


async def test_check_availability_honours_center_overrides(tmp_path, capsys):
    page = wizard(
        centres=[CENTRE_3242, CENTRE_4641],
        months={"Серпень 2026": [(21, True)]},
        slots={AUG_21: slots_for("09:20")},
    )
    config = build_config(tmp_path, [CENTRE_3242, CENTRE_4641])
    ctx = build_context(config, page)

    code = await run_check_availability(config, ctx, centers=["4641"])

    assert code == 0
    assert "Scanning 1 service centre(s): 4641" in capsys.readouterr().out
    assert "click:centre:4641" in page.actions
    assert "click:centre:3242" not in page.actions


async def test_check_availability_rejects_too_many_centres(tmp_path, capsys):
    six = centres(6)
    config = build_config(tmp_path, six)
    page = wizard(centres=six)

    code = await run_check_availability(config, build_context(config, page))

    assert code == 2
    assert "at most 5" in capsys.readouterr().err
    # Nothing was driven: it is a configuration answer, not a scan.
    assert page.actions == []


async def test_check_availability_rejects_an_empty_centre_list(tmp_path, capsys):
    off = [ServiceCenter(name="ТСЦ МВС № 3242", id="3242", enabled=False)]
    config = build_config(tmp_path, off)
    page = wizard()

    code = await run_check_availability(config, build_context(config, page))

    assert code == 2
    assert "No service centre to scan" in capsys.readouterr().err
    assert page.actions == []


async def test_an_unconfigured_calendar_selector_stops_the_run_once(tmp_path, capsys):
    """calendar.month ships as TODO: say so once, not once per centre."""
    page = wizard(centres=[CENTRE_3242, CENTRE_4641])
    config = build_config(
        tmp_path,
        [CENTRE_3242, CENTRE_4641],
        selectors_yaml=SELECTORS.replace('value: ".month"', 'value: "TODO"'),
    )

    code = await run_check_availability(config, build_context(config, page))

    assert code == 2
    err = capsys.readouterr().err
    assert err.count("calendar.month has not been configured") == 1
    assert "inspect" in err  # it says how to fix it
    assert "AVAILABILITY" not in capsys.readouterr().out


async def test_check_availability_books_nothing_end_to_end(tmp_path, capsys):
    page = wizard(slots={day: slots_for("09:20", "10:40") for day in OPEN_DATES})
    config = build_config(tmp_path)

    code = await run_check_availability(config, build_context(config, page))

    assert code == 0
    assert "contacts" not in page.screens_visited
    assert not [action for action in page.actions if action.startswith("click:slot:")]
    assert "Nothing was booked" in capsys.readouterr().out
