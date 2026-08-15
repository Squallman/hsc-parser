"""Single service centre lookup: search, identity matching, availability, click.

Nothing here touches the real site. ``DepartmentScreen`` stands in for the
service-centre screen and reproduces the one Playwright behaviour that matters
for this feature: ``get_by_role(name=..., exact=False)`` matches an accessible
name by substring, which is why identity still has to be checked in Python.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
import yaml
from conftest import FakeElement, FakeLocator, FakePage

from hsc_queue_monitor.browser.diagnostics import Diagnostics
from hsc_queue_monitor.cli import run_check_center
from hsc_queue_monitor.config import (
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SelectorRegistry,
    find_service_center,
    load_secrets,
    load_service_centers,
)
from hsc_queue_monitor.flow.steps import FlowContext, get_step
from hsc_queue_monitor.models import (
    ConfigError,
    DepartmentAmbiguous,
    DepartmentNotFound,
    DepartmentUnavailable,
    FlowError,
    ServiceCenter,
    identifies_service_center,
)

CABINET = "https://eqn.hsc.gov.ua/cabinet"
SEARCH_PLACEHOLDER = "Пошук сервісного центру МВС"

CENTER_3242 = ServiceCenter(
    name="ТСЦ МВС № 3242",
    id="3242",
    full_name="ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20",
    enabled=True,
)
FULL_TEXT_3242 = "ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20"

PREREQUISITES = (
    "queue.start_registration",
    "exam.practical_exam",
    "exam.service_center_vehicle",
    "category.category_a",
)

#: Accessible name -> the prerequisite it stands for.
PREREQUISITE_NAMES = {
    "Записатись у чергу": "queue.start_registration",
    "Практичний іспит": "exam.practical_exam",
    "Практичний іспит на транспортному засобі Сервісного центру МВС":
        "exam.service_center_vehicle",
    "категорія А (механична КПП)": "category.category_a",
}

SELECTORS = """
queue:
  start_registration:
    strategy: role
    role: link
    name: "Записатись у чергу"
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
    value: "Пошук сервісного центру МВС"
    exact: true
  department_card:
    strategy: role
    role: button
    name: DYNAMIC
    exact: false
    multiple: true
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


class LazySearchLocator(FakeLocator):
    """A search-box locator that re-evaluates on every poll, as a real one does.

    :class:`FakeLocator` captures its matches when it is built, which is the one
    thing that cannot reproduce a screen arriving late — a poll would keep
    re-reading a snapshot taken before the transition. Here each ``count()`` is
    one look at the page, and the box appears once enough of them have gone by.
    """

    def __init__(self, screen: DepartmentScreen, ready: list[FakeElement]) -> None:
        super().__init__([])
        self._screen = screen
        self._ready = ready

    async def count(self) -> int:
        self._screen.search_polls += 1
        if self._screen.search_polls > self._screen.search_appears_after:
            self._elements = self._ready
        return len(self._elements)


