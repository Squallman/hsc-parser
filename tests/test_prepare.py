"""Prerequisite-aware ``test-step`` preparation.

Covers the navigation chain executed before a target selector is validated, and
the rule that the target itself is only ever clicked with ``--click``, after it
has validated.
"""

from __future__ import annotations

import pytest
import yaml
from conftest import FakeElement, FakePage

from hsc_queue_monitor.browser.diagnostics import Diagnostics
from hsc_queue_monitor.cli import run_test_step
from hsc_queue_monitor.config import (
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SelectorRegistry,
    load_secrets,
)
from hsc_queue_monitor.flow.engine import FlowEngine
from hsc_queue_monitor.flow.steps import FlowContext
from hsc_queue_monitor.models import ConfigError, FlowError, LocatorNotFound, ServiceCenter

CABINET = "https://eqn.hsc.gov.ua/cabinet"

SELECTORS = """
queue:
  start_registration:
    strategy: role
    role: link
    name: "Записатись у чергу"
exam:
  practical_exam:
    strategy: role
    role: button
    name: "Практичний іспит"
category:
  category_a:
    strategy: text
    value: "Категорія A"
department:
  department_card:
    strategy: text
    value: DYNAMIC
calendar:
  available_slot:
    strategy: css
    value: ".slot.free"
    multiple: true
"""

FLOW = f"""
site:
  queue_url: "https://eqn.hsc.gov.ua/cabinet/queue"
  cabinet_url: "{CABINET}"
timeouts:
  default_locator: 200
  navigation: 200
debug:
  screenshots: false
steps:
  queue.start_registration:
    start_url: "{CABINET}"
    prerequisites: []
  exam.practical_exam:
    start_url: "{CABINET}"
    prerequisites:
      - queue.start_registration
  category.category_a:
    start_url: "{CABINET}"
    prerequisites:
      - queue.start_registration
      - exam.practical_exam
  department.department_card:
    prerequisites:
      - queue.start_registration
"""


def build_config(tmp_path, flow_yaml: str = FLOW, selectors_yaml: str = SELECTORS) -> AppConfig:
    return AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        app=AppSettings(),
        paths=Paths(data_dir=tmp_path),
        selectors=SelectorRegistry.from_dict(yaml.safe_load(selectors_yaml)),
        flow=FlowConfig.from_dict(yaml.safe_load(flow_yaml)),
        service_centers=[],
    )


def build_context(config: AppConfig, page: FakePage, tmp_path=None) -> FlowContext:
    diagnostics = (
        Diagnostics(tmp_path / "debug", enabled=True) if tmp_path is not None else None
    )
    return FlowContext(config=config, page=page, diagnostics=diagnostics)


def elements(**by_name: list[FakeElement]) -> FakePage:
    """A page where each accessible name / text matches its own element list."""
    return FakePage(matches=dict(by_name))


def clicked(page: FakePage, key: str) -> bool:
    for element_list in (page._matches or {}).values():
        for element in element_list:
            if element.attrs.get("marker") == key and element.attrs.get("clicked"):
                return True
    return False


def marker(key: str, **attrs) -> FakeElement:
    return FakeElement(marker=key, **attrs)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_plan_is_read_from_flow_yaml(tmp_path):
    flow = build_config(tmp_path).flow
    plan = flow.plan_for("category.category_a")
    assert plan.start_url == CABINET
    assert plan.prerequisites == ("queue.start_registration", "exam.practical_exam")
    assert plan.has_prerequisites


def test_unlisted_selector_gets_an_empty_plan(tmp_path):
    plan = build_config(tmp_path).flow.plan_for("login.password")
    assert plan.prerequisites == ()
    assert plan.has_prerequisites is False


def test_start_url_falls_back_to_the_cabinet_url(tmp_path):
    flow = build_config(tmp_path).flow
    # department.department_card declares no start_url of its own.
    assert flow.start_url_for("department.department_card") == CABINET
    assert flow.start_url_for("login.password") == CABINET


