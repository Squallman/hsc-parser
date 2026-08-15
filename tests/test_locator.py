"""The YAML -> Playwright locator mapping, and match validation."""

from __future__ import annotations

import pytest
import yaml
from conftest import FakeElement, FakePage

from hsc_queue_monitor.config import SelectorRegistry
from hsc_queue_monitor.models import (
    LocatorAmbiguous,
    LocatorNotFound,
    LocatorSpec,
    SelectorNotConfigured,
)
from hsc_queue_monitor.pages.base_page import BasePage, build_locator


def spec(key: str, **kwargs) -> LocatorSpec:
    return LocatorSpec.from_dict(key, kwargs)


def page_object(page: FakePage, yaml_text: str, **kwargs) -> BasePage:
    selectors = SelectorRegistry.from_dict(yaml.safe_load(yaml_text))
    return BasePage(page, selectors, default_timeout=kwargs.pop("timeout", 300), **kwargs)


# --------------------------------------------------------------------------- #
# Strategy mapping
# --------------------------------------------------------------------------- #


def test_role_strategy_maps_to_get_by_role():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="role", role="button", name="Увійти"))
    api, args, kwargs = page.last_call
    assert api == "get_by_role"
    assert args == ("button",)
    assert kwargs == {"name": "Увійти"}


def test_text_strategy_maps_to_get_by_text():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="text", value="Практичний іспит"))
    assert page.last_call == ("get_by_text", ("Практичний іспит",), {})


def test_label_strategy_maps_to_get_by_label():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="label", value="Пароль"))
    assert page.last_call == ("get_by_label", ("Пароль",), {})


def test_placeholder_strategy_maps_to_get_by_placeholder():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="placeholder", value="Введіть пароль"))
    assert page.last_call == ("get_by_placeholder", ("Введіть пароль",), {})


def test_css_strategy_maps_to_locator():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="css", value='input[type="file"]'))
    assert page.last_call == ("locator", ('input[type="file"]',), {})


def test_test_id_strategy_maps_to_get_by_test_id():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="test_id", value="submit-btn"))
    assert page.last_call == ("get_by_test_id", ("submit-btn",), {})


def test_exact_is_forwarded_only_when_set():
    page = FakePage()
    build_locator(page, spec("a.b", strategy="text", value="A", exact=True))
    assert page.last_call[2] == {"exact": True}

    build_locator(page, spec("a.b", strategy="text", value="A"))
    assert page.last_call[2] == {}


# --------------------------------------------------------------------------- #
# Spec semantics
# --------------------------------------------------------------------------- #


def test_describe_renders_the_playwright_call():
    assert spec("a.b", strategy="text", value="Категорія A").describe() == (
        'get_by_text("Категорія A")'
    )
    assert spec("a.b", strategy="role", role="button", name="Далі").describe() == (
        'get_by_role("button", name="Далі")'
    )
    assert spec("a.b", strategy="css", value=".x", nth=2).describe() == 'locator(".x").nth(2)'


def test_dynamic_spec_requires_a_runtime_value():
    dynamic = spec("department.department_card", strategy="text", value="DYNAMIC")
    assert dynamic.is_dynamic

    with pytest.raises(SelectorNotConfigured, match="DYNAMIC"):
        dynamic.resolved()

    filled = dynamic.resolved("ТСЦ 8041")
    assert filled.value == "ТСЦ 8041"
    assert not filled.is_dynamic


def test_dynamic_role_spec_fills_the_accessible_name():
    dynamic = spec("d.card", strategy="role", role="button", name="DYNAMIC")
    assert dynamic.resolved("ТСЦ 8041").name == "ТСЦ 8041"


def test_format_params_are_substituted():
    template = spec("category.any", strategy="text", value="Категорія {category}")
    assert template.resolved(category="A").value == "Категорія A"


def test_uniqueness_expectation_depends_on_multiple_and_nth():
    assert spec("a.b", strategy="css", value=".x").expects_unique
    assert not spec("a.b", strategy="css", value=".x", multiple=True).expects_unique
    assert not spec("a.b", strategy="css", value=".x", nth=1).expects_unique


# --------------------------------------------------------------------------- #
# Resolution and validation
# --------------------------------------------------------------------------- #

ONE_SELECTOR = """
category:
  category_a:
    strategy: text
    value: "Категорія A"
"""