class DepartmentScreen(FakePage):
    """The service-centre screen: prerequisite controls plus centre buttons.

    ``search_appears_after`` reproduces the live race: HSC keeps the category
    screen up behind its loading spinner, so for the first few looks the search
    box is simply not in the DOM yet.
    """

    def __init__(
        self,
        centers: list[FakeElement],
        *,
        prerequisites: bool = True,
        search_field: FakeElement | None = None,
        search_appears_after: int = 0,
    ) -> None:
        matches: dict[str, list[FakeElement]] = {
            SEARCH_PLACEHOLDER: [search_field or FakeElement(tag="input", text="")]
        }
        if prerequisites:
            for name, key in PREREQUISITE_NAMES.items():
                matches[name] = [FakeElement(marker=key, text=name)]
        super().__init__(matches=matches)
        self.centers = centers
        self.search_appears_after = search_appears_after
        #: Times the page has been *looked at* for the search box. One poll is
        #: one look, so this proves waiting happened without timing anything.
        self.search_polls = 0

    def get_by_placeholder(self, value: str, **kwargs: Any) -> FakeLocator:
        if value != SEARCH_PLACEHOLDER:
            return super().get_by_placeholder(value, **kwargs)
        self.calls.append(("get_by_placeholder", (value,), kwargs))
        return LazySearchLocator(self, (self._matches or {})[SEARCH_PLACEHOLDER])

    def get_by_role(self, role: str, **kwargs: Any) -> FakeLocator:
        # exact=False is how department.department_card is configured; emulate
        # Playwright's substring match on the accessible name.
        if kwargs.get("exact") is False:
            self.calls.append(("get_by_role", (role,), kwargs))
            term = str(kwargs.get("name") or "")
            return FakeLocator(
                [c for c in self.centers if term in str(c.attrs.get("text", ""))]
            )
        return super().get_by_role(role, **kwargs)

    @property
    def search_element(self) -> FakeElement:
        return (self._matches or {})[SEARCH_PLACEHOLDER][0]


def center_button(text: str, *, disabled: bool = False) -> FakeElement:
    return FakeElement(tag="button", text=text, disabled=disabled)


def build_config(
    tmp_path: Any,
    centers: list[ServiceCenter] | None = None,
    selectors_yaml: str = SELECTORS,
    *,
    navigation_ms: int = 200,
) -> AppConfig:
    """``navigation_ms`` is what the category → service-centre wait is given."""
    return AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        app=AppSettings(),
        paths=Paths(data_dir=tmp_path),
        selectors=SelectorRegistry.from_dict(yaml.safe_load(selectors_yaml)),
        flow=FlowConfig.from_dict(
            yaml.safe_load(FLOW.replace("navigation: 200", f"navigation: {navigation_ms}"))
        ),
        service_centers=[CENTER_3242] if centers is None else centers,
    )


def build_context(config: AppConfig, page: FakePage, tmp_path: Any = None) -> FlowContext:
    diagnostics = (
        Diagnostics(tmp_path / "debug", enabled=True) if tmp_path is not None else None
    )
    return FlowContext(config=config, page=page, diagnostics=diagnostics)


def department(config: AppConfig, page: FakePage) -> Any:
    return build_context(config, page).department


def call_index(page: FakePage, api: str, value: str) -> int:
    """Position of the first call of *api* made with *value*. -1 if never."""
    for index, (called, args, kwargs) in enumerate(page.calls):
        if called != api:
            continue
        if value in args or kwargs.get("name") == value:
            return index
    return -1


def role_lookups(page: FakePage) -> list[str]:
    """Accessible names looked up, in order."""
    return [
        str(kwargs.get("name"))
        for api, _args, kwargs in page.calls
        if api == "get_by_role"
    ]


# --------------------------------------------------------------------------- #
# Identity matching (pure)
# --------------------------------------------------------------------------- #


def test_id_matches_the_center_label():
    assert identifies_service_center(FULL_TEXT_3242, "3242")
    assert identifies_service_center("ТСЦ МВС № 3242", "3242")


def test_id_does_not_match_a_longer_number():
    assert not identifies_service_center("ТСЦ МВС № 13242 м. Київ", "3242")
    assert not identifies_service_center("ТСЦ МВС № 32421 м. Київ", "3242")
    assert not identifies_service_center("ТСЦ МВС № 8041 м. Київ", "3242")


def test_identity_survives_a_reworded_address():
    """Only the ID is the identity — the address is display text."""
    assert identifies_service_center("ТСЦ МВС № 3242, Біла Церква (нова адреса)", "3242")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


async def test_search_field_receives_the_service_center_id(tmp_path):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    await department(build_config(tmp_path), page).search_department("3242")

    assert page.search_element.attrs["fills"][-1] == "3242"
    assert ("get_by_placeholder", (SEARCH_PLACEHOLDER,), {"exact": True}) in page.calls


