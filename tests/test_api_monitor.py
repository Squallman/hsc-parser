"""The dry-run API monitor: poll, diff, print.

Everything is faked but the logic: a routing-table API (from
:mod:`test_api_availability`), a session provider that hands out clients without
a browser, and a clock nobody waits for.

Two properties get as much attention as the feature itself, because getting them
wrong would be worse than not having the monitor at all:

* a failed or partial read must never be reported as availability disappearing;
* a broken session buys exactly one recovery, never a loop.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import re
import textwrap
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import pytest
import requests
from test_api_availability import (
    AUG_21,
    AUG_26,
    THREE_DAYS,
    ApiServer,
    FakeClock,
    RefusingServer,
    TimingOutServer,
    client_with,
)
from test_api_probe import FakeHttpResponse

from hsc_queue_monitor.api.bootstrap import QueueBootstrap
from hsc_queue_monitor.api.client import HscApiClient
from hsc_queue_monitor.api.monitor import (
    ApiMonitor,
    ApiSession,
    CentreDiff,
    CentreReading,
    CycleReport,
    diff_states,
    read_centres,
    render_cycle,
)
from hsc_queue_monitor.api.probe import WIZARD_COOKIE, CookieInfo
from hsc_queue_monitor.api.session_store import (
    PersistedSession,
    SessionStoreError,
    session_from_cookies,
)
from hsc_queue_monitor.models import AuthenticationFailed, TimeSlot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"

QUEUE_URL = "https://eqn.hsc.gov.ua/cabinet/queue"
CENTRE_A = "3242"
CENTRE_B = "4641"

# The decoy department in the shared fixture is called ТСЦ МВС № 4641, so both
# centres resolve from one departments response.

AT_NOON = datetime(2026, 8, 15, 11, 55, 0)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeProvider:
    """Hands out API sessions the way the real provider does: browser in, HTTP out.

    Every call stands for one whole browser lifecycle — open, authenticate,
    bootstrap, copy cookies, close — so counting calls counts browser launches.
    """

    def __init__(self, *servers: ApiServer, fails_after: int | None = None) -> None:
        #: One server per client handed out; the last is reused if asked again.
        self.servers = list(servers)
        self.fails_after = fails_after
        self.authentications = 0
        self.restores = 0
        self.clients: list[HscApiClient] = []
        #: Set while a session is being built, cleared when it is handed over —
        #: so a test can assert nothing browser-shaped outlives the hand-over.
        self.browser_open = False

    @property
    def browser_opens(self) -> int:
        """How many times Chromium would have been launched."""
        return self.authentications

    @property
    def bootstraps(self) -> int:
        return self.authentications

    async def create_api_session(self) -> ApiSession:
        self.authentications += 1
        self.browser_open = True
        try:
            if self.fails_after is not None and self.authentications > self.fails_after:
                from hsc_queue_monitor.models import AuthenticationFailed

                raise AuthenticationFailed("the key was not accepted")

            server = self.servers[min(len(self.clients), len(self.servers) - 1)]
            client = client_with(server)
            self.clients.append(client)
        finally:
            # The real provider closes the browser before returning, whatever
            # happened inside.
            self.browser_open = False

        return ApiSession(
            client=client,
            cookies=(
                CookieInfo("__Secure-auth.access-token", "eqn.hsc.gov.ua", "/", "aa11bb22"),
            ),
            bootstrap=QueueBootstrap(
                url=QUEUE_URL, final_url=QUEUE_URL, before=None, after="9f8e7d6c"
            ),
        )

    def restore_api_session(self, persisted: PersistedSession) -> ApiSession:
        """A client from a stored jar. Opens nothing, exactly like the real one."""
        self.restores += 1
        server = self.servers[min(len(self.clients), len(self.servers) - 1)]
        client = HscApiClient(
            session_from_cookies(persisted.cookies, user_agent=persisted.user_agent),
            fetch=server,
            sleep=lambda _seconds: None,
        )
        self.clients.append(client)
        return ApiSession(client=client)

    @property
    def server(self) -> ApiServer:
        return self.servers[min(len(self.clients) - 1, len(self.servers) - 1)]


def days_for(*dates: str) -> list[dict[str, Any]]:
    return [{"date": f"{day}T00:00:00"} for day in dates]


EMPTY_DAYS: list[dict[str, Any]] = []

SLOT_0826 = {"startTime": "08:26:00", "stopTime": "08:52:00"}
SLOT_0918 = {"startTime": "09:18:00", "stopTime": "09:44:00"}


def monitor_for(
    provider: FakeProvider,
    *,
    centres: tuple[str, ...] = (CENTRE_A,),
    interval: float = 60.0,
    slot_interval: float = 0.0,
    clock: FakeClock | None = None,
    emit: Any = None,
    now: datetime = AT_NOON,
) -> ApiMonitor:
    fake_clock = clock or FakeClock()
    return ApiMonitor(
        provider,
        centres,
        interval=interval,
        slot_interval=slot_interval,
        sleep=_async_sleep(fake_clock),
        clock=fake_clock.monotonic,
        slot_sleep=fake_clock.sleep,
        now=lambda: now,
        emit=emit if emit is not None else (lambda text: None),
    )


def _async_sleep(clock: FakeClock) -> Any:
    async def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds

    return sleep


def monitor_config(tmp_path: Path) -> Any:
    """The command's config, with no browser context attached to it."""
    from test_api_availability import build_context
    from test_api_probe import ProbePage

    config, _ctx, _auth = build_context(tmp_path, ProbePage())
    return config


class Recorder:
    """Collects what the monitor printed."""

    def __init__(self) -> None:
        self.blocks: list[str] = []

    def __call__(self, text: str) -> None:
        self.blocks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)


# --------------------------------------------------------------------------- #
# Scan structure
# --------------------------------------------------------------------------- #


def test_departments_is_read_once_for_every_centre_in_a_scan():
    server = ApiServer(days=EMPTY_DAYS)
    readings = read_centres(client_with(server), [CENTRE_A, CENTRE_B])

    assert server.endpoints.count("departments") == 1
    assert [r.centre_id for r in readings] == [CENTRE_A, CENTRE_B]
    assert all(r.complete for r in readings)