# --------------------------------------------------------------------------- #
# /cabinet/queue is reached by clicking, never by navigating
# --------------------------------------------------------------------------- #

QUEUE_URL = "https://eqn.hsc.gov.ua/cabinet/queue"


async def test_open_queue_opens_the_cabinet_not_the_queue_url(tmp_path):
    from hsc_queue_monitor.flow.steps import get_step

    page = FakePage()
    ctx = build_context(build_config(tmp_path), page)

    await get_step("open_queue").action(ctx)

    assert page.navigations == [CABINET]


async def test_the_queue_screen_is_only_ever_reached_by_clicking(tmp_path):
    """Walking the whole configured chain must never navigate to /cabinet/queue."""
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("exam.practical_exam")],
            "Категорія A": [marker("category.category_a")],
        }
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    await run_test_step(config, ctx, selector="category.category_a", prompt=never_prompt)

    assert page.navigations == [CABINET]
    assert QUEUE_URL not in page.navigations
    assert clicked(page, "queue.start_registration")


async def test_manual_prepare_also_starts_at_the_cabinet(tmp_path):
    page = elements(**{"Практичний іспит": [marker("exam.practical_exam")]})
    page.url = "about:blank"
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    async def accept(_message: str) -> str:
        return ""

    await run_test_step(
        config, ctx, selector="exam.practical_exam", manual_prepare=True, prompt=accept
    )
    assert page.navigations == [CABINET]


def test_start_url_never_falls_back_to_the_queue_url():
    flow = FlowConfig.from_dict(
        {"site": {"queue_url": QUEUE_URL, "cabinet_url": CABINET}}
    )
    assert flow.queue_url == QUEUE_URL  # still readable as reference
    assert flow.start_url_for("anything.at.all") == CABINET


def test_self_referencing_prerequisite_is_rejected():
    with pytest.raises(ConfigError, match="lists itself as a prerequisite"):
        FlowConfig.from_dict(
            {"steps": {"exam.practical_exam": {"prerequisites": ["exam.practical_exam"]}}}
        )


def test_prerequisites_must_be_a_list_of_strings():
    with pytest.raises(ConfigError, match="list of selector keys"):
        FlowConfig.from_dict({"steps": {"a.b": {"prerequisites": [{"key": "c.d"}]}}})


def test_unknown_step_option_is_rejected():
    with pytest.raises(ConfigError, match="unknown option"):
        FlowConfig.from_dict({"steps": {"a.b": {"prerequisite": []}}})


def test_shipped_flow_file_defines_prerequisite_chains(config_dir):
    """The chains that ship with the repo must reference real selectors."""
    flow = FlowConfig.from_file(config_dir / "flow.yaml")
    selectors = SelectorRegistry.from_file(config_dir / "selectors.yaml")

    assert flow.steps, "flow.yaml should ship prerequisite chains"
    for key, plan in flow.steps.items():
        assert key in selectors, f"steps.{key} is not a known selector"
        for prerequisite in plan.prerequisites:
            assert prerequisite in selectors, f"{key} requires unknown {prerequisite}"


# --------------------------------------------------------------------------- #
# Preparation
# --------------------------------------------------------------------------- #


async def test_zero_prerequisites_only_navigates(tmp_path, capsys):
    page = elements(**{"Записатись у чергу": [marker("queue.start_registration")]})
    config = build_config(tmp_path)
    engine = FlowEngine(build_context(config, page), auto=True)

    results = await engine.prepare((), start_url=CABINET)

    assert results == []
    assert page.navigations == [CABINET]
    assert not clicked(page, "queue.start_registration")
    assert "PREPARE" not in capsys.readouterr().out


async def test_one_prerequisite_is_clicked(tmp_path, capsys):
    page = elements(**{"Записатись у чергу": [marker("queue.start_registration")]})
    config = build_config(tmp_path)
    engine = FlowEngine(build_context(config, page), auto=True)

    results = await engine.prepare(("queue.start_registration",), start_url=CABINET)

    assert [r.key for r in results] == ["queue.start_registration"]
    assert clicked(page, "queue.start_registration")

    out = capsys.readouterr().out
    assert "PREPARE 1/1: queue.start_registration" in out
    assert 'Locator: get_by_role("link", name="Записатись у чергу")' in out
    assert "Result: OK" in out
    assert f"URL: {CABINET}" in out