async def test_search_clears_existing_content_first(tmp_path):
    """A stale filter from an earlier check must not hide the wanted centre."""
    stale = FakeElement(tag="input", text="", value="8041")
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_field=stale)

    await department(build_config(tmp_path), page).search_department("3242")

    assert page.search_element.attrs["fills"] == ["", "3242"]


async def test_search_then_find_locates_the_center(tmp_path):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    dept = department(build_config(tmp_path), page)

    await dept.search_department("3242")
    button = await dept.find_department_button("3242")

    assert await button.inner_text() == FULL_TEXT_3242


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


async def test_the_exact_center_is_picked_out_of_several(tmp_path):
    page = DepartmentScreen(
        [
            center_button("ТСЦ МВС № 8041 м. Київ, вул. Лугова 19"),
            center_button(FULL_TEXT_3242),
        ]
    )
    button = await department(build_config(tmp_path), page).find_department_button("3242")
    assert await button.inner_text() == FULL_TEXT_3242


async def test_a_longer_number_is_not_mistaken_for_the_center(tmp_path):
    """`3242` must not resolve to `13242`, even though it is a substring of it."""
    page = DepartmentScreen(
        [
            center_button("ТСЦ МВС № 13242 м. Львів, вул. Наукова 1"),
            center_button(FULL_TEXT_3242),
        ]
    )
    button = await department(build_config(tmp_path), page).find_department_button("3242")
    assert await button.inner_text() == FULL_TEXT_3242


async def test_only_a_longer_number_on_screen_is_not_found(tmp_path):
    page = DepartmentScreen([center_button("ТСЦ МВС № 13242 м. Львів")])

    with pytest.raises(DepartmentNotFound) as exc:
        await department(build_config(tmp_path), page).find_department_button("3242")

    assert "13242" in str(exc.value)  # the near miss is reported, not silently used


async def test_zero_results_raise_a_useful_error(tmp_path):
    page = DepartmentScreen([])

    with pytest.raises(DepartmentNotFound) as exc:
        await department(build_config(tmp_path), page).find_department_button("3242")

    message = str(exc.value)
    assert "3242" in message
    assert "service_centers.yaml" in message


async def test_two_matching_centers_raise_an_ambiguity_error(tmp_path):
    page = DepartmentScreen(
        [center_button(FULL_TEXT_3242), center_button("ТСЦ МВС № 3242 (тимчасовий)")]
    )

    with pytest.raises(DepartmentAmbiguous) as exc:
        await department(build_config(tmp_path), page).find_department_button("3242")

    assert "matched 2 visible buttons" in str(exc.value)


async def test_ambiguity_clicks_nothing(tmp_path):
    buttons = [center_button(FULL_TEXT_3242), center_button("ТСЦ МВС № 3242 (тимчасовий)")]
    page = DepartmentScreen(buttons)

    with pytest.raises(DepartmentAmbiguous):
        await department(build_config(tmp_path), page).select_department("3242")

    assert not any(b.attrs.get("clicked") for b in buttons)


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


async def test_enabled_button_is_available(tmp_path):
    page = DepartmentScreen([center_button(FULL_TEXT_3242, disabled=False)])

    availability = await department(build_config(tmp_path), page).get_department_availability(
        "3242", name="ТСЦ МВС № 3242"
    )

    assert availability.found is True
    assert availability.disabled is False
    assert availability.available is True
    assert availability.name == "ТСЦ МВС № 3242"
    assert availability.full_text == FULL_TEXT_3242  # the whole label is captured


async def test_disabled_button_is_unavailable(tmp_path):
    page = DepartmentScreen([center_button(FULL_TEXT_3242, disabled=True)])

    availability = await department(build_config(tmp_path), page).get_department_availability(
        "3242"
    )

    assert availability.found is True
    assert availability.disabled is True
    assert availability.available is False


async def test_availability_never_clicks(tmp_path):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button])

    await department(build_config(tmp_path), page).get_department_availability("3242")

    assert not button.attrs.get("clicked")