async def test_resolve_returns_the_single_visible_match():
    page = FakePage([FakeElement(text="Категорія A")])
    resolved = await page_object(page, ONE_SELECTOR).resolve("category.category_a")
    assert await resolved.is_visible()


async def test_resolve_raises_when_nothing_matches():
    page = FakePage([])
    with pytest.raises(LocatorNotFound, match="matched 0 visible elements"):
        await page_object(page, ONE_SELECTOR).resolve("category.category_a")


async def test_hidden_matches_do_not_count_as_found():
    page = FakePage([FakeElement(text="Категорія A", visible=False)])
    with pytest.raises(LocatorNotFound):
        await page_object(page, ONE_SELECTOR).resolve("category.category_a")


async def test_ambiguous_match_fails_with_candidates():
    page = FakePage(
        [
            FakeElement(text="Категорія A", id="one"),
            FakeElement(text="Категорія A1", id="two"),
            FakeElement(text="Категорія AM", id="three"),
        ]
    )
    with pytest.raises(LocatorAmbiguous) as exc:
        await page_object(page, ONE_SELECTOR).resolve("category.category_a")

    message = str(exc.value)
    assert "matched 3 visible elements" in message
    # The diagnostic must name the candidates, not just the count.
    assert "'one'" in message and "'three'" in message
    assert len(exc.value.candidates) == 3


async def test_explicit_nth_disambiguates():
    yaml_text = """
    category:
      category_a:
        strategy: text
        value: "Категорія A"
        nth: 1
    """
    page = FakePage([FakeElement(text="first"), FakeElement(text="second")])
    resolved = await page_object(page, yaml_text).resolve("category.category_a")
    assert await resolved.inner_text() == "second"


async def test_nth_beyond_the_match_count_is_an_error():
    yaml_text = """
    category:
      category_a:
        strategy: text
        value: "A"
        nth: 5
    """
    page = FakePage([FakeElement(text="only")])
    with pytest.raises(LocatorNotFound, match="nth=5"):
        await page_object(page, yaml_text).resolve("category.category_a")


async def test_nth_counts_visible_matches_only():
    """nth: 0 must mean 'first *visible*', not 'first in the DOM'."""
    yaml_text = """
    category:
      category_a:
        strategy: text
        value: "A"
        nth: 0
    """
    page = FakePage([FakeElement(text="hidden", visible=False), FakeElement(text="shown")])
    resolved = await page_object(page, yaml_text).resolve("category.category_a")
    assert await resolved.inner_text() == "shown"


async def test_multiple_true_allows_many_matches():
    yaml_text = """
    department:
      department_list_item:
        strategy: css
        value: ".card"
        multiple: true
    """
    page = FakePage([FakeElement(text="ТСЦ 8041"), FakeElement(text="ТСЦ 8042")])
    names = await page_object(page, yaml_text).texts("department.department_list_item")
    assert names == ["ТСЦ 8041", "ТСЦ 8042"]


async def test_visible_false_allows_hidden_elements():
    """The MasterKey file input is typically hidden behind a styled button."""
    yaml_text = """
    login:
      key_file:
        strategy: css
        value: 'input[type="file"]'
        visible: false
    """
    page = FakePage([FakeElement(tag="input", visible=False)])
    resolved = await page_object(page, yaml_text).resolve("login.key_file")
    assert resolved is not None


async def test_todo_selector_is_never_executed():
    yaml_text = """
    exam:
      practical_exam:
        strategy: text
        value: "TODO"
    """
    page = FakePage([FakeElement(text="anything")])
    with pytest.raises(SelectorNotConfigured):
        await page_object(page, yaml_text).click("exam.practical_exam")
    assert page.calls == []  # nothing was ever looked up in the DOM


async def test_click_targets_the_resolved_element():
    page = FakePage([FakeElement(text="Категорія A")])
    obj = page_object(page, ONE_SELECTOR)
    await obj.click("category.category_a")
    assert page._elements[0].attrs.get("clicked") is True


async def test_is_present_does_not_raise_when_absent():
    page = FakePage([])
    assert await page_object(page, ONE_SELECTOR).is_present(
        "category.category_a", timeout=10
    ) is False


async def test_count_visible_ignores_hidden():
    yaml_text = """
    calendar:
      available_slot:
        strategy: css
        value: ".slot"
        multiple: true
    """
    page = FakePage(
        [FakeElement(visible=True), FakeElement(visible=False), FakeElement(visible=True)]
    )
    assert await page_object(page, yaml_text).count_visible("calendar.available_slot") == 2