async def test_multiple_prerequisites_run_in_order(tmp_path, capsys):
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("exam.practical_exam")],
        }
    )
    config = build_config(tmp_path)
    engine = FlowEngine(build_context(config, page), auto=True)

    results = await engine.prepare(
        ("queue.start_registration", "exam.practical_exam"), start_url=CABINET
    )

    assert [r.key for r in results] == ["queue.start_registration", "exam.practical_exam"]
    # Locator lookups happened in the configured order.
    lookups = [kwargs.get("name") for api, _, kwargs in page.calls if api == "get_by_role"]
    assert lookups == ["Записатись у чергу", "Практичний іспит"]

    out = capsys.readouterr().out
    assert "PREPARE 1/2: queue.start_registration" in out
    assert "PREPARE 2/2: exam.practical_exam" in out


async def test_prerequisite_failure_stops_and_saves_a_screenshot(tmp_path, capsys):
    page = elements(**{"Записатись у чергу": [], "Практичний іспит": [marker("exam")]})
    config = build_config(tmp_path)
    ctx = build_context(config, page, tmp_path)
    engine = FlowEngine(ctx, auto=True)

    with pytest.raises(LocatorNotFound, match="queue.start_registration"):
        await engine.prepare(
            ("queue.start_registration", "exam.practical_exam"), start_url=CABINET
        )

    out = capsys.readouterr().out
    assert "PREPARE 1/2: queue.start_registration" in out
    assert "Result: FAILED" in out
    assert "The target was never reached" in out
    # The second prerequisite must not have been attempted.
    assert "PREPARE 2/2" not in out
    assert not clicked(page, "exam")

    errors = list((tmp_path / "debug" / "errors").glob("*.png"))
    assert len(errors) == 1, "a failure screenshot should have been captured"


async def test_dynamic_prerequisite_without_a_value_explains_the_flag(tmp_path):
    page = elements(**{"ТСЦ 8041": [marker("department.department_card")]})
    config = build_config(tmp_path)
    engine = FlowEngine(build_context(config, page), auto=True)

    with pytest.raises(FlowError, match="--service-center"):
        await engine.prepare(("department.department_card",), start_url=CABINET)


async def test_dynamic_prerequisite_uses_the_service_center_id(tmp_path):
    """The runtime value is the centre's ID, not its human-readable name."""
    page = elements(**{"8041": [marker("department.department_card")]})
    config = build_config(tmp_path)
    ctx = build_context(config, page)
    ctx.current_service_center = ServiceCenter(name="ТСЦ 8041", id="8041")
    engine = FlowEngine(ctx, auto=True)

    await engine.prepare(("department.department_card",), start_url=CABINET)
    assert clicked(page, "department.department_card")


async def test_prerequisites_route_to_the_owning_page_object(tmp_path):
    ctx = build_context(build_config(tmp_path), FakePage())
    assert ctx.page_object_for("exam.practical_exam") is ctx.exam
    assert ctx.page_object_for("calendar.available_slot") is ctx.calendar
    assert ctx.page_object_for("queue.start_registration") is ctx.queue


# --------------------------------------------------------------------------- #
# test-step end to end (no browser)
# --------------------------------------------------------------------------- #


async def never_prompt(_message: str) -> str:
    raise AssertionError("test-step must not prompt when preparing automatically")


async def test_target_is_validated_but_not_clicked(tmp_path, capsys):
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("exam.practical_exam")],
        }
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_test_step(
        config, ctx, selector="exam.practical_exam", prompt=never_prompt
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "PREPARE 1/1: queue.start_registration" in out
    assert "PASS: selector matched exactly one visible element" in out
    assert "Not clicking" in out
    # The prerequisite was clicked; the target was not.
    assert clicked(page, "queue.start_registration")
    assert not clicked(page, "exam.practical_exam")


async def test_click_interacts_with_the_target_after_it_validates(tmp_path, capsys):
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("exam.practical_exam")],
        }
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_test_step(
        config, ctx, selector="exam.practical_exam", click=True, prompt=never_prompt
    )

    assert code == 0
    assert "PASS:" in capsys.readouterr().out
    assert clicked(page, "exam.practical_exam")