async def test_missing_center_is_reported_not_raised(tmp_path):
    page = DepartmentScreen([center_button("ТСЦ МВС № 8041 м. Київ")])

    availability = await department(build_config(tmp_path), page).get_department_availability(
        "3242", name="ТСЦ МВС № 3242"
    )

    assert availability.found is False
    assert availability.available is False
    assert availability.full_text == ""


# --------------------------------------------------------------------------- #
# Click safety
# --------------------------------------------------------------------------- #


async def test_select_clicks_an_enabled_center(tmp_path):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button])

    await department(build_config(tmp_path), page).select_department("3242")

    assert button.attrs.get("clicked") is True


async def test_select_refuses_to_force_click_a_disabled_center(tmp_path):
    button = center_button(FULL_TEXT_3242, disabled=True)
    page = DepartmentScreen([button])

    with pytest.raises(DepartmentUnavailable) as exc:
        await department(build_config(tmp_path), page).select_department("3242")

    assert not button.attrs.get("clicked")
    message = str(exc.value)
    assert "3242" in message
    assert "never force-clicked" in message


# --------------------------------------------------------------------------- #
# check-center: flow
# --------------------------------------------------------------------------- #


async def test_check_center_runs_exactly_the_configured_prerequisites(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    out = capsys.readouterr().out
    for number, key in enumerate(PREREQUISITES, start=1):
        assert f"PREPARE {number}/4: {key}" in out
    # Exactly four: no exam.continue / category.continue / department.continue.
    assert "PREPARE 5/" not in out
    assert "continue" not in out

    # The four prerequisites were clicked in order, before the search box.
    assert role_lookups(page)[:4] == list(PREREQUISITE_NAMES)
    apis = [api for api, _a, _k in page.calls]
    assert apis.index("get_by_placeholder") > apis.index("get_by_role")


async def test_check_center_starts_at_the_cabinet(tmp_path):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    await run_check_center(config, ctx, service_center_id="3242")

    assert page.navigations == [CABINET]


# --------------------------------------------------------------------------- #
# The category → service-centre transition
# --------------------------------------------------------------------------- #
#
# Observed live: the click on category.category_a returned while HSC was still
# showing its spinner, department.search was not in the DOM yet, and the next
# operation reported it as "matched 0 visible elements" — a state-transition
# race dressed up as a broken selector.

CATEGORY_A_NAME = "категорія А (механична КПП)"


async def test_the_category_click_comes_first_and_the_screen_is_then_awaited(
    tmp_path, caplog
):
    """The flow / monitor path: click, then wait for where the click leads."""
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_appears_after=2)
    config = build_config(tmp_path, navigation_ms=3_000)
    ctx = build_context(config, page)

    with caplog.at_level(logging.INFO):
        await get_step("category_a").action(ctx)

    assert page._matches[CATEGORY_A_NAME][0].attrs.get("clicked") is True
    # Click first, wait second — never the other way round.
    assert call_index(page, "get_by_role", CATEGORY_A_NAME) < call_index(
        page, "get_by_placeholder", SEARCH_PLACEHOLDER
    )
    positions = [
        caplog.text.index("Clicking category.category_a"),
        caplog.text.index("Waiting for the service-centre screen"),
        caplog.text.index("Service-centre screen ready"),
    ]
    assert positions == sorted(positions)


async def test_a_screen_that_arrives_late_is_waited_for_not_failed_on(tmp_path, capsys):
    """The exact reported failure, reproduced and then survived."""
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_appears_after=3)
    config = build_config(tmp_path, navigation_ms=3_000)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    assert "Available:  YES" in capsys.readouterr().out
    # It really did have to poll: the box was absent for the first three looks.
    assert page.search_polls > 3
    assert page.search_element.attrs["fills"] == ["", "3242"]