def test_two_centres_with_no_dates_cost_three_reads_and_no_slots_call():
    server = ApiServer(days=EMPTY_DAYS)
    read_centres(client_with(server), [CENTRE_A, CENTRE_B])

    assert server.endpoints == ["departments", "days", "days"]
    assert "slots" not in server.endpoints


def test_each_centre_is_asked_about_with_its_own_resolved_department_id():
    server = ApiServer(days=EMPTY_DAYS)
    read_centres(client_with(server), [CENTRE_A, CENTRE_B])

    asked = [q["departmentId"][0] for q in server.queries_for("days")]
    # 3242 -> 2 in this fixture, and 4641 -> 3242. Neither is its visible number.
    assert asked == ["2", "3242"]


def test_slots_are_read_when_dates_exist():
    server = ApiServer(days=THREE_DAYS)
    readings = read_centres(client_with(server), [CENTRE_A])

    assert server.endpoints.count("slots") == 3
    assert readings[0].complete
    assert readings[0].slot_count == 3


# --------------------------------------------------------------------------- #
# Baseline and diffs
# --------------------------------------------------------------------------- #


async def test_the_first_scan_is_a_baseline_and_prints_a_compact_summary():
    recorder = Recorder()
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(provider, centres=(CENTRE_A, CENTRE_B), emit=recorder)

    await monitor.run(once=True)

    assert "11:55:00 BASELINE" in recorder.text
    assert f"  {CENTRE_A}: no availability" in recorder.text
    assert f"  {CENTRE_B}: no availability" in recorder.text


async def test_a_baseline_with_slots_does_not_list_every_one():
    recorder = Recorder()
    provider = FakeProvider(ApiServer(days=THREE_DAYS))
    monitor = monitor_for(provider, emit=recorder)

    await monitor.run(once=True)

    assert "BASELINE" in recorder.text
    assert f"  {CENTRE_A}: 3 slot(s) across 2 date(s)" in recorder.text
    assert "09:20-09:46" not in recorder.text  # compact: no slot list on a baseline


async def test_an_identical_second_scan_reports_no_changes():
    recorder = Recorder()
    provider = FakeProvider(ApiServer(days=THREE_DAYS))
    monitor = monitor_for(provider, emit=recorder)
    await monitor.start()

    first, second = await monitor.cycle(), await monitor.cycle()
    recorder(render_cycle(second, AT_NOON, retained=monitor.retained()))

    assert first.baseline and not second.baseline
    assert not second.changed
    assert "11:55:00 no changes" in recorder.text
    assert f"  {CENTRE_A}: 3 slots" in recorder.text


async def test_a_new_slot_is_reported_with_its_window():
    server = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [SLOT_0826]},
    )
    monitor = monitor_for(FakeProvider(server))
    await monitor.start()
    await monitor.cycle()

    # The site opens another appointment on the same day.
    server.payloads["slots"]["2026-08-26T00:00:00"] = [SLOT_0826, SLOT_0918]
    report = await monitor.cycle()

    rendered = render_cycle(report, AT_NOON)
    assert "11:55:00 NEW AVAILABILITY" in rendered
    assert f"  {CENTRE_A}" in rendered
    assert "    2026-08-26" in rendered
    assert "      + 09:18-09:44" in rendered
    assert "+ 08:26-08:52" not in rendered  # unchanged slots are not news


async def test_a_removed_slot_is_reported():
    server = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [SLOT_0826, SLOT_0918]},
    )
    monitor = monitor_for(FakeProvider(server))
    await monitor.start()
    await monitor.cycle()

    server.payloads["slots"]["2026-08-26T00:00:00"] = [SLOT_0918]
    report = await monitor.cycle()

    rendered = render_cycle(report, AT_NOON)
    assert "11:55:00 AVAILABILITY REMOVED" in rendered
    assert "      - 08:26-08:52" in rendered


async def test_additions_and_removals_in_one_cycle_are_both_shown():
    server = ApiServer(
        days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]}
    )
    monitor = monitor_for(FakeProvider(server))
    await monitor.start()
    await monitor.cycle()

    server.payloads["slots"]["2026-08-26T00:00:00"] = [SLOT_0918]
    rendered = render_cycle(await monitor.cycle(), AT_NOON)

    assert "11:55:00 CHANGES" in rendered
    assert "NEW AVAILABILITY" in rendered and "+ 09:18-09:44" in rendered
    assert "AVAILABILITY REMOVED" in rendered and "- 08:26-08:52" in rendered


def test_a_slot_is_identified_by_centre_date_and_start_time():
    start = clock_time(8, 26)
    previous = {(AUG_26, start): TimeSlot(start, "08:26", clock_time(8, 52))}
    # The same start, a different window: still the same slot.
    current = {(AUG_26, start): TimeSlot(start, "08:26", clock_time(9, 0))}

    assert not diff_states(CENTRE_A, previous, current).changed
    # A different date with the same clock time is a different slot.
    moved = {(AUG_21, start): TimeSlot(start, "08:26", clock_time(8, 52))}
    assert len(diff_states(CENTRE_A, previous, moved).added) == 1


def test_the_end_time_survives_into_the_report():
    slot = TimeSlot(clock_time(8, 26), "08:26", clock_time(8, 52))
    diff = CentreDiff(centre_id=CENTRE_A, added=((AUG_26, slot),))
    report = CycleReport(readings=(), diffs=(diff,))

    assert "+ 08:26-08:52" in render_cycle(report, AT_NOON)


# --------------------------------------------------------------------------- #
# Partial reads never erase state
# --------------------------------------------------------------------------- #


async def test_a_partial_centre_keeps_its_previous_availability():
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    monitor = monitor_for(FakeProvider(good))
    await monitor.start()
    await monitor.cycle()
    before = dict(monitor.state[CENTRE_A])

    # The next cycle is refused partway through.
    monitor.client = client_with(
        RefusingServer(
            refuse_from="2026-08-26T00:00:00",
            days=days_for("2026-08-26"),
        )
    )
    report = await monitor.cycle()

    assert not report.changed  # nothing is reported as gone
    assert monitor.state[CENTRE_A] == before  # and nothing was forgotten
    rendered = render_cycle(report, AT_NOON, retained=monitor.retained())
    # Not "no changes": this cycle read nothing completely and says so.
    assert "11:55:00 no complete read" in rendered
    assert f"  {CENTRE_A}: partial — HTTP 429 Too Many Requests" in rendered
    assert "  previous availability retained" in rendered
    assert rendered.count("partial —") == 1  # listed once, not twice


