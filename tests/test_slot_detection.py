"""Calendar text parsing and slot extraction."""

from __future__ import annotations

from datetime import date

import pytest
import yaml
from conftest import FakeElement, FakePage

from hsc_queue_monitor.config import SelectorRegistry
from hsc_queue_monitor.notification.base import Notification
from hsc_queue_monitor.pages.calendar_page import (
    CalendarPage,
    parse_date_text,
    parse_times,
)

CALENDAR_YAML = """
calendar:
  available_day:
    strategy: css
    value: ".day.available"
    multiple: true
  available_slot:
    strategy: css
    value: ".slot.free"
    multiple: true
  no_slots:
    strategy: text
    value: "Немає вільних місць"
"""


def calendar(page: FakePage, yaml_text: str = CALENDAR_YAML) -> CalendarPage:
    return CalendarPage(
        page, SelectorRegistry.from_dict(yaml.safe_load(yaml_text)), default_timeout=200
    )


def calendar_page(
    *,
    slots: list[FakeElement] | None = None,
    days: list[FakeElement] | None = None,
    no_slots: list[FakeElement] | None = None,
) -> FakePage:
    """A page where each calendar selector matches only its own elements."""
    return FakePage(
        matches={
            ".slot.free": slots or [],
            ".day.available": days or [],
            "Немає вільних місць": no_slots or [],
        }
    )


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20.08.2026", date(2026, 8, 20)),
        ("2026-08-20", date(2026, 8, 20)),
        ("20/08/2026", date(2026, 8, 20)),
        ("Запис на 20.08.2026 о 10:40", date(2026, 8, 20)),
        ("20 серпня 2026", date(2026, 8, 20)),
        ("1 січня 2027", date(2027, 1, 1)),
    ],
)
def test_dates_are_parsed_from_ui_text(text, expected):
    assert parse_date_text(text) == expected


def test_ukrainian_date_without_a_year_uses_the_reference_year():
    assert parse_date_text("20 серпня", today=date(2026, 1, 5)) == date(2026, 8, 20)


@pytest.mark.parametrize("text", [None, "", "немає вільних дат", "10:40", "32.13.2026"])
def test_unparseable_dates_return_none(text):
    assert parse_date_text(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10:40", ["10:40"]),
        ("9:05", ["09:05"]),
        ("10:40 – 11:00", ["10:40", "11:00"]),
        ("23:59", ["23:59"]),
        ("Вільно: 08:00, 08:20, 08:40", ["08:00", "08:20", "08:40"]),
    ],
)
def test_times_are_parsed_and_zero_padded(text, expected):
    assert parse_times(text) == expected


@pytest.mark.parametrize("text", [None, "", "24:00", "10:75", "немає"])
def test_invalid_times_are_ignored(text):
    assert parse_times(text) == []


# --------------------------------------------------------------------------- #
# Reading the calendar
# --------------------------------------------------------------------------- #


async def test_slots_are_read_from_element_text():
    page = FakePage([FakeElement(text="10:40"), FakeElement(text="11:00")])
    slots = await calendar(page).get_available_slots("ТСЦ 8041", on_date=date(2026, 8, 20))

    assert [s.time for s in slots] == ["10:40", "11:00"]
    assert all(s.date == date(2026, 8, 20) for s in slots)
    assert all(s.service_center == "ТСЦ 8041" for s in slots)


async def test_slot_date_from_a_data_attribute_beats_the_fallback():
    page = FakePage([FakeElement(text="10:40", data_date="2026-09-01")])
    slots = await calendar(page).get_available_slots("ТСЦ", on_date=date(2026, 8, 20))
    assert slots[0].date == date(2026, 9, 1)


async def test_slot_without_a_recognisable_time_is_skipped():
    page = FakePage([FakeElement(text="зайнято"), FakeElement(text="10:40")])
    slots = await calendar(page).get_available_slots("ТСЦ")
    assert [s.time for s in slots] == ["10:40"]


async def test_no_matching_elements_means_no_slots():
    page = FakePage([])
    assert await calendar(page).get_available_slots("ТСЦ") == []


async def test_available_dates_are_sorted_and_deduplicated():
    page = FakePage(
        [
            FakeElement(text="21.08.2026"),
            FakeElement(text="20.08.2026"),
            FakeElement(text="20.08.2026"),
        ]
    )
    assert await calendar(page).get_available_dates() == [
        date(2026, 8, 20),
        date(2026, 8, 21),
    ]


async def test_has_available_slots_is_true_when_slots_render():
    page = calendar_page(slots=[FakeElement(text="10:40")])
    assert await calendar(page).has_available_slots() is True


async def test_has_available_slots_is_true_when_only_day_cells_render():
    page = calendar_page(days=[FakeElement(text="20.08.2026")])
    assert await calendar(page).has_available_slots() is True


async def test_has_available_slots_is_false_on_an_empty_calendar():
    assert await calendar(calendar_page()).has_available_slots() is False


async def test_no_slots_message_wins_over_stale_slot_nodes():
    """A leftover slot node must not be reported once the UI says there are none."""
    page = calendar_page(
        slots=[FakeElement(text="10:40")],
        no_slots=[FakeElement(text="Немає вільних місць")],
    )
    assert await calendar(page).has_available_slots() is False


# --------------------------------------------------------------------------- #
# Notification rendering
# --------------------------------------------------------------------------- #


def test_single_slot_notification_matches_the_documented_format():
    from hsc_queue_monitor.models import AvailableSlot

    rendered = Notification(
        service_center="ТСЦ 8041",
        slots=(AvailableSlot("ТСЦ 8041", "10:40", date(2026, 8, 20)),),
    ).render()

    assert "🏍 HSC appointment available" in rendered
    assert "Exam: Practical" in rendered
    assert "Category: A" in rendered
    assert "Service center: ТСЦ 8041" in rendered
    assert "Date: 20.08.2026" in rendered
    assert "Time: 10:40" in rendered


def test_multi_slot_notification_lists_each_slot():
    from hsc_queue_monitor.models import AvailableSlot

    rendered = Notification(
        service_center="ТСЦ 8041",
        slots=(
            AvailableSlot("ТСЦ 8041", "10:40", date(2026, 8, 20)),
            AvailableSlot("ТСЦ 8041", "11:00", date(2026, 8, 20)),
        ),
    ).render()

    assert "Slots: 2" in rendered
    assert "20.08.2026 10:40" in rendered
    assert "20.08.2026 11:00" in rendered