async def test_readiness_is_polled_and_returns_the_moment_it_is_ready(tmp_path):
    """No fixed sleep: a screen already there costs one lookup, not a delay."""
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    config = build_config(tmp_path, navigation_ms=30_000)
    ctx = build_context(config, page)

    started = time.monotonic()
    await ctx.department.wait_until_ready()
    elapsed = time.monotonic() - started

    assert page.search_polls == 1
    assert elapsed < 0.25, f"waited {elapsed:.2f}s out of a 30s budget"


async def test_the_transition_budget_is_the_configured_navigation_timeout(tmp_path):
    """Not the per-locator timeout: this is a screen change, not a slow element."""
    config = build_config(tmp_path, navigation_ms=30_000)
    ctx = build_context(config, DepartmentScreen([center_button(FULL_TEXT_3242)]))

    assert ctx.department.transition_timeout == 30_000
    assert config.flow.timeouts.navigation == 30_000
    assert ctx.department.default_timeout == config.flow.timeouts.default_locator


async def test_the_wait_uses_the_search_selector_from_the_registry(tmp_path):
    """Nothing in flow code repeats the placeholder text."""
    other = "Інший пошук сервісного центру"
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    page._matches[other] = [FakeElement(tag="input", text="")]
    config = build_config(
        tmp_path,
        selectors_yaml=SELECTORS.replace(SEARCH_PLACEHOLDER, other),
        navigation_ms=1_000,
    )
    ctx = build_context(config, page)

    await ctx.department.wait_until_ready()

    assert ("get_by_placeholder", (other,), {"exact": True}) in page.calls
    assert not [
        args for api, args, _k in page.calls
        if api == "get_by_placeholder" and args == (SEARCH_PLACEHOLDER,)
    ]


async def test_a_screen_that_never_arrives_fails_as_a_transition(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_appears_after=10_000)
    config = build_config(tmp_path)  # navigation: 200ms
    ctx = build_context(config, page, tmp_path)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 1
    err = capsys.readouterr().err
    assert "FlowError" in err
    assert "Timed out waiting for the service-centre screen" in err
    assert "category A" in err
    # It is not reported as if the selector were wrong.
    assert "matched 0 visible elements" not in err
    assert "department.search never became visible" in err
    # Nothing was typed into a box that was not on screen.
    assert "fills" not in page.search_element.attrs


async def test_a_transition_timeout_saves_and_names_diagnostics(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_appears_after=10_000)
    config = build_config(tmp_path)
    ctx = build_context(config, page, tmp_path)

    assert await run_check_center(config, ctx, service_center_id="3242") == 1

    err = capsys.readouterr().err
    debug = tmp_path / "debug"
    shots = list(debug.glob("*department-screen-timeout.png"))
    dumps = list(debug.glob("*department-screen-timeout-elements.json"))
    assert shots and dumps
    assert str(shots[0]) in err
    assert str(dumps[0]) in err


async def test_check_center_without_click_is_unchanged_by_the_wait(tmp_path, capsys):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button], search_appears_after=1)
    config = build_config(tmp_path, navigation_ms=3_000)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    out = capsys.readouterr().out
    for number, key in enumerate(PREREQUISITES, start=1):
        assert f"PREPARE {number}/4: {key}" in out
    assert "Available:  YES" in out
    assert "Not clicking. Pass --click" in out
    assert not button.attrs.get("clicked")


async def test_check_center_with_click_takes_the_same_preparation_path(
    tmp_path, capsys, caplog
):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button], search_appears_after=1)
    config = build_config(tmp_path, navigation_ms=3_000)
    ctx = build_context(config, page)

    with caplog.at_level(logging.INFO):
        code = await run_check_center(config, ctx, service_center_id="3242", click=True)

    assert code == 0
    out = capsys.readouterr().out
    for number, key in enumerate(PREREQUISITES, start=1):
        assert f"PREPARE {number}/4: {key}" in out
    # The same wait, on the same path — --click only changes what happens after.
    assert "Waiting for the service-centre screen" in caplog.text
    assert "Service-centre screen ready" in caplog.text
    assert button.attrs.get("clicked") is True
    assert "Stopped after service-center selection." in out