async def test_one_centre_can_update_while_another_is_partial():
    """A 429 for one centre must not freeze the other."""
    days = days_for("2026-08-26")

    class HalfRefusing(ApiServer):
        """Refuses slots only while the second centre is being read."""

        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
            response = super().__call__(session, url, timeout)
            if self.endpoints[-1] == "slots" and self.endpoints.count("days") == 2:
                return FakeHttpResponse(429, {"Content-Type": "application/json"}, b'"no"')
            return response

    server = HalfRefusing(days=days, slots={"2026-08-26T00:00:00": [SLOT_0826]})
    monitor = monitor_for(FakeProvider(server), centres=(CENTRE_A, CENTRE_B))
    await monitor.start()
    await monitor.cycle()

    assert monitor.state[CENTRE_A]  # read completely
    assert CENTRE_B not in monitor.state  # never read completely, never invented

    server.payloads["slots"]["2026-08-26T00:00:00"] = [SLOT_0826, SLOT_0918]
    report = await monitor.cycle()

    assert [diff.centre_id for diff in report.diffs] == [CENTRE_A]
    assert report.diffs[0].added[0][1].display_range == "09:18-09:44"


@pytest.mark.parametrize("status", [401, 429, 500, 502])
async def test_a_refused_departments_call_freezes_every_centre(status):
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    monitor = monitor_for(FakeProvider(good), centres=(CENTRE_A, CENTRE_B))
    await monitor.start()
    await monitor.cycle()
    before = dict(monitor.state)

    monitor.client = client_with(
        ApiServer(statuses={"departments": status}, content_type="text/html")
    )
    report = await monitor.cycle()

    assert not report.changed
    assert dict(monitor.state) == before
    assert all(not reading.complete for reading in report.readings)


async def test_a_timed_out_read_keeps_state_and_does_not_re_authenticate():
    """A slow server is not a broken session, so it buys no recovery."""
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    provider = FakeProvider(good)
    monitor = monitor_for(provider)
    await monitor.start()
    await monitor.cycle()
    before = dict(monitor.state[CENTRE_A])

    monitor.client = client_with(
        TimingOutServer(timeout_from="2026-08-26T00:00:00", days=days_for("2026-08-26"))
    )
    report = await monitor.cycle()

    assert not report.changed
    assert monitor.state[CENTRE_A] == before
    assert "ReadTimeout" in report.readings[0].detail
    assert provider.authentications == 1  # no recovery: the session is fine
    assert monitor.recoveries == 0


async def test_an_unreadable_schema_does_not_erase_state():
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    monitor = monitor_for(FakeProvider(good))
    await monitor.start()
    await monitor.cycle()

    monitor.client = client_with(
        ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [{"x": 1}]})
    )
    report = await monitor.cycle()

    assert not report.changed
    assert monitor.state[CENTRE_A]
    assert "not recognised" in report.readings[0].detail


# --------------------------------------------------------------------------- #
# Session recovery
# --------------------------------------------------------------------------- #


class HtmlServer(ApiServer):
    """Answers with a body that is not JSON at all."""

    def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
        super().__call__(session, url, timeout)
        return FakeHttpResponse(200, {"Content-Type": "text/html"}, b"<html>no</html>")


