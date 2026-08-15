"""Model, change-detection and state-persistence tests (no network)."""

from __future__ import annotations

import json
from datetime import date

from hsc_queue_monitor.models import (
    AvailableDate,
    AvailableSlot,
    Department,
    MonitorState,
    normalize_date,
    normalize_time,
    unwrap_list,
)


def slot(date_str: str, time_str: str, *, department_id: int = 1, available: bool = True):
    return AvailableSlot(
        department_id=department_id,
        date=date_str,
        time=time_str,
        available=available,
        service_id=47,
    )


def day(date_str: str, *, department_id: int = 1, available: bool = True):
    return AvailableDate(
        department_id=department_id, date=date_str, available=available, service_id=47
    )


# ------------------------------------------------------------------ helpers
def test_normalize_date_variants():
    assert normalize_date("2026-08-20") == "2026-08-20"
    assert normalize_date("20.08.2026") == "2026-08-20"
    assert normalize_date("2026-08-20T10:40:00Z") == "2026-08-20"
    assert normalize_date(date(2026, 8, 20)) == "2026-08-20"
    assert normalize_date(None) is None
    assert normalize_date("not-a-date") == "not-a-date"


def test_normalize_time_variants():
    assert normalize_time("10:40") == "10:40"
    assert normalize_time("10:40:00") == "10:40"
    assert normalize_time("2026-08-20T10:40:00") == "10:40"
    assert normalize_time(None) is None


def test_unwrap_list_handles_wrappers():
    assert unwrap_list([{"id": 1}]) == [{"id": 1}]
    assert unwrap_list({"data": [{"id": 1}]}) == [{"id": 1}]
    assert unwrap_list({"data": {"items": [{"id": 2}]}}) == [{"id": 2}]
    assert unwrap_list({"unexpected": 1}) == []
    assert unwrap_list(None) == []


# ------------------------------------------------------------------- models
def test_available_date_from_api_infers_availability_from_count():
    parsed = AvailableDate.from_api(
        {"date": "20.08.2026", "freeCount": 3}, department_id=5, service_id=47
    )
    assert parsed is not None
    assert parsed.date == "2026-08-20"
    assert parsed.department_id == 5
    assert parsed.free_count == 3
    assert parsed.available is True
    assert parsed.key == "47|5|2026-08-20"


def test_available_date_requires_a_date():
    assert AvailableDate.from_api({"freeCount": 1}) is None


def test_available_slot_from_api_uses_fallback_date():
    parsed = AvailableSlot.from_api(
        {"time": "10:40:00", "available": True},
        department_id=5,
        service_id=47,
        date="2026-08-20",
    )
    assert parsed is not None
    assert parsed.time == "10:40"
    assert parsed.key == "47|5|2026-08-20|10:40"


def test_available_slot_without_time_is_ignored():
    assert AvailableSlot.from_api({"date": "2026-08-20"}) is None


def test_department_address_and_label():
    department = Department.from_api(
        {
            "id": 12,
            "name": "ТСЦ 8041",
            "region": "Київська обл.",
            "city": "Київ",
            "street": "вул. Набережно-Хрещатицька",
            "building": "27",
            "office": "3",
        }
    )
    assert department.label == "[12] ТСЦ 8041"
    assert "Київ" in department.address
    assert "каб. 3" in department.address


# -------------------------------------------------------- change detection
def test_new_slots_reports_only_unseen():
    state = MonitorState()
    first = [slot("2026-08-20", "10:00"), slot("2026-08-20", "10:20")]
    assert [s.time for s in state.new_slots(first)] == ["10:00", "10:20"]
    state.mark_slots_seen(first)

    second = [*first, slot("2026-08-20", "10:40")]
    assert [s.time for s in state.new_slots(second)] == ["10:40"]


def test_new_slots_suppresses_duplicates_within_one_batch():
    state = MonitorState()
    batch = [slot("2026-08-20", "10:00"), slot("2026-08-20", "10:00")]
    assert len(state.new_slots(batch)) == 1


def test_unavailable_slots_are_never_reported():
    state = MonitorState()
    assert state.new_slots([slot("2026-08-20", "10:00", available=False)]) == []


def test_date_becoming_available_is_detected_once():
    state = MonitorState()
    state.mark_dates_seen([day("2026-08-20", available=False)])
    assert state.newly_available_dates([day("2026-08-20", available=False)]) == []

    became = state.newly_available_dates([day("2026-08-20", available=True)])
    assert [d.date for d in became] == ["2026-08-20"]

    state.mark_dates_seen([day("2026-08-20", available=True)])
    assert state.newly_available_dates([day("2026-08-20", available=True)]) == []


def test_prune_drops_past_days():
    state = MonitorState()
    state.mark_slots_seen([slot("2026-08-01", "09:00"), slot("2026-09-01", "09:00")])
    state.mark_dates_seen([day("2026-08-01"), day("2026-09-01")])

    removed = state.prune(date(2026, 8, 15))

    assert removed == 2
    assert state.seen_slots == {"47|1|2026-09-01|09:00"}
    assert list(state.date_availability) == ["47|1|2026-09-01"]


# ------------------------------------------------------------- persistence
def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = MonitorState(path=path)
    state.mark_slots_seen([slot("2026-08-20", "10:40")])
    state.mark_dates_seen([day("2026-08-20")])
    state.touch()
    state.save()

    loaded = MonitorState.load(path)
    assert loaded.seen_slots == {"47|1|2026-08-20|10:40"}
    assert loaded.date_availability == {"47|1|2026-08-20": True}
    assert loaded.last_check_at == state.last_check_at
    assert loaded.new_slots([slot("2026-08-20", "10:40")]) == []


def test_state_never_contains_secrets(tmp_path):
    path = tmp_path / "state.json"
    state = MonitorState(path=path)
    state.mark_slots_seen([slot("2026-08-20", "10:40")])
    state.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "last_check_at", "seen_slots", "date_availability"}
    blob = path.read_text(encoding="utf-8").lower()
    for forbidden in ("cookie", "token", "session", "authorization"):
        assert forbidden not in blob


def test_load_missing_or_corrupt_state_returns_empty(tmp_path):
    missing = MonitorState.load(tmp_path / "nope.json")
    assert missing.seen_slots == set()

    corrupt_path = tmp_path / "broken.json"
    corrupt_path.write_text("{not json", encoding="utf-8")
    assert MonitorState.load(corrupt_path).seen_slots == set()