async def test_the_wait_can_be_given_its_own_timeout(tmp_path):
    """The budget is a parameter, so a caller is never stuck with a default."""
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], search_appears_after=10_000)
    ctx = build_context(build_config(tmp_path, navigation_ms=30_000), page)

    with pytest.raises(FlowError, match="service-centre screen"):
        await ctx.department.wait_until_ready(timeout=200)


# --------------------------------------------------------------------------- #
# check-center: reporting and click policy
# --------------------------------------------------------------------------- #


async def test_check_center_reports_an_available_center(tmp_path, capsys):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "SERVICE CENTER CHECK" in out
    assert "ID:         3242" in out
    assert "Name:       ТСЦ МВС № 3242" in out
    assert "Found:      yes" in out
    assert "Disabled:   false" in out
    assert "Available:  YES" in out
    assert FULL_TEXT_3242 in out


async def test_check_center_reports_an_unavailable_center_with_exit_code_zero(
    tmp_path, capsys
):
    """Unavailability is an observation, not a process failure."""
    page = DepartmentScreen([center_button(FULL_TEXT_3242, disabled=True)])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "Disabled:   true" in out
    assert "Available:  NO" in out


async def test_check_center_does_not_click_by_default(tmp_path, capsys):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 0
    assert not button.attrs.get("clicked")
    assert "Not clicking" in capsys.readouterr().out


async def test_check_center_click_selects_an_available_center(tmp_path, capsys):
    button = center_button(FULL_TEXT_3242)
    page = DepartmentScreen([button])
    config = build_config(tmp_path)
    ctx = build_context(config, page, tmp_path)

    code = await run_check_center(config, ctx, service_center_id="3242", click=True)

    assert code == 0
    assert button.attrs.get("clicked") is True
    out = capsys.readouterr().out
    assert "Clicking service center 3242" in out
    assert "URL after click:" in out
    assert "Stopped after service-center selection." in out
    assert "No date or time was selected." in out