class ForbiddenServer(ApiServer):
    """Answers 403 — the one status that is allowed to open a browser."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.broken = True

    def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
        response = super().__call__(session, url, timeout)
        if self.broken:
            return FakeHttpResponse(403, {"Content-Type": "application/json"}, b'"no"')
        return response


async def test_a_403_buys_exactly_one_recovery():
    broken = ForbiddenServer(days=EMPTY_DAYS)
    healthy = ApiServer(days=EMPTY_DAYS)
    provider = FakeProvider(broken, healthy)
    monitor = monitor_for(provider)

    await monitor.start()
    report = await monitor.cycle()

    assert provider.browser_opens == 2  # startup, then one recovery
    assert provider.bootstraps == 2
    assert len(provider.clients) == 2  # a *fresh* client, not the refused one
    assert monitor.client is provider.clients[-1]
    assert monitor.client is not provider.clients[0]
    assert not provider.browser_open  # closed again before polling resumed
    assert report.recovered and not report.failed
    assert monitor.recoveries == 1
    assert report.readings[0].complete


async def test_a_second_failure_in_the_same_cycle_is_not_a_second_recovery():
    provider = FakeProvider(ForbiddenServer(days=EMPTY_DAYS))  # always 401
    monitor = monitor_for(provider)

    await monitor.start()
    report = await monitor.cycle()

    # One recovery attempt, one repeat of the scan, and then it gives up.
    assert provider.authentications == 2
    assert monitor.cycles == 1
    assert not report.failed  # the readings are simply incomplete
    assert all(not reading.complete for reading in report.readings)


async def test_a_failed_recovery_reports_a_failed_cycle_and_keeps_state():
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    provider = FakeProvider(good, fails_after=1)
    monitor = monitor_for(provider)
    await monitor.start()
    await monitor.cycle()
    before = dict(monitor.state)

    monitor.client = client_with(ForbiddenServer(days=EMPTY_DAYS))
    report = await monitor.cycle()

    assert report.failed == "session recovery failed"
    assert dict(monitor.state) == before
    rendered = render_cycle(report, AT_NOON, retained=monitor.retained())
    assert "CYCLE FAILED — session recovery failed" in rendered
    assert "previous availability retained" in rendered


async def test_an_empty_response_no_longer_opens_a_browser():
    """204 used to count as a broken session. The policy is now 403-only."""

    class NoContent(ApiServer):
        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    provider = FakeProvider(NoContent(days=EMPTY_DAYS))
    monitor = monitor_for(provider)

    await monitor.start()
    report = await monitor.cycle()

    assert provider.browser_opens == 1  # the startup one, and no other
    assert not report.recovered
    assert not report.readings[0].complete


async def test_the_same_session_is_reused_across_healthy_cycles():
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(provider)
    await monitor.start()
    client = monitor.client

    await monitor.cycle()
    await monitor.cycle()
    await monitor.cycle()

    assert monitor.client is client
    assert len(provider.clients) == 1
    assert provider.authentications == 1  # no re-authentication while it works


# --------------------------------------------------------------------------- #
# Timing and lifecycle
# --------------------------------------------------------------------------- #


async def test_the_interval_is_measured_between_scan_starts():
    clock = FakeClock()
    server = ApiServer(days=EMPTY_DAYS, clock=clock, duration=4.0)  # 3 calls -> 12s
    monitor = monitor_for(
        FakeProvider(server), centres=(CENTRE_A, CENTRE_B), interval=60.0, clock=clock
    )
    await monitor.start()

    stop_after_two = 0

    async def run_two() -> None:
        nonlocal stop_after_two
        while stop_after_two < 2:
            started = clock.monotonic()
            await monitor.cycle()
            stop_after_two += 1
            remaining = 60.0 - (clock.monotonic() - started)
            if remaining > 0:
                await _async_sleep(clock)(remaining)

    await run_two()
    # The scan itself took 12s, so the wait is the remaining 48s — not 60.
    assert clock.sleeps == [48.0, 48.0]


async def test_a_scan_longer_than_the_interval_starts_the_next_one_immediately():
    clock = FakeClock()
    server = ApiServer(days=EMPTY_DAYS, clock=clock, duration=40.0)  # 120s per cycle
    monitor = monitor_for(FakeProvider(server), interval=60.0, clock=clock)
    await monitor.start()

    started = clock.monotonic()
    await monitor.cycle()
    remaining = 60.0 - (clock.monotonic() - started)

    assert remaining < 0  # no additional delay is added
    assert clock.sleeps == []


async def test_only_one_scan_runs_at_a_time():
    """The loop is sequential by construction; this proves it observably."""
    depth = 0
    peak = 0

    class Watching(ApiServer):
        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
            nonlocal depth, peak
            depth += 1
            peak = max(peak, depth)
            try:
                return super().__call__(session, url, timeout)
            finally:
                depth -= 1

    monitor = monitor_for(FakeProvider(Watching(days=THREE_DAYS)))
    await monitor.start()
    await monitor.cycle()
    await monitor.cycle()

    assert peak == 1


async def test_once_performs_exactly_one_cycle():
    clock = FakeClock()
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(provider, clock=clock)

    await monitor.run(once=True)

    assert monitor.cycles == 1
    assert clock.sleeps == []  # no waiting after the only cycle
    assert monitor.client is None  # the session was closed on the way out


async def test_a_keyboard_interrupt_closes_the_session_and_does_not_propagate_dirty():
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(provider)

    async def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monitor._sleep = interrupt  # the wait between cycles is where Ctrl+C lands

    with pytest.raises(KeyboardInterrupt):
        await monitor.run()

    assert monitor.cycles == 1
    assert monitor.client is None  # closed by the finally, not left dangling


async def test_the_command_prints_a_clean_stop_on_ctrl_c(tmp_path, capsys):
    from hsc_queue_monitor.cli import EXIT_OK, run_api_monitor

    config = monitor_config(tmp_path)
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))

    async def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    result = await run_api_monitor(
        config, centers=[CENTRE_A], interval=60, provider=provider, sleep=interrupt
    )

    out = capsys.readouterr().out
    assert result == EXIT_OK
    assert "Monitor stopped." in out
    assert "Traceback" not in out


async def test_the_command_runs_one_cycle_and_reports(tmp_path, capsys):
    from hsc_queue_monitor.cli import EXIT_OK, run_api_monitor

    config = monitor_config(tmp_path)
    provider = FakeProvider(ApiServer(days=THREE_DAYS))

    result = await run_api_monitor(config, centers=[CENTRE_A], once=True, provider=provider)

    out = capsys.readouterr().out
    assert result == EXIT_OK
    assert "API MONITOR (dry run)" in out
    assert "BASELINE" in out
    assert "Nothing is booked and nothing is notified." in out


async def test_an_unusable_interval_is_refused_before_the_browser_is_used(tmp_path):
    from hsc_queue_monitor.cli import EXIT_CONFIG, run_api_monitor

    config = monitor_config(tmp_path)
    provider = FakeProvider(ApiServer())

    result = await run_api_monitor(config, centers=[CENTRE_A], interval=0, provider=provider)

    assert result == EXIT_CONFIG
    assert provider.browser_opens == 0


# --------------------------------------------------------------------------- #
# Browser lifecycle
# --------------------------------------------------------------------------- #


async def test_the_browser_opens_once_at_startup_and_not_again():
    provider = FakeProvider(ApiServer(days=THREE_DAYS))
    monitor = monitor_for(provider)

    await monitor.start()
    assert provider.browser_opens == 1

    await monitor.cycle()
    await monitor.cycle()
    await monitor.cycle()

    # Polling is HTTP and nothing else.
    assert provider.browser_opens == 1
    assert not provider.browser_open


async def test_the_client_outlives_the_browser():
    """What comes back is a requests.Session, not anything Playwright-shaped."""
    provider = FakeProvider(ApiServer(days=THREE_DAYS))
    monitor = monitor_for(provider)
    await monitor.start()

    assert not provider.browser_open  # already closed when start() returned
    client = monitor.client
    assert client is not None
    assert isinstance(client.session, requests.Session)
    for attribute in ("page", "context", "browser", "playwright"):
        assert not hasattr(client, attribute)

    # And it still works.
    report = await monitor.cycle()
    assert report.readings[0].complete


async def test_a_failed_startup_still_leaves_no_browser_open():
    provider = FakeProvider(ApiServer(), fails_after=0)
    monitor = monitor_for(provider)

    with pytest.raises(AuthenticationFailed):
        await monitor.start()

    assert not provider.browser_open
    assert monitor.client is None


@pytest.mark.parametrize(
    ("name", "server"),
    [
        ("401", lambda: ApiServer(statuses={"departments": 401}, content_type="text/html")),
        ("429", lambda: ApiServer(statuses={"departments": 429}, content_type="text/html")),
        ("500", lambda: ApiServer(statuses={"departments": 500}, content_type="text/html")),
        ("502", lambda: ApiServer(statuses={"departments": 502}, content_type="text/html")),
        ("302", lambda: ApiServer(statuses={"departments": 302}, content_type="text/html")),
        ("non-json", lambda: HtmlServer(days=EMPTY_DAYS)),
        ("schema", lambda: ApiServer(days={"unexpected": True})),
        ("timeout", lambda: TimingOutServer(timeout_from="", days=THREE_DAYS)),
    ],
)
async def test_only_403_opens_a_browser(name, server):
    good = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    provider = FakeProvider(good)
    monitor = monitor_for(provider)
    await monitor.start()
    await monitor.cycle()
    before = dict(monitor.state[CENTRE_A])

    monitor.client = client_with(server())
    report = await monitor.cycle()

    assert provider.browser_opens == 1, f"{name} opened a browser"
    assert monitor.recoveries == 0
    assert not report.recovered
    assert dict(monitor.state[CENTRE_A]) == before  # nothing was forgotten


class ProviderWithFakeBrowser:
    """The real provider, with a scripted browser instead of Chromium.

    Everything under test is the real code: the ordering, the bootstrap, the
    cookie copy and — the point of this class — that the browser context is
    exited before the session is handed back.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        fetch: Any = None,
        mints: str | None = "queue-session-NEVER-LOG-ME",
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        from test_api_availability import BootstrapPage, build_context

        from hsc_queue_monitor.cli import BrowserSessionProvider

        self.page = BootstrapPage(mints=mints, cookies=cookies)
        config, ctx, auth = build_context(tmp_path, self.page)
        self.ctx = ctx
        self.auth = auth
        self.open = False
        self.closed = False
        self.opens = 0

        provider = BrowserSessionProvider(config, fetch=fetch)
        provider._browser = self._browser  # type: ignore[method-assign]
        self.provider = provider

    @asynccontextmanager
    async def _browser(self) -> Any:
        self.open = True
        self.opens += 1
        try:
            yield self.ctx
        finally:
            self.open = False
            self.closed = True

    async def create_api_session(self) -> ApiSession:
        return await self.provider.create_api_session()


