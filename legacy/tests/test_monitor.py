"""QueueMonitor tests with a fully faked API client (no browser, no network)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from hsc_queue_monitor.api import ApiError, EndpointNotDiscoveredError
from hsc_queue_monitor.config import Settings
from hsc_queue_monitor.models import AvailableDate, AvailableSlot, Department, MonitorState
from hsc_queue_monitor.monitor import QueueMonitor
from hsc_queue_monitor.notifier import NotificationEvent, Notifier

SERVICE_ID = 47
DEPARTMENT_ID = 8041


class FakeApi:
    """Implements just the surface QueueMonitor uses."""

    def __init__(self, *, dates=None, slots=None, departments=None):
        self._dates = dates or {}
        self._slots = slots or {}
        self._departments = departments or [
            Department(id=DEPARTMENT_ID, name="ТСЦ 8041"),
            Department(id=8042, name="ТСЦ 8042"),
        ]
        self.date_calls: list[int] = []
        self.slot_calls: list[tuple[int, str]] = []

    async def get_departments(self, service_id=None):
        return self._departments

    async def get_available_dates(
        self, department_id, *, service_id=None, date_from=None, date_to=None
    ):
        self.date_calls.append(department_id)
        return list(self._dates.get(department_id, []))

    async def get_available_slots(self, department_id, date, *, service_id=None):
        self.slot_calls.append((department_id, date))
        return list(self._slots.get((department_id, date), []))


class RecordingNotifier(Notifier):
    def __init__(self):
        self.events: list[NotificationEvent] = []

    async def notify(self, event: NotificationEvent) -> None:
        self.events.append(event)


def make_settings(tmp_path, **overrides):
    base = Settings().with_overrides(
        service_id=SERVICE_ID,
        data_dir=tmp_path,
        profile_dir=tmp_path / "browser-profile",
        request_delay_seconds=0.0,
        **overrides,
    )
    return base


def make_monitor(tmp_path, api, *, state=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    notifier = RecordingNotifier()

    async def no_sleep(_delay: float) -> None:
        return None

    monitor = QueueMonitor(
        api,
        notifier,
        settings,
        state or MonitorState(path=settings.state_file),
        sleep=no_sleep,
        today=lambda: date(2026, 8, 1),
    )
    return monitor, notifier


def day(date_str, *, available=True, department_id=DEPARTMENT_ID):
    return AvailableDate(
        department_id=department_id, date=date_str, available=available, service_id=SERVICE_ID
    )


def slot(date_str, time_str, *, department_id=DEPARTMENT_ID):
    return AvailableSlot(
        department_id=department_id, date=date_str, time=time_str, service_id=SERVICE_ID
    )


# ------------------------------------------------------------- department set
async def test_resolve_departments_defaults_to_all(tmp_path):
    monitor, _ = make_monitor(tmp_path, FakeApi())
    assert await monitor.resolve_departments() == [DEPARTMENT_ID, 8042]


async def test_resolve_departments_respects_configuration(tmp_path):
    monitor, _ = make_monitor(tmp_path, FakeApi(), department_ids=(8042,))
    assert await monitor.resolve_departments() == [8042]


# --------------------------------------------------------- change detection
async def test_new_slots_are_reported_once(tmp_path):
    api = FakeApi(
        dates={DEPARTMENT_ID: [day("2026-08-20")]},
        slots={
            (DEPARTMENT_ID, "2026-08-20"): [
                slot("2026-08-20", "10:00"),
                slot("2026-08-20", "10:20"),
            ]
        },
    )
    monitor, _ = make_monitor(tmp_path, api)
    await monitor.resolve_departments()

    first = await monitor.check_once([DEPARTMENT_ID])
    assert sorted(e.time for e in first if e.kind == "new_slot") == ["10:00", "10:20"]

    second = await monitor.check_once([DEPARTMENT_ID])
    assert [e for e in second if e.kind == "new_slot"] == []


async def test_only_the_new_slot_is_reported(tmp_path):
    slots = {
        (DEPARTMENT_ID, "2026-08-20"): [slot("2026-08-20", "10:00"), slot("2026-08-20", "10:20")]
    }
    api = FakeApi(dates={DEPARTMENT_ID: [day("2026-08-20")]}, slots=slots)
    monitor, _ = make_monitor(tmp_path, api)
    await monitor.resolve_departments()
    await monitor.check_once([DEPARTMENT_ID])

    slots[(DEPARTMENT_ID, "2026-08-20")].append(slot("2026-08-20", "10:40"))
    events = await monitor.check_once([DEPARTMENT_ID])

    assert [e.time for e in events if e.kind == "new_slot"] == ["10:40"]


async def test_date_flipping_to_available_is_notified(tmp_path):
    dates = {DEPARTMENT_ID: [day("2026-08-20", available=False)]}
    api = FakeApi(dates=dates)
    monitor, _ = make_monitor(tmp_path, api)
    await monitor.resolve_departments()

    assert await monitor.check_once([DEPARTMENT_ID]) == []

    dates[DEPARTMENT_ID] = [day("2026-08-20", available=True)]
    events = await monitor.check_once([DEPARTMENT_ID])

    assert [(e.kind, e.date) for e in events] == [("date_available", "2026-08-20")]
    assert "ТСЦ 8041" in events[0].department


async def test_unavailable_dates_do_not_trigger_slot_requests(tmp_path):
    api = FakeApi(dates={DEPARTMENT_ID: [day("2026-08-20", available=False)]})
    monitor, _ = make_monitor(tmp_path, api)
    await monitor.resolve_departments()
    await monitor.check_once([DEPARTMENT_ID])
    assert api.slot_calls == []


async def test_date_range_filter_is_applied(tmp_path):
    api = FakeApi(
        dates={DEPARTMENT_ID: [day("2026-08-10"), day("2026-08-25")]},
        slots={(DEPARTMENT_ID, "2026-08-25"): [slot("2026-08-25", "09:00")]},
    )
    monitor, _ = make_monitor(tmp_path, api, date_from="2026-08-20", date_to="2026-08-31")
    await monitor.resolve_departments()

    events = await monitor.check_once([DEPARTMENT_ID])

    assert {e.date for e in events} == {"2026-08-25"}
    assert api.slot_calls == [(DEPARTMENT_ID, "2026-08-25")]


# ------------------------------------------------------------------ runtime
async def test_check_once_persists_state(tmp_path):
    api = FakeApi(
        dates={DEPARTMENT_ID: [day("2026-08-20")]},
        slots={(DEPARTMENT_ID, "2026-08-20"): [slot("2026-08-20", "10:00")]},
    )
    monitor, _ = make_monitor(tmp_path, api)
    await monitor.resolve_departments()
    await monitor.check_once([DEPARTMENT_ID])

    reloaded = MonitorState.load(tmp_path / "state.json")
    assert reloaded.seen_slots == {f"{SERVICE_ID}|{DEPARTMENT_ID}|2026-08-20|10:00"}
    assert reloaded.last_check_at is not None


async def test_api_errors_do_not_kill_the_cycle(tmp_path):
    class FlakyApi(FakeApi):
        async def get_available_dates(self, department_id, **kwargs):
            if department_id == DEPARTMENT_ID:
                raise ApiError("boom")
            return [day("2026-08-20", department_id=department_id)]

    monitor, _ = make_monitor(tmp_path, FlakyApi(slots={}))
    await monitor.resolve_departments()

    events = await monitor.check_once([DEPARTMENT_ID, 8042])

    assert [e.date for e in events] == ["2026-08-20"]


async def test_missing_endpoints_stop_the_run_cleanly(tmp_path, caplog):
    class UndiscoveredApi(FakeApi):
        async def get_available_dates(self, department_id, **kwargs):
            raise EndpointNotDiscoveredError("dates endpoint unknown")

    monitor, notifier = make_monitor(tmp_path, UndiscoveredApi())

    with caplog.at_level("ERROR"):
        await monitor.run()

    assert notifier.events == []
    assert any("not implemented yet" in record.getMessage() for record in caplog.records)


async def test_run_notifies_and_stops_on_event(tmp_path):
    api = FakeApi(
        dates={DEPARTMENT_ID: [day("2026-08-20")], 8042: []},
        slots={(DEPARTMENT_ID, "2026-08-20"): [slot("2026-08-20", "10:40")]},
    )
    monitor, notifier = make_monitor(tmp_path, api)
    stop = monitor.stop_event

    original = monitor.check_once

    async def check_and_stop(department_ids):
        events = await original(department_ids)
        stop.set()
        return events

    monitor.check_once = check_and_stop  # type: ignore[method-assign]
    await asyncio.wait_for(monitor.run(), timeout=5)

    assert [(e.kind, e.time) for e in notifier.events if e.kind == "new_slot"] == [
        ("new_slot", "10:40")
    ]


def test_next_delay_respects_minimum_and_jitter(tmp_path):
    monitor, _ = make_monitor(
        tmp_path,
        FakeApi(),
        poll_interval_seconds=10,
        min_poll_interval_seconds=30,
        poll_jitter_seconds=10,
    )
    delays = [monitor.next_delay() for _ in range(200)]
    assert min(delays) >= 30
    assert max(delays) <= 40


def test_default_interval_is_60_seconds():
    assert Settings().poll_interval_seconds == 60
    assert Settings().effective_poll_interval == 60


def test_notification_rendering_is_readable():
    event = NotificationEvent(
        kind="new_slot",
        service_id=47,
        department="[8041] ТСЦ 8041",
        date="2026-08-20",
        time="10:40",
    )
    rendered = event.render()
    assert "NEW HSC APPOINTMENT AVAILABLE" in rendered
    assert "Service: 47" in rendered
    assert "Date: 2026-08-20" in rendered
    assert "Time: 10:40" in rendered


def test_telegram_is_optional():
    from hsc_queue_monitor.notifier import ConsoleNotifier, build_notifier

    assert isinstance(build_notifier(Settings()), ConsoleNotifier)


def test_telegram_repr_hides_the_token():
    from hsc_queue_monitor.notifier import TelegramNotifier

    notifier = TelegramNotifier("123:SECRET-TOKEN", "555")
    assert "SECRET-TOKEN" not in repr(notifier)

    with pytest.raises(ValueError):
        TelegramNotifier("", "555")
