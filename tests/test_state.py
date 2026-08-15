"""Seen-slot bookkeeping and the re-notification cooldown."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from hsc_queue_monitor.models import AvailableSlot
from hsc_queue_monitor.monitor.state import StateStore

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def slot(time: str = "10:40", center: str = "ТСЦ 8041", day: date | None = date(2026, 8, 20)):
    return AvailableSlot(service_center=center, time=time, date=day)


def store(tmp_path, cooldown: int = 3600) -> StateStore:
    return StateStore(tmp_path / "state.json", cooldown_seconds=cooldown)


def test_slot_key_is_stable_and_readable():
    assert slot().key == "ТСЦ 8041|2026-08-20|10:40"


def test_slot_without_a_date_still_has_a_key():
    assert slot(day=None).key == "ТСЦ 8041|unknown-date|10:40"


def test_unseen_slot_is_new(tmp_path):
    assert store(tmp_path).is_new(slot(), now=NOW) is True


def test_slot_is_not_new_immediately_after_notification(tmp_path):
    state = store(tmp_path)
    state.mark_notified([slot()], now=NOW)
    assert state.is_new(slot(), now=NOW + timedelta(minutes=5)) is False


def test_slot_becomes_new_again_after_the_cooldown(tmp_path):
    state = store(tmp_path, cooldown=3600)
    state.mark_notified([slot()], now=NOW)
    assert state.is_new(slot(), now=NOW + timedelta(hours=2)) is True


def test_select_new_filters_known_slots_and_duplicates(tmp_path):
    state = store(tmp_path)
    state.mark_notified([slot("10:40")], now=NOW)

    fresh = state.select_new(
        [slot("10:40"), slot("11:00"), slot("11:00")], now=NOW + timedelta(minutes=1)
    )
    assert [s.time for s in fresh] == ["11:00"]


def test_state_round_trips_through_the_file(tmp_path):
    state = store(tmp_path)
    state.mark_notified([slot("10:40"), slot("11:00")], now=NOW)
    state.save()

    reloaded = StateStore(tmp_path / "state.json", cooldown_seconds=3600).load()
    assert reloaded.known_keys == state.known_keys
    assert reloaded.is_new(slot("10:40"), now=NOW) is False


def test_saved_file_contains_no_credentials(tmp_path):
    state = store(tmp_path)
    state.mark_notified([slot()], now=NOW)
    state.save()

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload) == {"version", "updated_at", "seen_slots"}
    for forbidden in ("cookie", "token", "password", "masterkey", "authorization"):
        assert forbidden not in raw.lower()


def test_a_plain_list_state_file_is_accepted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"seen_slots": ["ТСЦ 8041|2026-08-20|10:40"]}), encoding="utf-8")

    state = StateStore(path, cooldown_seconds=3600).load()
    assert len(state) == 1
    assert state.is_new(slot(), now=NOW) is False


def test_corrupt_state_file_does_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = StateStore(path, cooldown_seconds=3600).load()
    assert len(state) == 0


def test_missing_state_file_starts_empty(tmp_path):
    assert len(store(tmp_path).load()) == 0


def test_prune_drops_entries_older_than_the_retention_window(tmp_path):
    state = store(tmp_path)
    state.mark_notified([slot()], now=NOW - timedelta(days=45))
    state.mark_notified([slot("11:00")], now=NOW)

    assert state.prune(now=NOW) == 1
    assert len(state) == 1


def test_zero_cooldown_means_always_notify(tmp_path):
    state = store(tmp_path, cooldown=0)
    state.mark_notified([slot()], now=NOW)
    assert state.is_new(slot(), now=NOW) is True