async def test_the_real_provider_closes_the_browser_before_handing_over(tmp_path):
    server = ApiServer(days=EMPTY_DAYS)
    harness = ProviderWithFakeBrowser(tmp_path, fetch=server)

    session = await harness.create_api_session()

    assert harness.opens == 1
    assert harness.closed and not harness.open  # closed on the way out
    assert harness.auth.calls == 1
    assert harness.page.navigations == [QUEUE_URL]  # the bootstrap, nothing else
    assert harness.page.locator_calls == []  # and no wizard control
    assert session.bootstrap is not None and session.bootstrap.worked


async def test_the_returned_client_needs_no_browser(tmp_path):
    server = ApiServer(days=EMPTY_DAYS)
    harness = ProviderWithFakeBrowser(tmp_path, fetch=server)
    session = await harness.create_api_session()

    page_calls = len(harness.page.calls)
    read_centres(session.client, [CENTRE_A])

    # The API work touched the page not at all.
    assert len(harness.page.calls) == page_calls
    assert not harness.open
    assert server.endpoints == ["departments", "days"]


async def test_the_monitor_drives_the_real_provider_once_at_startup(tmp_path):
    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(harness)  # type: ignore[arg-type]

    await monitor.run(once=True)

    assert harness.opens == 1
    assert harness.closed
    assert monitor.cycles == 1


async def test_a_502_does_not_open_a_browser_even_after_its_retries():
    """Retries are a request-level thing; they never become an auth problem."""
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    monitor = monitor_for(provider)
    await monitor.start()

    bad_gateway = ApiServer(statuses={"departments": 502}, content_type="text/html")
    monitor.client = client_with(bad_gateway)
    report = await monitor.cycle()

    # The bounded attempt budget, and no browser.
    assert len(bad_gateway.requests) == 3
    assert provider.browser_opens == 1
    assert not report.readings[0].complete
    assert "502" in report.readings[0].detail


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class FakeStateStore:
    """The monitor's own state, in a variable."""

    def __init__(self, *, state: Any = None, fails: bool = False) -> None:
        self.state = state
        self.saved: list[Any] = []
        self.fails = fails
        self.closed = False

    def load(self) -> Any:
        if self.fails:
            raise SessionStoreError("the database is unreachable")
        return self.state

    def save(self, state: Any) -> None:
        if self.fails:
            raise SessionStoreError("the write was refused")
        self.saved.append(state)
        self.state = state

    def close(self) -> None:
        self.closed = True


class FakeSnapshotStore:
    """The availability snapshot, in a variable."""

    def __init__(self, *, snapshot: Any = None, fails_load: bool = False,
                 fails_save: bool = False) -> None:
        self.snapshot = snapshot
        self.saved: list[Any] = []
        self.fails_load = fails_load
        self.fails_save = fails_save
        self.closed = False

    def load(self) -> Any:
        if self.fails_load:
            raise SessionStoreError("the snapshot could not be read")
        return self.snapshot

    def save(self, snapshot: Any) -> None:
        if self.fails_save:
            raise SessionStoreError("the snapshot could not be written")
        self.saved.append(snapshot)
        self.snapshot = snapshot

    def close(self) -> None:
        self.closed = True


class FakeStore:
    """A session store that lives in a variable.

    It carries the monitor-state store beside it — two documents, one fake — so
    a test can assert on both without threading two objects everywhere.
    """

    def __init__(
        self,
        *,
        stored: PersistedSession | None = None,
        fails_save: bool = False,
        fails_load: bool = False,
    ) -> None:
        self.stored = stored
        self.saves: list[PersistedSession] = []
        self.deletes = 0
        self.closed = False
        self.fails_save = fails_save
        self.fails_load = fails_load
        #: The *other two* documents. Separate, exactly as in MongoDB.
        self.states = FakeStateStore()
        self.snapshots = FakeSnapshotStore()

    @property
    def state(self) -> Any:
        return self.states.state

    def load(self) -> PersistedSession | None:
        if self.fails_load:
            raise SessionStoreError("the database is unreachable")
        return self.stored

    def save(self, session: PersistedSession) -> None:
        if self.fails_save:
            raise SessionStoreError("the write was refused")
        self.saves.append(session)
        self.stored = session

    def delete(self) -> None:
        self.deletes += 1
        self.stored = None

    def close(self) -> None:
        self.closed = True