async def test_click_is_skipped_when_the_target_fails_validation(tmp_path, capsys):
    """An ambiguous target must not be clicked, even with --click."""
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("first"), marker("second")],
        }
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_test_step(
        config, ctx, selector="exam.practical_exam", click=True, prompt=never_prompt
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "FAIL: selector matched 2 elements" in out
    assert "Clicking" not in out
    assert not clicked(page, "first") and not clicked(page, "second")


async def test_click_is_skipped_when_the_target_is_absent(tmp_path, capsys):
    page = elements(
        **{"Записатись у чергу": [marker("queue.start_registration")], "Практичний іспит": []}
    )
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_test_step(
        config, ctx, selector="exam.practical_exam", click=True, prompt=never_prompt
    )

    assert code == 1
    assert "FAIL: selector matched 0 elements" in capsys.readouterr().out


async def test_manual_prepare_prompts_and_runs_no_prerequisites(tmp_path, capsys):
    page = elements(
        **{
            "Записатись у чергу": [marker("queue.start_registration")],
            "Практичний іспит": [marker("exam.practical_exam")],
        }
    )
    page.url = "about:blank"
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    prompted: list[str] = []

    async def record_prompt(message: str) -> str:
        prompted.append(message)
        return ""

    code = await run_test_step(
        config,
        ctx,
        selector="exam.practical_exam",
        manual_prepare=True,
        prompt=record_prompt,
    )

    assert code == 0
    assert len(prompted) == 1
    assert "Navigate to the screen" in prompted[0]
    out = capsys.readouterr().out
    assert "PREPARE" not in out
    assert not clicked(page, "queue.start_registration")


async def test_manual_prepare_with_no_wait_does_not_prompt(tmp_path):
    page = elements(**{"Практичний іспит": [marker("exam.practical_exam")]})
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    code = await run_test_step(
        config,
        ctx,
        selector="exam.practical_exam",
        manual_prepare=True,
        wait=False,
        prompt=never_prompt,
    )
    assert code == 0


async def test_target_start_url_is_used_automatically(tmp_path):
    page = elements(**{"Записатись у чергу": [marker("queue.start_registration")]})
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    await run_test_step(
        config, ctx, selector="queue.start_registration", prompt=never_prompt
    )
    assert page.navigations == [CABINET]


async def test_url_override_beats_the_configured_start_url(tmp_path):
    page = elements(**{"Записатись у чергу": [marker("queue.start_registration")]})
    config = build_config(tmp_path)
    ctx = build_context(config, page)

    await run_test_step(
        config,
        ctx,
        selector="queue.start_registration",
        url="https://example.test/other",
        prompt=never_prompt,
    )
    assert page.navigations == ["https://example.test/other"]


async def test_todo_target_fails_before_any_navigation(tmp_path):
    from hsc_queue_monitor.models import SelectorNotConfigured

    config = build_config(
        tmp_path, selectors_yaml="exam:\n  practical_exam:\n    strategy: text\n    value: TODO"
    )
    page = FakePage()
    ctx = build_context(config, page)

    with pytest.raises(SelectorNotConfigured):
        await run_test_step(config, ctx, selector="exam.practical_exam", prompt=never_prompt)
    assert page.navigations == []


async def test_dynamic_target_without_a_value_fails_before_navigating(tmp_path, capsys):
    config = build_config(tmp_path)
    page = FakePage()
    ctx = build_context(config, page)

    code = await run_test_step(
        config, ctx, selector="department.department_card", prompt=never_prompt
    )

    assert code == 1
    assert "is DYNAMIC" in capsys.readouterr().out
    assert page.navigations == []