async def test_check_center_click_saves_uniquely_named_diagnostics(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    config = build_config(tmp_path)
    ctx = build_context(config, page, tmp_path)

    await run_check_center(config, ctx, service_center_id="3242", click=True)

    debug = tmp_path / "debug"
    shots = list(debug.glob("*check-center-3242-after-click.png"))
    dumps = list(debug.glob("check-center-3242-after-click-elements.json"))
    assert len(shots) == 1
    assert len(dumps) == 1
    # The generic dump must not be the only artifact left behind.
    assert not (debug / "page-elements.json").exists()

    out = capsys.readouterr().out
    assert shots[0].name in out
    assert dumps[0].name in out


async def test_check_center_click_does_not_click_a_disabled_center(tmp_path, capsys):
    button = center_button(FULL_TEXT_3242, disabled=True)
    page = DepartmentScreen([button])
    config = build_config(tmp_path)
    ctx = build_context(config, page, tmp_path)

    code = await run_check_center(config, ctx, service_center_id="3242", click=True)

    assert code == 0
    assert not button.attrs.get("clicked")
    out = capsys.readouterr().out
    assert "Not clicking: service centre 3242 is disabled" in out
    assert "Clicking service center" not in out


# --------------------------------------------------------------------------- #
# check-center: exit codes
# --------------------------------------------------------------------------- #


async def test_unknown_center_id_is_a_configuration_error(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="9999"
    )

    assert code == 2
    assert page.navigations == []  # nothing was opened
    err = capsys.readouterr().err
    assert "Unknown service centre '9999'" in err
    assert "3242" in err  # the known IDs are listed


async def test_unconfigured_search_selector_is_a_configuration_error(tmp_path, capsys):
    selectors = SELECTORS.replace(f'value: "{SEARCH_PLACEHOLDER}"', 'value: "TODO"')
    config = build_config(tmp_path, selectors_yaml=selectors)
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 2
    assert page.navigations == []
    assert "department.search has not been configured" in capsys.readouterr().err


async def test_a_broken_prerequisite_is_a_runtime_failure(tmp_path, capsys):
    page = DepartmentScreen([center_button(FULL_TEXT_3242)], prerequisites=False)
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 1
    assert "queue.start_registration" in capsys.readouterr().err


async def test_a_center_missing_from_the_screen_is_a_runtime_failure(tmp_path, capsys):
    page = DepartmentScreen([center_button("ТСЦ МВС № 8041 м. Київ")])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Found:      no" in out
    assert "Available:  NO" in out
    # Nothing matched "3242", so the operator is told what to look at next.
    assert "Nothing matched it at all" in out


async def test_a_near_miss_lists_what_was_on_screen(tmp_path, capsys):
    """A centre whose label merely contains the ID is reported, not selected."""
    page = DepartmentScreen([center_button("ТСЦ МВС № 13242 м. Львів, вул. Наукова 1")])
    config = build_config(tmp_path)

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Buttons that did match the search term:" in out
    assert "ТСЦ МВС № 13242 м. Львів, вул. Наукова 1" in out


async def test_a_disabled_center_in_config_can_still_be_checked(tmp_path, capsys):
    config = build_config(
        tmp_path, centers=[ServiceCenter(name="ТСЦ МВС № 3242", id="3242", enabled=False)]
    )
    page = DepartmentScreen([center_button(FULL_TEXT_3242)])

    code = await run_check_center(
        config, build_context(config, page), service_center_id="3242"
    )

    assert code == 0
    assert "disabled in config/service_centers.yaml" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_shipped_service_centers_carry_ids(config_dir):
    """The catalogue is discovered, so its contents move; its shape does not."""
    centers = load_service_centers(config_dir / "service_centers.yaml")
    by_id = {c.id: c for c in centers}

    assert centers, "the shipped catalogue is empty"
    assert all(c.id and c.id.isdigit() for c in centers)
    assert all(c.search_term == c.id for c in centers)
    # 3242 is the centre this project was built around, and it is monitored.
    assert "3242" in by_id
    assert by_id["3242"].enabled
    assert by_id["3242"].full_name.startswith("ТСЦ МВС № 3242")
    # Discovery adds centres disabled, so only chosen ones are ever scanned.
    assert sum(c.enabled for c in centers) < len(centers)


def test_a_center_without_an_id_is_rejected(tmp_path):
    path = tmp_path / "service_centers.yaml"
    path.write_text('service_centers:\n  - name: "ТСЦ МВС № 3242"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="needs an `id:`"):
        load_service_centers(path)


def test_duplicate_center_ids_are_rejected(tmp_path):
    path = tmp_path / "service_centers.yaml"
    path.write_text(
        'service_centers:\n  - id: "3242"\n    name: "A"\n  - id: "3242"\n    name: "B"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate service centre id"):
        load_service_centers(path)


def test_a_second_center_needs_no_code_change(tmp_path):
    """Adding centres is configuration only."""
    path = tmp_path / "service_centers.yaml"
    path.write_text(
        'service_centers:\n'
        '  - id: "3242"\n    name: "ТСЦ МВС № 3242"\n'
        '  - id: "8041"\n    name: "ТСЦ МВС № 8041"\n    enabled: false\n',
        encoding="utf-8",
    )
    centers = load_service_centers(path)

    assert [c.id for c in centers] == ["3242", "8041"]
    assert find_service_center(centers, "8041").name == "ТСЦ МВС № 8041"
    assert find_service_center(centers, "ТСЦ МВС № 3242").id == "3242"


def test_find_service_center_lists_the_known_ids():
    with pytest.raises(ConfigError, match="3242"):
        find_service_center([CENTER_3242], "9999")