STORED_COOKIES = (
    {
        "name": "__Secure-auth.access-token",
        "value": "stored-access-token-NEVER-LOG-ME",
        "domain": "eqn.hsc.gov.ua",
        "path": "/",
        "secure": True,
        "expires": None,
    },
    {
        "name": WIZARD_COOKIE,
        "value": "stored-queue-session-NEVER-LOG-ME",
        "domain": "eqn.hsc.gov.ua",
        "path": "/",
        "secure": True,
        "expires": None,
    },
)


def stored_session(**kwargs: Any) -> PersistedSession:
    return PersistedSession(
        cookies=STORED_COOKIES,
        user_agent="Mozilla/5.0 (Macintosh) TestChrome/131.0.0.0",
        updated_at=datetime.now(UTC) - timedelta(seconds=252),
        **kwargs,
    )


def monitor_with_store(
    provider: FakeProvider, store: FakeStore, **kwargs: Any
) -> ApiMonitor:
    monitor = monitor_for(provider, **kwargs)
    monitor.store = store
    return monitor


async def test_a_usable_stored_session_means_no_browser_at_all(caplog):
    caplog.set_level(logging.INFO)
    server = ApiServer(days=EMPTY_DAYS)
    provider = FakeProvider(server)
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(provider, store)

    await monitor.start()
    report = await monitor.cycle()

    assert provider.browser_opens == 0  # Chromium was never launched
    assert provider.restores == 1
    assert report.readings[0].complete  # and the stored jar worked
    # The stored cookies are the ones that went to HSC.
    assert server.cookies_seen[0][WIZARD_COOKIE] == "stored-queue-session-NEVER-LOG-ME"
    assert "Loaded persisted HSC session updated 4m12s ago" in caplog.text


async def test_an_expired_stored_session_goes_straight_to_the_browser(caplog):
    caplog.set_level(logging.INFO)
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    store = FakeStore(
        stored=stored_session(
            queue_session_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    monitor = monitor_with_store(provider, store)

    await monitor.start()

    assert provider.browser_opens == 1
    assert provider.restores == 0
    assert store.deletes == 1  # the stale jar is not left lying about
    assert "Persisted session expired; browser bootstrap required" in caplog.text


async def test_no_stored_session_opens_the_browser_once_and_persists_the_result():
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    store = FakeStore()
    monitor = monitor_with_store(provider, store)

    await monitor.start()

    assert provider.browser_opens == 1
    assert store.saves, "the fresh session was not persisted"
    assert store.saves[-1].names  # cookie names, from the new jar


async def test_a_load_failure_falls_back_to_the_browser(caplog):
    caplog.set_level(logging.WARNING)
    provider = FakeProvider(ApiServer(days=EMPTY_DAYS))
    store = FakeStore(fails_load=True)
    monitor = monitor_with_store(provider, store)

    await monitor.start()
    report = await monitor.cycle()

    assert provider.browser_opens == 1
    assert report.readings[0].complete  # monitoring is unaffected
    assert "Could not read the stored HSC session" in caplog.text


async def test_a_refreshed_queue_cookie_is_written_back(caplog):
    caplog.set_level(logging.INFO)
    # The site rewrites the cookie on departments, days and every slots call.
    server = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [SLOT_0826]},
        sets={"departments": "after-departments", "days": "after-days", "slots": "after-slots"},
    )
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(FakeProvider(server), store)
    await monitor.start()

    await monitor.cycle()

    written = [
        {c["name"]: c["value"] for c in save.cookies}[WIZARD_COOKIE]
        for save in store.saves
    ]
    assert written == ["after-departments", "after-days", "after-slots"]
    assert "Persisted refreshed HSC session" in caplog.text
    # Values never appear in the log, only the fact that a write happened.
    for secret in ("after-departments", "stored-queue-session-NEVER-LOG-ME"):
        assert secret not in caplog.text


async def test_an_unchanged_jar_is_not_written_again():
    server = ApiServer(days=EMPTY_DAYS, sets={})  # HSC changes nothing
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(FakeProvider(server), store)
    await monitor.start()

    await monitor.cycle()
    await monitor.cycle()

    assert store.saves == []  # three cycles of reads, nothing to say


async def test_the_last_written_jar_rebuilds_the_live_session():
    server = ApiServer(days=EMPTY_DAYS)
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(FakeProvider(server), store)
    await monitor.start()
    await monitor.cycle()

    assert monitor.client is not None
    live = {c.name: c.value for c in monitor.client.session.cookies}
    rebuilt = session_from_cookies(store.saves[-1].cookies)

    assert {c.name: c.value for c in rebuilt.cookies} == live


async def test_the_stored_expiry_follows_the_queue_cookie():
    expires = datetime.now(UTC) + timedelta(seconds=900)
    server = ApiServer(days=EMPTY_DAYS, sets={})
    store = FakeStore()
    provider = FakeProvider(server)
    monitor = monitor_with_store(provider, store)
    await monitor.start()

    # The provider's fresh jar carries an expiry the way HSC's Max-Age does.
    assert monitor.client is not None
    monitor.client.session.cookies.set(
        WIZARD_COOKIE,
        "with-expiry",
        domain="eqn.hsc.gov.ua",
        path="/",
        expires=expires.timestamp(),
    )
    monitor._on_response(monitor.client.session)

    saved = store.saves[-1]
    assert saved.queue_session_expires_at is not None
    assert abs((saved.queue_session_expires_at - expires).total_seconds()) < 1


async def test_a_save_failure_never_costs_the_live_session(caplog):
    caplog.set_level(logging.WARNING)
    server = ApiServer(days=EMPTY_DAYS)
    store = FakeStore(stored=stored_session(), fails_save=True)
    monitor = monitor_with_store(FakeProvider(server), store)
    await monitor.start()

    report = await monitor.cycle()

    assert report.readings[0].complete  # the HSC read is untouched
    assert monitor.client is not None
    assert monitor.persistence_degraded
    assert "Could not persist the HSC session" in caplog.text


async def test_degraded_persistence_is_visible_in_the_report():
    store = FakeStore(stored=stored_session(), fails_save=True)
    monitor = monitor_with_store(FakeProvider(ApiServer(days=EMPTY_DAYS)), store)
    await monitor.start()

    rendered = render_cycle(await monitor.cycle(), AT_NOON)

    assert "session persistence is degraded" in rendered
    assert "the HSC session is unaffected" in rendered


def test_the_mongodb_uri_and_key_are_registered_as_secrets(tmp_path, monkeypatch):
    """So the redactor scrubs them out of anything anyone ever logs."""
    from hsc_queue_monitor.config import load_secrets

    monkeypatch.setenv("HSC_MONGODB_URI", "mongodb+srv://u:pw@cluster.example.net/")
    monkeypatch.setenv("HSC_SESSION_ENCRYPTION_KEY", "a-fernet-key-value")
    secrets = load_secrets(env_file=tmp_path / "absent.env")

    assert "mongodb+srv://u:pw@cluster.example.net/" in secrets.redactable()
    assert "a-fernet-key-value" in secrets.redactable()


def test_persisting_without_a_key_is_refused(tmp_path, monkeypatch):
    from hsc_queue_monitor.cli import build_session_store
    from hsc_queue_monitor.config import load_secrets
    from hsc_queue_monitor.models import ConfigError

    monkeypatch.setenv("HSC_MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.delenv("HSC_SESSION_ENCRYPTION_KEY", raising=False)
    config = dataclasses.replace(
        monitor_config(tmp_path), secrets=load_secrets(env_file=tmp_path / "absent.env")
    )

    with pytest.raises(ConfigError, match="HSC_SESSION_ENCRYPTION_KEY"):
        build_session_store(config)


def test_no_mongodb_configured_means_no_persistence(tmp_path, monkeypatch):
    from hsc_queue_monitor.cli import build_session_store

    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    assert build_session_store(monitor_config(tmp_path)) is None


async def test_a_later_save_persists_the_state_that_could_not_be_written():
    server = ApiServer(days=EMPTY_DAYS)
    store = FakeStore(stored=stored_session(), fails_save=True)
    monitor = monitor_with_store(FakeProvider(server), store)
    await monitor.start()
    await monitor.cycle()

    store.fails_save = False
    await monitor.cycle()

    assert store.saves, "the recovered write never happened"
    assert not monitor.persistence_degraded
    current = {c.name: c.value for c in monitor.client.session.cookies}  # type: ignore[union-attr]
    assert {c["name"]: c["value"] for c in store.saves[-1].cookies} == current


async def test_a_403_replaces_the_stored_session():
    refused = ForbiddenServer(days=EMPTY_DAYS)
    healthy = ApiServer(days=EMPTY_DAYS)
    provider = FakeProvider(refused, healthy)
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(provider, store)
    await monitor.start()

    report = await monitor.cycle()

    assert provider.browser_opens == 1  # exactly one, for the 403
    assert store.deletes == 1  # the refused jar is dropped, not kept
    assert store.saves, "the fresh session was not persisted"
    assert report.recovered
    assert not provider.browser_open  # closed again


async def test_shutdown_closes_the_store_as_well_as_the_client():
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(FakeProvider(ApiServer(days=EMPTY_DAYS)), store)

    await monitor.run(once=True)

    assert monitor.client is None
    assert store.closed


async def test_two_centres_with_no_dates_still_cost_three_reads_per_cycle():
    server = ApiServer(days=EMPTY_DAYS)
    store = FakeStore(stored=stored_session())
    monitor = monitor_with_store(
        FakeProvider(server), store, centres=(CENTRE_A, CENTRE_B)
    )
    await monitor.start()

    await monitor.cycle()

    assert server.endpoints == ["departments", "days", "days"]


# --------------------------------------------------------------------------- #
# refresh-session: the local half
# --------------------------------------------------------------------------- #


async def test_refresh_session_authenticates_bootstraps_and_stores(tmp_path, capsys):
    from hsc_queue_monitor.cli import EXIT_OK, run_refresh_session

    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer(days=EMPTY_DAYS))
    store = FakeStore()

    result = await run_refresh_session(
        monitor_config(tmp_path), provider=harness, store=store
    )

    assert result == EXIT_OK
    assert harness.opens == 1 and harness.closed  # browser opened, then closed
    assert harness.auth.calls == 1
    assert harness.page.navigations == [QUEUE_URL]  # the bootstrap, nothing else
    assert harness.page.locator_calls == []
    assert len(store.saves) == 1

    out = capsys.readouterr().out
    assert "HSC SESSION REFRESH" in out
    assert "Authentication: OK" in out
    assert "Queue bootstrap: OK" in out
    assert "Session persistence: MongoDB" in out
    assert "Session saved: OK" in out
    assert "Browser closed." in out
    assert "Session is ready for headless monitoring." in out


async def test_refresh_session_reads_no_availability(tmp_path):
    from hsc_queue_monitor.cli import run_refresh_session

    server = ApiServer(days=EMPTY_DAYS)
    harness = ProviderWithFakeBrowser(tmp_path, fetch=server)

    await run_refresh_session(monitor_config(tmp_path), provider=harness, store=FakeStore())

    # Not one API read: this command exists to mint a session, not to use one.
    assert server.endpoints == []


async def test_refresh_session_stores_the_queue_cookie_it_minted(tmp_path):
    from hsc_queue_monitor.cli import run_refresh_session

    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer(days=EMPTY_DAYS))
    store = FakeStore()

    await run_refresh_session(monitor_config(tmp_path), provider=harness, store=store)

    saved = store.saves[-1]
    assert WIZARD_COOKIE in saved.names
    assert saved.user_agent  # the identity the cookies were minted for
    # And what the headless half will read back is the same jar.
    rebuilt = session_from_cookies(saved.cookies)
    assert WIZARD_COOKIE in {c.name for c in rebuilt.cookies}


async def test_refresh_session_refuses_without_persistence(tmp_path, capsys):
    from hsc_queue_monitor.cli import EXIT_CONFIG, run_refresh_session

    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer())

    result = await run_refresh_session(monitor_config(tmp_path), provider=harness, store=None)

    assert result == EXIT_CONFIG
    assert harness.opens == 0  # refused before the browser was opened
    assert "HSC_MONGODB_URI" in capsys.readouterr().err


async def test_refresh_session_reports_a_missing_queue_cookie(tmp_path, capsys):
    """No queue session means the stored jar would be useless to the monitor."""
    from test_api_availability import cookies_without_queue_session

    from hsc_queue_monitor.cli import EXIT_RUNTIME, run_refresh_session

    # Signed in, but the navigation minted no queue session.
    harness = ProviderWithFakeBrowser(
        tmp_path, fetch=ApiServer(), mints=None, cookies=cookies_without_queue_session()
    )
    store = FakeStore()

    result = await run_refresh_session(
        monitor_config(tmp_path), provider=harness, store=store
    )

    assert result == EXIT_RUNTIME
    assert store.saves == []  # nothing half-usable is written
    assert "Queue bootstrap: FAILED" in capsys.readouterr().err


async def test_refresh_session_reports_a_persistence_failure(tmp_path, capsys):
    from hsc_queue_monitor.api.headless_monitor import EXIT_PERSISTENCE
    from hsc_queue_monitor.cli import run_refresh_session

    harness = ProviderWithFakeBrowser(tmp_path, fetch=ApiServer())
    result = await run_refresh_session(
        monitor_config(tmp_path), provider=harness, store=FakeStore(fails_save=True)
    )

    assert result == EXIT_PERSISTENCE
    assert "Session saved: FAILED" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def identifiers(source: str) -> set[str]:
    """Every name the *code* uses: imports, attributes, calls, variables.

    Prose is excluded on purpose. A docstring that says "nothing here books"
    must not read as booking, and a comment naming Telegram must not read as an
    integration — only what the interpreter would execute counts.
    """
    tree = ast.parse(textwrap.dedent(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return {name.lower() for name in names}


def test_the_monitor_never_reaches_for_telegram():
    for path in (SRC / "api").glob("*.py"):
        used = identifiers(path.read_text(encoding="utf-8"))
        assert not [name for name in used if "telegram" in name or "notif" in name]


def test_the_command_path_notifies_nobody():
    import inspect

    from hsc_queue_monitor.cli import BrowserSessionProvider, run_api_monitor

    for obj in (run_api_monitor, BrowserSessionProvider):
        used = identifiers(inspect.getsource(obj))
        assert not [name for name in used if "telegram" in name or "notif" in name]


def test_the_monitor_introduces_no_mutating_request():
    source = (SRC / "api" / "monitor.py").read_text(encoding="utf-8")
    assert not re.search(
        r"\b(session|requests|client|http|fetch)\.(post|put|patch|delete)\s*\(", source
    )
    used = identifiers(source)
    for forbidden in ("book", "reserve", "submit", "select_option", "click"):
        assert not [name for name in used if forbidden in name]


def test_the_browser_monitor_and_notifier_are_untouched():
    from hsc_queue_monitor.monitor.monitor import Monitor
    from hsc_queue_monitor.notification.telegram import TelegramNotifier

    assert Monitor is not None and TelegramNotifier is not None
    for path in (SRC / "monitor" / "monitor.py", SRC / "notification" / "telegram.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("ApiMonitor", "HscApiClient", "from ..api", "from .api"):
            assert forbidden not in source


def test_nothing_is_written_to_disk():
    used = identifiers((SRC / "api" / "monitor.py").read_text(encoding="utf-8"))
    for forbidden in ("open", "write_text", "dump", "statestore", "pickle"):
        assert forbidden not in used


def test_a_reading_that_failed_carries_no_slots():
    """Structural: an incomplete reading has nowhere to put availability."""
    reading = CentreReading(centre_id=CENTRE_A, complete=False, detail="HTTP 429")
    assert reading.state == {}
    assert reading.slot_count == 0
    assert reading.short() == "partial — HTTP 429"


def test_retrying_is_bounded_and_lives_in_one_place():
    """Retries exist now, but only in the client, and only within a budget."""
    for path in (SRC / "api").glob("*.py"):
        if path.name in {"client.py", "retry.py"}:
            continue
        used = identifiers(path.read_text(encoding="utf-8"))
        for forbidden in ("backoff", "max_retries", "urllib3"):
            assert forbidden not in used, f"{path.name} mentions {forbidden}"

    # The loop is over a fixed range, never a while-true.
    tree = ast.parse((SRC / "api" / "client.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_get":
            loops = [n for n in ast.walk(node) if isinstance(n, ast.While | ast.For)]
            assert len(loops) == 1
            assert isinstance(loops[0], ast.For)  # bounded by construction

    # And the policy itself has no randomness to make timing unpredictable.
    assert "random" not in identifiers((SRC / "api" / "retry.py").read_text(encoding="utf-8"))


def test_the_monitor_owns_no_browser_object():
    """Playwright appears nowhere in the API package, provider protocol aside."""
    for path in (SRC / "api").glob("*.py"):
        used = identifiers(path.read_text(encoding="utf-8"))
        for forbidden in ("playwright", "browsermanager", "chromium", "new_page"):
            assert forbidden not in used, f"{path.name} reaches for {forbidden}"


def test_asyncio_is_only_used_for_waiting_and_threading():
    """No concurrency: no gather, no task groups, no parallel slot fetching."""
    source = (SRC / "api" / "monitor.py").read_text(encoding="utf-8")
    used = identifiers(source)
    for forbidden in ("gather", "taskgroup", "create_task", "as_completed", "wait_for"):
        assert forbidden not in used
    assert "to_thread" in used  # one thread, sequential inside it


def test_the_shipped_configuration_has_a_monitor_interval():
    from hsc_queue_monitor.config import AppSettings

    shipped = AppSettings.from_file(PROJECT_ROOT / "config" / "app.yaml")
    # 300s against the measured 900s queue-session lifetime.
    assert shipped.api.monitor_interval_seconds == 300.0


@pytest.mark.parametrize("value", [0, -5, 3601, "soon", None, True])
def test_a_malformed_monitor_interval_is_rejected(value):
    from hsc_queue_monitor.config import ApiConfig
    from hsc_queue_monitor.models import ConfigError

    with pytest.raises(ConfigError):
        ApiConfig.from_dict({"monitor_interval_seconds": value})
