"""Availability read through the API: departments -> days -> slots.

The fake below is the measured API as a small routing table — it answers the
three endpoints, records exactly what was asked of it, and rewrites the
wizard-session cookie the way the live site does. Nothing here touches the
network or a browser.

The browser-side fakes come from :mod:`test_api_probe`, the same way this
feature's client builds on the probe's session bridge.

As in the UI scanner's tests, the safety boundary is tested as hard as the
feature: a run that resolved the wrong centre, hammered a 429 or learned to say
POST would pass every functional test in this file and be a serious bug.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_api_probe import (
    ACCESS_TOKEN_VALUE,
    CSRF_VALUE,
    EQUEUE_VALUE,
    IDGOV_VALUE,
    SECRET_VALUES,
    USER_AGENT,
    FakeAuth,
    FakeHttpResponse,
    ProbePage,
)

from hsc_queue_monitor.api.availability import (
    ApiSchemaUnknown,
    Department,
    DepartmentUnresolved,
    parse_days,
    parse_slots,
    read_clock,
    read_strict_clock,
    render_api_availability,
    resolve_department,
    scan_centre,
)
from hsc_queue_monitor.api.client import (
    DEFAULT_SERVICE_ID,
    PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A,
    ApiRequestFailed,
    HscApiClient,
    slot_date_param,
)
from hsc_queue_monitor.api.probe import WIZARD_COOKIE, build_session, fingerprint, hsc_cookies
from hsc_queue_monitor.api.retry import RetryConfig
from hsc_queue_monitor.cli import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RUNTIME,
    run_api_availability,
)
from hsc_queue_monitor.config import (
    ApiConfig,
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SelectorRegistry,
    load_secrets,
)
from hsc_queue_monitor.flow.steps import FlowContext
from hsc_queue_monitor.models import ServiceCenter, TimeSlot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"

CABINET = "https://eqn.hsc.gov.ua/cabinet"

CENTRE_3242 = ServiceCenter(
    name="ТСЦ МВС № 3242",
    id="3242",
    full_name="ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20",
)

NAME_3242 = "ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20"

#: The internal id is a property of *this response*, never of the centre. Live
#: runs have returned 2 and 100 for the same visible 3242, so no fixture pins
#: one: the tests take it from the response they were given, exactly as the
#: resolver does. The trap stays — a *different* centre is internally id=3242.
def departments_with(internal_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": 12,
            "name": "ТСЦ МВС № 8043 м. Київ, вул. Кустанайська 15",
            "allowOnlineCount": 0,
        },
        {"id": internal_id, "name": NAME_3242, "allowOnlineCount": 1},
        {"id": 3242, "name": "ТСЦ МВС № 4641 м. Київ, вул. Лугова 19", "allowOnlineCount": 2},
    ]


AUG_21 = date(2026, 8, 21)
AUG_26 = date(2026, 8, 26)

DAYS: list[dict[str, Any]] = [
    {"date": "2026-08-21T00:00:00", "freeCount": 2},
    {"date": "2026-08-26T00:00:00", "freeCount": 1},
]

#: The measured /slots record, exactly as the live API returned it.
SLOTS: dict[str, list[dict[str, Any]]] = {
    "2026-08-21T00:00:00": [
        {"startTime": "09:20:00", "stopTime": "09:46:00"},
        {"startTime": "10:40:00", "stopTime": "11:06:00"},
    ],
    "2026-08-26T00:00:00": [{"startTime": "08:26:00", "stopTime": "08:52:00"}],
}

EQUEUE_AFTER_DEPARTMENTS = "wizard-state-after-departments-NEVER-LOG-ME"


# --------------------------------------------------------------------------- #
# The fake API
# --------------------------------------------------------------------------- #


class ApiServer:
    """The three measured endpoints, as a routing table.

    Records the full URL, the headers and the cookies of every request, so the
    tests can assert on what was actually sent rather than on what was intended.
    """

    def __init__(
        self,
        *,
        departments: Any = None,
        days: Any = None,
        slots: Any = None,
        statuses: dict[str, int] | None = None,
        content_type: str = "application/json",
        sets: dict[str, str] | None = None,
        internal_id: int = 2,
        events: list[str] | None = None,
        clock: FakeClock | None = None,
        duration: float = 0.0,
    ) -> None:
        #: How long each request "takes", on the fake clock.
        self.clock = clock
        self.duration = duration
        #: Shared ordering log, when a test cares where the calls sit relative
        #: to authentication and the queue navigation.
        self.events = events
        #: What *this* response says centre 3242 is called internally. Tests
        #: assert against it rather than against a literal.
        self.internal_id = internal_id
        self.payloads = {
            "departments": departments_with(internal_id)
            if departments is None
            else departments,
            "days": DAYS if days is None else days,
            "slots": SLOTS if slots is None else slots,
        }
        self.statuses = statuses or {}
        self.content_type = content_type
        #: endpoint -> the value it rewrites the wizard cookie to.
        self.sets = sets if sets is not None else {"departments": EQUEUE_AFTER_DEPARTMENTS}

        self.requests: list[str] = []
        self.timeouts: list[tuple[float, float]] = []
        self.endpoints: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.cookies_seen: list[dict[str, str]] = []
        self.session_ids: list[int] = []

    def __call__(
        self, session: Any, url: str, timeout: tuple[float, float] = (5, 60)
    ) -> FakeHttpResponse:
        if self.events is not None:
            self.events.append("fetch")
        self.timeouts.append(timeout)
        parts = urlsplit(url)
        endpoint = parts.path.rsplit("/", 1)[-1]
        query = parse_qs(parts.query)

        self.requests.append(url)
        self.endpoints.append(endpoint)
        self.queries.append(query)
        self.headers_seen.append(dict(session.headers))
        self.cookies_seen.append({c.name: c.value for c in session.cookies})
        self.session_ids.append(id(session))

        new_value = self.sets.get(endpoint)
        if new_value is not None:
            session.cookies.set(WIZARD_COOKIE, new_value, domain="eqn.hsc.gov.ua", path="/")

        status = self.statuses.get(endpoint, 200)
        payload = self.payloads[endpoint]
        if endpoint == "slots" and isinstance(payload, dict):
            payload = payload.get(query.get("date", [""])[0], [])

        if self.clock is not None and self.duration:
            self.clock.advance(self.duration)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return FakeHttpResponse(status, {"Content-Type": self.content_type}, body)

    # -- convenience -------------------------------------------------------
    def query_for(self, endpoint: str) -> dict[str, list[str]]:
        index = self.endpoints.index(endpoint)
        return self.queries[index]

    def queries_for(self, endpoint: str) -> list[dict[str, list[str]]]:
        return [q for e, q in zip(self.endpoints, self.queries, strict=True) if e == endpoint]

    def dates_requested(self) -> list[str]:
        """The distinct dates asked about, in order.

        Retries repeat a physical request; they do not ask about a new date.
        These tests are about the *scan*, so they count dates, not attempts.
        """
        seen: list[str] = []
        for query in self.queries_for("slots"):
            date = query["date"][0]
            if date not in seen:
                seen.append(date)
        return seen


QUEUE_URL = "https://eqn.hsc.gov.ua/cabinet/queue"
MINTED_EQUEUE_VALUE = "queue-session-minted-by-navigation-NEVER-LOG-ME"

SELECTORS = {
    "login": {"authenticated_marker": {"strategy": "text", "value": "Записатись у чергу"}}
}
FLOW = {
    "site": {
        "base_url": "https://eqn.hsc.gov.ua",
        "cabinet_url": CABINET,
        "queue_url": QUEUE_URL,
    },
}


class FakeClock:
    """A monotonic clock nobody has to wait for.

    ``sleep`` moves it forward, which is what makes "the request already used
    most of the interval" testable without spending the interval.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BootstrapPage(ProbePage):
    """A page whose queue navigation can mint the queue-session cookie.

    ``mints`` reproduces the hypothesis being tested; ``redirect_to`` reproduces
    HSC bouncing the navigation somewhere else.
    """

    def __init__(
        self,
        *,
        mints: str | None = None,
        redirect_to: str | None = None,
        events: list[str] | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        # Always a private copy: the navigation appends to this jar.
        super().__init__(BROWSER_COOKIES() if cookies is None else cookies)
        self._mints = mints
        self._redirect_to = redirect_to
        self._events = events

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("goto", (url,), kwargs))
        if self._events is not None:
            self._events.append("goto")
        if self._mints is not None:
            self.context._cookies.append(
                {
                    "name": WIZARD_COOKIE,
                    "value": self._mints,
                    "domain": "eqn.hsc.gov.ua",
                    "path": "/",
                    "secure": True,
                }
            )
        self.url = self._redirect_to or url

    @property
    def locator_calls(self) -> list[str]:
        """Every call that would have touched a wizard control."""
        return [
            api
            for api, _args, _kwargs in self.calls
            if api.startswith("get_by") or api == "locator"
        ]


def build_context(
    tmp_path: Path, page: ProbePage, events: list[str] | None = None
) -> tuple[AppConfig, FlowContext, FakeAuth]:
    config = AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        # No pacing: a test that waits out the shipped 3s between dates is a
        # test of time.sleep. The pacing itself is asserted with a fake clock.
        app=AppSettings(api=ApiConfig(slot_request_interval_seconds=0.0)),
        paths=Paths(data_dir=tmp_path),
        service_centers=[CENTRE_3242],
        _selectors=SelectorRegistry.from_dict(SELECTORS),
        _flow=FlowConfig.from_dict(FLOW),
    )
    ctx = FlowContext(config=config, page=page)
    auth = FakeAuth(events if events is not None else [])
    ctx.auth = auth  # type: ignore[assignment]
    return config, ctx, auth


def client_with(server: ApiServer, **kwargs: Any) -> HscApiClient:
    session = build_session(hsc_cookies(BROWSER_COOKIES()), user_agent=USER_AGENT)
    # Nothing in the tests waits out a real backoff: the policy is asserted on
    # what it *asks* for, through the recorded waits.
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HscApiClient(session, fetch=server, **kwargs)


def BROWSER_COOKIES() -> list[dict[str, Any]]:  # noqa: N802 - reads as a constant
    """A fresh copy per test: the session mutates its jar."""
    from test_api_probe import BROWSER_COOKIES as COOKIES

    return [dict(cookie) for cookie in COOKIES]


def cookies_without_queue_session() -> list[dict[str, Any]]:
    """The live 204 state: signed in, but no queue session at all."""
    return [c for c in BROWSER_COOKIES() if c["name"] != WIZARD_COOKIE]


# --------------------------------------------------------------------------- #
# The service id
# --------------------------------------------------------------------------- #


def test_the_measured_service_is_named_once_and_used_everywhere():
    assert PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A == 47
    assert DEFAULT_SERVICE_ID == PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A

    server = ApiServer()
    scan_centre(client_with(server), "3242")

    assert server.requests, "the scan made no requests"
    for query in server.queries:
        assert query["serviceId"] == ["47"]

    # One place builds the parameter. Everywhere else that mentions serviceId is
    # documenting the measured URL, not deciding which queue is read.
    for path in SRC.rglob("*.py"):
        if path.name == "client.py":
            continue
        assert '"serviceId"' not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Departments -> internal id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("internal_id", [2, 100, 7431])
def test_the_centre_is_resolved_by_name_not_by_internal_id(internal_id):
    department = resolve_department(departments_with(internal_id), "3242")
    # The trap: a *different* department is internally id=3242.
    assert department.department_id == internal_id
    assert department.display_name == NAME_3242
    assert department.allow_online_count == 1


def test_the_internal_id_may_differ_between_runs_for_the_same_centre():
    """3242 resolved to 100 once and to 2 on the live run. Both must work.

    This is why there is no persisted or hardcoded mapping anywhere: the id is a
    property of the response, and re-using yesterday's would silently query some
    other service centre.
    """
    first = resolve_department(departments_with(100), "3242")
    second = resolve_department(departments_with(2), "3242")

    assert (first.department_id, second.department_id) == (100, 2)
    assert first.display_name == second.display_name == NAME_3242
    # The visible number never changes, and is never the internal id.
    assert CENTRE_3242.id == "3242"
    assert str(second.department_id) != CENTRE_3242.id


def test_a_wrapped_departments_list_resolves_too():
    assert resolve_department({"data": departments_with(2)}, "3242").department_id == 2


def test_a_centre_number_never_matches_a_longer_number():
    departments = [{"id": 7, "name": "ТСЦ МВС № 13242 м. Львів", "allowOnlineCount": 1}]
    with pytest.raises(DepartmentUnresolved):
        resolve_department(departments, "3242")


def test_an_ambiguous_centre_is_refused_rather_than_guessed():
    departments = [
        {"id": 100, "name": NAME_3242, "allowOnlineCount": 1},
        {"id": 101, "name": "ТСЦ МВС № 3242 (тимчасовий)", "allowOnlineCount": 0},
    ]
    with pytest.raises(DepartmentUnresolved) as excinfo:
        resolve_department(departments, "3242")
    assert len(excinfo.value.matches) == 2
    assert "not safe to pick one" in str(excinfo.value)


def test_an_unknown_centre_lists_what_was_returned():
    with pytest.raises(DepartmentUnresolved) as excinfo:
        resolve_department(departments_with(2), "9999")
    assert "ТСЦ МВС № 8043" in str(excinfo.value)


def test_a_departments_response_without_the_measured_fields_stops_cleanly():
    with pytest.raises(ApiSchemaUnknown) as excinfo:
        resolve_department([{"code": 100, "title": NAME_3242}], "3242")
    assert excinfo.value.what == "Departments"
    assert any("code" in line for line in excinfo.value.summary)


# --------------------------------------------------------------------------- #
# Days
# --------------------------------------------------------------------------- #


def test_days_are_read_from_objects_with_one_date_field():
    assert parse_days(DAYS) == [AUG_21, AUG_26]


def test_days_are_read_from_plain_strings_too():
    assert parse_days(["2026-08-26", "2026-08-21T00:00:00"]) == [AUG_21, AUG_26]


def test_a_wrapped_days_list_is_read():
    assert parse_days({"days": DAYS}) == [AUG_21, AUG_26]


def test_an_empty_days_response_is_an_answer_not_an_error():
    assert parse_days([]) == []


def test_two_possible_date_fields_stop_the_run_instead_of_being_guessed():
    ambiguous = [{"date": "2026-08-21T00:00:00", "createdAt": "2026-08-01T10:00:00"}]
    with pytest.raises(ApiSchemaUnknown) as excinfo:
        parse_days(ambiguous)
    assert excinfo.value.what == "Days"
    assert "exactly one is needed" in str(excinfo.value)


def test_a_days_response_that_is_not_a_list_stops_the_run():
    with pytest.raises(ApiSchemaUnknown) as excinfo:
        parse_days({"state": "WIZARD", "step": 2})
    assert any("payload: object" in line for line in excinfo.value.summary)


# --------------------------------------------------------------------------- #
# Slots
# --------------------------------------------------------------------------- #


#: The one record the live API was measured returning.
MEASURED_SLOT = {"startTime": "08:26:00", "stopTime": "08:52:00"}


def test_the_measured_slot_record_becomes_a_time_slot():
    slots = parse_slots([MEASURED_SLOT])

    assert slots == [TimeSlot(time=time(8, 26), text="08:26", end_time=time(8, 52))]
    assert slots[0].time == time(8, 26)
    assert slots[0].end_time == time(8, 52)
    assert slots[0].display == "08:26"
    assert slots[0].display_range == "08:26-08:52"


def test_the_measured_list_is_parsed_in_order():
    payload = [
        {"startTime": "10:40:00", "stopTime": "11:06:00"},
        MEASURED_SLOT,
        {"startTime": "09:20:00", "stopTime": "09:46:00"},
    ]
    assert [slot.display_range for slot in parse_slots(payload)] == [
        "08:26-08:52",
        "09:20-09:46",
        "10:40-11:06",
    ]


def test_a_sixteen_slot_response_is_read_whole():
    payload = [
        {"startTime": f"{8 + index // 2:02d}:{(index % 2) * 30:02d}:00",
         "stopTime": f"{8 + index // 2:02d}:{(index % 2) * 30 + 26:02d}:00"}
        for index in range(16)
    ]
    assert len(parse_slots(payload)) == 16


def test_an_empty_slots_list_means_nothing_free_not_a_broken_schema():
    assert parse_slots([]) == []
    assert parse_slots({"slots": []}) == []


@pytest.mark.parametrize(
    "record",
    [
        {"stopTime": "08:52:00"},  # missing startTime
        {"startTime": "08:26:00"},  # missing stopTime
        {"startTime": "8:26", "stopTime": "08:52:00"},  # malformed startTime
        {"startTime": "08:26:00", "stopTime": "later"},  # malformed stopTime
        {"startTime": "08:26:00", "stopTime": ""},
        {"startTime": "2026-08-26T08:26:00", "stopTime": "08:52:00"},  # not a clock
        {"startTime": "08:26:00+02:00", "stopTime": "08:52:00"},  # unmeasured offset
        {"startTime": None, "stopTime": "08:52:00"},
    ],
)
def test_a_malformed_measured_record_stops_the_run(record):
    with pytest.raises(ApiSchemaUnknown) as excinfo:
        parse_slots([record])
    assert excinfo.value.what == "Slots"


def test_one_bad_record_fails_the_list_rather_than_being_skipped():
    payload = [MEASURED_SLOT, {"startTime": "09:20:00"}, MEASURED_SLOT]

    with pytest.raises(ApiSchemaUnknown) as excinfo:
        parse_slots(payload)
    # Partially parsing would report 2 free times where the API offered 3.
    assert "Nothing is skipped" in str(excinfo.value)


def test_strict_clock_parsing_refuses_anything_unmeasured():
    assert read_strict_clock("08:26:00") == time(8, 26)
    assert read_strict_clock("08:26") == time(8, 26)
    for rejected in ("8:26", "08:26:00.500", "08:26:00Z", "2026-08-26T08:26:00", "", 826):
        assert read_strict_clock(rejected) is None


def test_slots_can_still_be_plain_strings():
    assert [s.display for s in parse_slots(["09:20", "10:40"])] == ["09:20", "10:40"]


def test_an_unmeasured_object_shape_is_still_read_when_unambiguous():
    assert parse_slots([{"beginAt": "2026-08-26T14:00:00"}])[0].time == time(14, 0)


def test_a_date_only_value_is_never_read_as_a_midnight_slot():
    assert read_clock("2026-08-26") is None
    with pytest.raises(ApiSchemaUnknown):
        parse_slots([{"date": "2026-08-26"}])


def test_an_unfamiliar_slot_shape_stops_cleanly():
    with pytest.raises(ApiSchemaUnknown) as excinfo:
        parse_slots([{"slotId": 12, "free": True}])
    assert excinfo.value.what == "Slots"


# --------------------------------------------------------------------------- #
# The sequence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("internal_id", [2, 100])
def test_the_sequence_is_departments_then_days_then_one_slots_call_per_date(internal_id):
    server = ApiServer(internal_id=internal_id)
    scan = scan_centre(client_with(server), "3242")

    assert server.endpoints == ["departments", "days", "slots", "slots"]
    # Whatever this response called it, that is what the later calls use.
    resolved = [str(internal_id)]
    assert server.query_for("days")["departmentId"] == resolved
    assert [q["departmentId"] for q in server.queries_for("slots")] == [resolved, resolved]
    assert [q["date"][0] for q in server.queries_for("slots")] == [
        "2026-08-21T00:00:00",
        "2026-08-26T00:00:00",
    ]

    assert scan.department.department_id == internal_id
    assert scan.dates == (AUG_21, AUG_26)
    assert [d.date for d in scan.availability] == [AUG_21, AUG_26]
    assert [s.display_range for s in scan.availability[0].slots] == [
        "09:20-09:46",
        "10:40-11:06",
    ]
    assert [s.display_range for s in scan.availability[1].slots] == ["08:26-08:52"]
    assert scan.slot_count == 3 and scan.bookable
    assert scan.status == "bookable"


def test_every_date_is_scanned_when_max_dates_is_not_given():
    server = ApiServer(
        days=[{"date": f"2026-09-{day:02d}T00:00:00"} for day in range(1, 8)],
        slots={f"2026-09-{day:02d}T00:00:00": [MEASURED_SLOT] for day in range(1, 8)},
    )
    scan = scan_centre(client_with(server), "3242")

    assert server.endpoints.count("slots") == 7
    assert len(scan.availability) == 7
    assert scan.skipped_dates == 0


def test_the_date_parameter_is_local_midnight_without_a_timezone():
    assert slot_date_param(AUG_26) == "2026-08-26T00:00:00"
    assert not re.search(r"(Z|[+-]\d{2}:\d{2})$", slot_date_param(AUG_26))


def test_every_call_shares_one_session_so_set_cookie_carries_forward():
    server = ApiServer()
    client = client_with(server)
    scan_centre(client, "3242")

    # The same object, not a new one per call.
    assert set(server.session_ids) == {id(client.session)}
    # The site rewrote the wizard cookie on the departments response; every
    # later request carried the new value.
    assert server.cookies_seen[0][WIZARD_COOKIE] == EQUEUE_VALUE
    assert server.cookies_seen[1][WIZARD_COOKIE] == EQUEUE_AFTER_DEPARTMENTS
    assert server.cookies_seen[-1][WIZARD_COOKIE] == EQUEUE_AFTER_DEPARTMENTS


def test_max_dates_limits_the_slots_calls():
    server = ApiServer()
    scan = scan_centre(client_with(server), "3242", max_dates=1)

    assert server.endpoints.count("slots") == 1
    assert scan.dates == (AUG_21, AUG_26)  # every date is still reported
    assert scan.skipped_dates == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refusal_stops_the_sequence_without_a_retry(status):
    server = ApiServer(statuses={"days": status}, content_type="text/html")

    with pytest.raises(ApiRequestFailed) as excinfo:
        scan_centre(client_with(server), "3242")

    # departments, then the refused days call (retried, then given up on), and
    # no slots call at all.
    assert set(server.endpoints) == {"departments", "days"}
    assert excinfo.value.call.outcome.status == status
    assert "slots" not in server.endpoints


# --------------------------------------------------------------------------- #
# Partial results
# --------------------------------------------------------------------------- #


THREE_DAYS = [
    {"date": "2026-08-21T00:00:00"},
    {"date": "2026-08-26T00:00:00"},
    {"date": "2026-08-27T00:00:00"},
]


class RefusingServer(ApiServer):
    """Answers normally until the slots request for ``refuse_from``."""

    def __init__(self, *, refuse_from: str, status: int = 429, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refuse_from = refuse_from
        self.refuse_status = status

    def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
        response = super().__call__(session, url, timeout)
        if self.endpoints[-1] == "slots" and self.queries[-1]["date"][0] >= self.refuse_from:
            return FakeHttpResponse(
                self.refuse_status, {"Content-Type": "application/json"}, b'"slow down"'
            )
        return response


def test_a_429_on_the_second_date_keeps_the_first_dates_slots():
    server = RefusingServer(refuse_from="2026-08-27T00:00:00", days=THREE_DAYS)
    scan = scan_centre(client_with(server), "3242")

    assert [day.date for day in scan.availability] == [AUG_21, AUG_26, date(2026, 8, 27)]
    assert [slot.display_range for slot in scan.availability[0].slots] == [
        "09:20-09:46",
        "10:40-11:06",
    ]
    assert [slot.display_range for slot in scan.availability[1].slots] == ["08:26-08:52"]
    assert scan.slot_count == 3  # the refusal cost nothing that was already read


def test_the_refused_date_carries_the_error_and_the_scan_is_partial():
    server = RefusingServer(refuse_from="2026-08-27T00:00:00", days=THREE_DAYS)
    scan = scan_centre(client_with(server), "3242")

    failed = scan.failed_dates
    assert [day.date for day in failed] == [date(2026, 8, 27)]
    assert failed[0].error == "HTTP 429 Too Many Requests"
    assert failed[0].slots == ()

    # Real slots were found, and a date was refused: neither word alone is honest.
    assert scan.bookable
    assert scan.status == "partial"
    assert not scan.complete


def test_no_further_date_is_requested_after_a_429():
    days = [*THREE_DAYS, {"date": "2026-08-28T00:00:00"}]
    server = RefusingServer(refuse_from="2026-08-26T00:00:00", days=days)
    scan = scan_centre(client_with(server), "3242")

    assert server.dates_requested() == ["2026-08-21T00:00:00", "2026-08-26T00:00:00"]
    assert scan.skipped_dates == 2
    assert "429" in scan.stopped


def test_a_refused_date_is_not_revisited_once_the_scan_moves_on():
    server = RefusingServer(refuse_from="2026-08-21T00:00:00", days=THREE_DAYS)
    scan_centre(client_with(server), "3242")

    # The client spends its bounded retry budget on that one date and stops;
    # no later date is attempted, and the date is not revisited afterwards.
    assert server.dates_requested() == ["2026-08-21T00:00:00"]
    assert server.endpoints.count("slots") == 3  # the attempt budget, exactly


def test_every_attempted_date_is_requested_exactly_once_and_in_order():
    server = ApiServer(days=THREE_DAYS, slots={})
    scan_centre(client_with(server), "3242")

    requested = [q["date"][0] for q in server.queries_for("slots")]
    assert requested == [
        "2026-08-21T00:00:00",
        "2026-08-26T00:00:00",
        "2026-08-27T00:00:00",
    ]
    assert len(requested) == len(set(requested))
    # Sequential by construction: one session, one thread, calls in date order.
    assert len(set(server.session_ids)) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_other_refusals_also_stop_the_scan_but_keep_what_was_read(status):
    server = RefusingServer(
        refuse_from="2026-08-26T00:00:00", status=status, days=THREE_DAYS
    )
    scan = scan_centre(client_with(server), "3242")

    assert scan.availability[0].has_slots
    assert scan.failed_dates[0].error.startswith(f"HTTP {status}")
    # An answer, so not retried: one attempt at that date, and no third date.
    assert server.endpoints.count("slots") == 2


def test_a_content_level_oddity_is_recorded_and_the_scan_continues():
    """A 500 on one date says nothing about the next one, so the scan goes on."""
    server = RefusingServer(
        refuse_from="2026-08-26T00:00:00", status=500, days=THREE_DAYS
    )
    scan = scan_centre(client_with(server), "3242")

    assert server.dates_requested() == [
        "2026-08-21T00:00:00",
        "2026-08-26T00:00:00",
        "2026-08-27T00:00:00",
    ]  # every date was still attempted
    assert [day.date for day in scan.failed_dates] == [AUG_26, date(2026, 8, 27)]
    assert scan.failed_dates[0].error == "HTTP 500 Internal Server Error"
    assert scan.status == "partial"


def test_an_unreadable_slots_schema_keeps_the_dates_already_read():
    server = ApiServer(
        days=THREE_DAYS,
        slots={
            "2026-08-21T00:00:00": [MEASURED_SLOT],
            "2026-08-26T00:00:00": [{"slotId": 3, "free": True}],
        },
    )
    scan = scan_centre(client_with(server), "3242")

    assert scan.availability[0].has_slots  # not discarded
    assert scan.schema_stop is not None and scan.schema_stop.what == "Slots"
    assert scan.availability[1].error == "Slots response not recognised"
    assert server.endpoints.count("slots") == 2  # it stopped there
    assert scan.status == "partial"


class TimingOutServer(ApiServer):
    """Answers normally, then times out from ``timeout_from`` onwards."""

    def __init__(self, *, timeout_from: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.timeout_from = timeout_from

    def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> FakeHttpResponse:
        import requests

        response = super().__call__(session, url, timeout)
        if self.endpoints[-1] == "slots" and self.queries[-1]["date"][0] >= self.timeout_from:
            raise requests.ReadTimeout("read timed out")
        return response


def test_a_timed_out_date_is_partial_and_keeps_what_was_read():
    """Measured live: /slots read-timed-out on an endpoint that had answered."""
    server = TimingOutServer(timeout_from="2026-08-26T00:00:00", days=THREE_DAYS)
    scan = scan_centre(client_with(server), "3242")

    assert scan.availability[0].has_slots  # 2026-08-21 survived
    failed = scan.failed_dates
    assert [day.date for day in failed] == [AUG_26]
    # The same wording the live run printed, with the new budget in it.
    assert failed[0].error == "timeout: ReadTimeout after 5s/60s (connect/read)"
    assert scan.status == "partial"

    # The timed-out date is retried within its budget, and then the scan stops:
    # no later date is attempted.
    assert server.dates_requested() == ["2026-08-21T00:00:00", "2026-08-26T00:00:00"]


def test_the_client_uses_the_configured_timeout():
    from hsc_queue_monitor.config import ApiConfig

    assert ApiConfig.from_dict({}).timeout == (5.0, 60.0)
    assert HscApiClient(build_session([])).timeout == (5, 60)
    assert HscApiClient(build_session([]), timeout=(5, 120)).timeout == (5, 120)


def test_the_shipped_configuration_allows_a_slow_read():
    from hsc_queue_monitor.config import AppSettings

    shipped = AppSettings.from_file(PROJECT_ROOT / "config" / "app.yaml")
    assert shipped.api.timeout == (5.0, 60.0)


@pytest.mark.parametrize("value", [0, -1, 301, "soon", None, True])
def test_a_malformed_timeout_is_rejected(value):
    from hsc_queue_monitor.config import ApiConfig
    from hsc_queue_monitor.models import ConfigError

    with pytest.raises(ConfigError):
        ApiConfig.from_dict({"read_timeout_seconds": value})
    with pytest.raises(ConfigError):
        ApiConfig.from_dict({"connect_timeout_seconds": value})


# --------------------------------------------------------------------------- #
# The one retry policy
# --------------------------------------------------------------------------- #


class Scripted:
    """A fetch that answers from a script, recording the timeout of each call."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.timeouts: list[tuple[float, float]] = []
        self.urls: list[str] = []

    def __call__(
        self, session: Any, url: str, timeout: tuple[float, float] = (5, 60)
    ) -> FakeHttpResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        answer = self.answers[min(len(self.urls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def calls(self) -> int:
        return len(self.urls)


def responds(status: int, body: bytes = b"[]", **headers: str) -> FakeHttpResponse:
    return FakeHttpResponse(status, {"Content-Type": "application/json", **headers}, body)


def scripted_client(*answers: Any, **kwargs: Any) -> tuple[HscApiClient, Scripted, list[float]]:
    """A client whose waits are recorded instead of slept."""
    fetch = Scripted(*answers)
    waits: list[float] = []
    kwargs.setdefault("sleep", waits.append)
    client = HscApiClient(build_session(hsc_cookies(BROWSER_COOKIES())), fetch=fetch, **kwargs)
    return client, fetch, waits


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_status_is_retried_until_it_answers(status):
    client, fetch, waits = scripted_client(responds(status, b'"busy"'), responds(200, b"[]"))

    call = client.departments()

    assert fetch.calls == 2
    assert fetch.urls[0] == fetch.urls[1]  # the same request, not a different one
    assert call.outcome.status == 200 and call.ok
    assert call.attempts == 2
    assert waits == [2.0]  # the first backoff, once


@pytest.mark.parametrize(
    "failure",
    [
        requests.ReadTimeout("slow"),
        requests.ConnectTimeout("slow"),
        requests.ConnectionError("reset"),
    ],
)
def test_a_transport_failure_is_retried(failure):
    client, fetch, _waits = scripted_client(failure, responds(200, b"[]"))

    call = client.departments()

    assert fetch.calls == 2
    assert call.ok


def test_the_attempt_budget_is_strict():
    client, fetch, waits = scripted_client(responds(502, b'"gateway"'))

    call = client.departments()

    assert fetch.calls == 3  # three attempts, and no fourth
    assert waits == [2.0, 4.0]  # two waits between them
    assert call.outcome.status == 502 and not call.ok
    assert call.attempts == 3


def test_the_backoff_widens_deterministically():
    client, fetch, waits = scripted_client(
        responds(500, b'"no"'), retry=RetryConfig(max_attempts=4, initial_backoff_seconds=1.0)
    )

    client.departments()

    assert fetch.calls == 4
    assert waits == [1.0, 2.0, 4.0]  # x2 each time, and nothing random


def test_the_backoff_is_capped():
    client, _fetch, waits = scripted_client(
        responds(500, b'"no"'),
        retry=RetryConfig(max_attempts=4, initial_backoff_seconds=10.0, max_backoff_seconds=15.0),
    )

    client.departments()

    assert waits == [10.0, 15.0, 15.0]  # never past the ceiling


def test_one_attempt_means_no_retry_at_all():
    client, fetch, waits = scripted_client(
        responds(502, b'"gateway"'), retry=RetryConfig(max_attempts=1)
    )

    client.departments()

    assert fetch.calls == 1
    assert waits == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_an_answer_is_never_retried(status):
    client, fetch, waits = scripted_client(responds(status, b'"no"'), responds(200))

    client.departments()

    assert fetch.calls == 1  # asking again would produce the same answer
    assert waits == []


def test_a_body_that_is_not_json_is_not_retried():
    client, fetch, _waits = scripted_client(
        FakeHttpResponse(200, {"Content-Type": "text/html"}, b"<html>no</html>")
    )

    call = client.departments()

    assert fetch.calls == 1
    assert call.outcome.kind == "non-json"


def test_a_retry_after_header_is_honoured():
    client, _fetch, waits = scripted_client(
        responds(429, b'"slow down"', **{"Retry-After": "10"}), responds(200, b"[]")
    )

    client.departments()

    assert waits == [10.0]  # the server's number, not ours


def test_a_retry_after_header_is_capped():
    client, _fetch, waits = scripted_client(
        responds(429, b'"slow"', **{"Retry-After": "3600"}),
        responds(200, b"[]"),
        retry=RetryConfig(max_retry_after_seconds=60.0),
    )

    client.departments()

    assert waits == [60.0]  # a scheduled run will not sleep for an hour


def test_an_unusable_retry_after_falls_back_to_the_backoff():
    client, _fetch, waits = scripted_client(
        responds(429, b'"slow"', **{"Retry-After": "soon"}), responds(200, b"[]")
    )

    assert client.departments().ok
    assert waits == [2.0]


def test_a_retry_after_date_is_understood():
    from hsc_queue_monitor.api.retry import read_retry_after

    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    assert read_retry_after("Sat, 15 Aug 2026 12:00:30 GMT", now=now) == 30.0
    assert read_retry_after("Sat, 15 Aug 2026 11:59:30 GMT", now=now) == 0.0
    assert read_retry_after("not a date") is None
    assert read_retry_after("") is None


@pytest.mark.parametrize("endpoint", ["departments", "days", "slots"])
def test_every_endpoint_shares_the_same_retry(endpoint):
    client, fetch, waits = scripted_client(responds(502, b'"gateway"'), responds(200, b"[]"))

    call = {
        "departments": lambda: client.departments(),
        "days": lambda: client.days(2),
        "slots": lambda: client.slots(2, AUG_26),
    }[endpoint]()

    assert fetch.calls == 2
    assert waits == [2.0]
    assert call.ok


def test_every_attempt_uses_the_configured_timeout():
    """There is no second, larger timeout hiding in the retry any more."""
    client, fetch, _waits = scripted_client(responds(502, b'"gateway"'), responds(200, b"[]"))

    client.departments()
    client.days(2)

    assert fetch.timeouts == [(5, 60), (5, 60), (5, 60)]
    assert client.timeout == (5, 60)


def test_a_retry_is_logged_with_its_reason(caplog):
    caplog.set_level(logging.INFO)
    client, _fetch, _waits = scripted_client(
        responds(502, b'"gateway"'), responds(429, b'"slow"'), responds(200, b"[]")
    )

    client.departments()

    text = caplog.text
    assert "-> 502 (" in text
    assert "transient failure; retry 2/3 in 2.0s" in text
    assert "rate limited; retry 3/3 in 4.0s" in text
    assert "-> 200 (" in text and "[attempt 3]" in text
    for forbidden in ("Cookie", "Authorization", ACCESS_TOKEN_VALUE):
        assert forbidden not in text


def test_the_cookie_jar_is_persisted_even_on_a_failed_attempt():
    """A 5xx can still rotate the queue cookie, and the retry must carry it."""
    seen: list[str] = []
    client, _fetch, _waits = scripted_client(responds(502, b'"gateway"'), responds(200, b"[]"))
    client.on_response = lambda session: seen.append("saw")

    client.departments()

    assert seen == ["saw", "saw"]  # after both attempts, not just the good one


def test_the_shipped_configuration_defines_the_retry():
    from hsc_queue_monitor.config import AppSettings

    shipped = AppSettings.from_file(PROJECT_ROOT / "config" / "app.yaml")
    retry = shipped.api.retry

    assert retry.max_attempts == 3
    assert retry.initial_backoff_seconds == 2.0
    assert retry.max_backoff_seconds == 15.0
    assert retry.multiplier == 2.0
    assert [retry.backoff_for(1), retry.backoff_for(2)] == [2.0, 4.0]


@pytest.mark.parametrize(
    "raw",
    [
        {"max_attempts": 0},
        {"max_attempts": 6},
        {"max_attempts": 2.5},
        {"max_attempts": True},
        {"initial_backoff_seconds": 0},
        {"initial_backoff_seconds": -1},
        {"initial_backoff_seconds": float("nan")},
        {"initial_backoff_seconds": float("inf")},
        {"max_backoff_seconds": 1000},
        {"multiplier": 0.5},
        {"multiplier": 100},
        {"initial_backoff_seconds": 10.0, "max_backoff_seconds": 5.0},
        {"unknown": 1},
    ],
)
def test_a_malformed_retry_configuration_is_rejected(raw):
    from hsc_queue_monitor.config import ApiConfig
    from hsc_queue_monitor.models import ConfigError

    with pytest.raises(ConfigError):
        ApiConfig.from_dict({"retry": raw})


def test_there_is_exactly_one_retry_owner():
    """Two layers would multiply: three attempts each is nine requests."""
    import ast

    retrying: set[str] = set()
    for path in API_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            names = {
                inner.id if isinstance(inner, ast.Name) else inner.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Name | ast.Attribute)
            }
            if {"is_retryable", "wait_for"} & names:
                retrying.add(f"{path.name}:{node.name}")

    assert retrying == {"client.py:_get"}

    # And no module rolls its own loop around a request.
    for path in API_SOURCES:
        source = path.read_text(encoding="utf-8")
        if path.name in {"client.py", "retry.py"}:
            continue
        assert "max_attempts" not in source, path.name


def test_the_report_shows_read_and_refused_dates_together():
    server = RefusingServer(refuse_from="2026-08-27T00:00:00", days=THREE_DAYS)
    report = render_api_availability(scan_centre(client_with(server), "3242"))

    assert "  2026-08-21\n    09:20-09:46\n    10:40-11:06" in report
    assert "  2026-08-27\n    ERROR: HTTP 429 Too Many Requests" in report
    assert "Status: partial" in report


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #


def test_there_is_no_delay_before_the_first_request():
    clock = FakeClock()
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    # Three dates, two gaps.
    assert len(clock.sleeps) == 2


def test_the_configured_interval_is_what_is_waited():
    clock = FakeClock()
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    assert clock.sleeps == [2.0, 2.0]


def test_the_time_the_request_took_is_deducted_from_the_wait():
    clock = FakeClock()
    # Each request "takes" 1.3s on the fake clock, as in the worked example.
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock, duration=1.3)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    assert [round(seconds, 2) for seconds in clock.sleeps] == [0.7, 0.7]


def test_a_request_slower_than_the_interval_waits_not_at_all():
    clock = FakeClock()
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock, duration=5.0)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    assert clock.sleeps == []


def test_an_interval_of_zero_disables_pacing():
    clock = FakeClock()
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock)

    scan_centre(client_with(server), "3242", slot_interval=0, sleep=clock.sleep,
                clock=clock.monotonic)

    assert clock.sleeps == []


def test_pacing_is_separate_from_the_retry_policy():
    """The pacer waits between dates; the retry waits between attempts."""
    clock = FakeClock()
    server = RefusingServer(refuse_from="2026-08-26T00:00:00", days=THREE_DAYS, clock=clock)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    # One pacing wait, between the first and second date. The 429 on the second
    # is retried by the client (its own waits are not the pacer's), and the
    # centre scan then stops rather than moving to a third date.
    assert clock.sleeps == [2.0]
    assert [q["date"][0] for q in server.queries_for("slots")][:1] == ["2026-08-21T00:00:00"]


def test_the_pacer_logs_what_it_is_waiting_for(caplog):
    caplog.set_level(logging.INFO)
    clock = FakeClock()
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock, duration=1.3)

    scan_centre(client_with(server), "3242", slot_interval=2.0, sleep=clock.sleep,
                clock=clock.monotonic)

    assert "Waiting 0.70s before next slots request" in caplog.text


def test_the_rate_limit_is_logged_semantically(caplog):
    caplog.set_level(logging.INFO)
    server = RefusingServer(refuse_from="2026-08-26T00:00:00", days=THREE_DAYS)

    scan_centre(client_with(server), "3242")

    assert (
        "HSC rate limit reached while reading slots for 2026-08-26; preserving "
        "partial results and stopping centre scan." in caplog.text
    )


def test_the_shipped_configuration_paces_slot_requests():
    from hsc_queue_monitor.config import AppSettings

    shipped = AppSettings.from_file(PROJECT_ROOT / "config" / "app.yaml")
    assert shipped.api.slot_request_interval_seconds >= 2.0


@pytest.mark.parametrize("value", [-1, -0.5, 61, "soon", None, True, float("nan")])
def test_a_malformed_interval_is_rejected(value):
    from hsc_queue_monitor.config import ApiConfig
    from hsc_queue_monitor.models import ConfigError

    with pytest.raises(ConfigError):
        ApiConfig.from_dict({"slot_request_interval_seconds": value})


def test_a_sensible_interval_is_accepted():
    from hsc_queue_monitor.config import ApiConfig

    assert ApiConfig.from_dict({}).slot_request_interval_seconds == 2.0
    assert ApiConfig.from_dict(
        {"slot_request_interval_seconds": 0}
    ).slot_request_interval_seconds == 0.0
    assert ApiConfig.from_dict(
        {"slot_request_interval_seconds": "1.5"}
    ).slot_request_interval_seconds == 1.5


def test_a_redirect_is_reported_rather_than_followed():
    server = ApiServer(statuses={"departments": 302}, content_type="text/html")
    with pytest.raises(ApiRequestFailed) as excinfo:
        scan_centre(client_with(server), "3242")
    assert excinfo.value.call.outcome.kind == "redirect"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_the_report_has_the_shape_the_experiment_asked_for():
    server = ApiServer()
    report = render_api_availability(scan_centre(client_with(server), "3242"))

    assert "API AVAILABILITY" in report
    assert "Service ID: 47" in report
    assert "  requested:   3242" in report
    assert f"  internal id: {server.internal_id}" in report
    assert f"  name:        {NAME_3242}" in report
    assert "Dates:\n  2026-08-21\n  2026-08-26" in report
    assert "  2026-08-21\n    09:20-09:46\n    10:40-11:06" in report
    assert "  2026-08-26\n    08:26-08:52" in report


def test_the_report_distinguishes_no_dates_from_no_times():
    empty = ApiServer(days=[])
    no_dates = scan_centre(client_with(empty), "3242")
    assert no_dates.status == "no-dates"
    assert "Status: no-dates" in render_api_availability(no_dates)

    full = ApiServer(slots={"2026-08-21T00:00:00": [], "2026-08-26T00:00:00": []})
    no_times = scan_centre(client_with(full), "3242")
    assert no_times.status == "no-times"
    assert "Status: no-times" in render_api_availability(no_times)


def test_the_report_tracks_the_session_cookie_per_request():
    server = ApiServer()
    report = render_api_availability(scan_centre(client_with(server), "3242"))

    assert "  departments:\n    equeue-session changed: yes" in report
    assert "  days:\n    equeue-session changed: no" in report
    assert "  slots 2026-08-26:\n    equeue-session changed: no" in report
    # Fingerprints, never values.
    assert fingerprint(EQUEUE_VALUE) in report
    assert fingerprint(EQUEUE_AFTER_DEPARTMENTS) in report
    for secret in (*SECRET_VALUES, EQUEUE_AFTER_DEPARTMENTS):
        assert secret not in report


def test_a_date_with_no_free_time_is_reported_as_such():
    server = ApiServer(slots={"2026-08-21T00:00:00": [], "2026-08-26T00:00:00": []})
    scan = scan_centre(client_with(server), "3242")
    assert not scan.bookable
    assert "    (none)" in render_api_availability(scan)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


async def test_authentication_runs_first_and_nothing_is_clicked(tmp_path, capsys):
    events: list[str] = []
    page = ProbePage()
    config, ctx, auth = build_context(tmp_path, page, events)

    server = ApiServer(events=events)
    assert await run_api_availability(config, ctx, center="3242", fetch=server) == EXIT_OK

    assert auth.calls == 1
    assert events[0] == "authenticate"
    assert events.count("authenticate") == 1
    assert page.interactions == []  # no queue card, no menu, no wizard step

    out = capsys.readouterr().out
    assert "API AVAILABILITY" in out
    assert f"internal id: {server.internal_id}" in out
    assert "08:26-08:52" in out


async def test_the_command_bridges_the_browser_cookies_and_identity(tmp_path):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer()

    await run_api_availability(config, ctx, center="3242", fetch=server)

    assert server.cookies_seen[0] == {
        "__Secure-auth.access-token": ACCESS_TOKEN_VALUE,
        WIZARD_COOKIE: EQUEUE_VALUE,
        "__Host-next-auth.csrf-token": CSRF_VALUE,
    }
    assert IDGOV_VALUE not in str(server.cookies_seen)
    headers = server.headers_seen[0]
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Referer"] == "https://eqn.hsc.gov.ua/"
    assert headers["Accept-Language"] == "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"
    assert not [h for h in headers if h.lower().startswith("sec-fetch")]


async def test_no_cookie_value_reaches_the_logs_or_the_terminal(tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", fetch=ApiServer())

    printed = capsys.readouterr()
    everything = "\n".join(
        [*(r.getMessage() for r in caplog.records), printed.out, printed.err]
    )
    for secret in (*SECRET_VALUES, EQUEUE_AFTER_DEPARTMENTS):
        assert secret not in everything
    assert "Exported 3 HSC cookies" in everything


async def test_an_unreadable_days_schema_stops_cleanly(tmp_path, capsys):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer(days={"state": "WIZARD", "step": 2})

    assert await run_api_availability(config, ctx, center="3242", fetch=server) == EXIT_RUNTIME

    out = capsys.readouterr().out
    assert "Days response schema:" in out
    assert "payload: object(2 keys)" in out
    assert "No field name is guessed" in out
    assert server.endpoints == ["departments", "days"]  # it stopped there


async def test_a_refusal_is_reported_with_its_status(tmp_path, capsys):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer(statuses={"departments": 403}, content_type="text/html")

    assert await run_api_availability(config, ctx, center="3242", fetch=server) == EXIT_RUNTIME

    out = capsys.readouterr().out.lower()
    assert "403" in out
    assert "no bot/waf workaround" in out
    for forbidden in ("captcha", "akamai", "bypass", "spoof", "rotate"):
        assert forbidden not in out
    assert len(server.requests) == 1  # one request, no retry


async def test_an_empty_departments_response_stops_with_the_measured_reading(
    tmp_path, capsys
):
    """Measured live: 204 with an empty body, and no wizard cookie to send.

    It is reported as its own outcome — not as a broken body and not as a
    refusal — because it says something specific about the session state.
    """
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)

    class NoContent(ApiServer):
        def __call__(self, session, url, timeout=(5, 60)):
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    server = NoContent()
    assert await run_api_availability(config, ctx, center="3242", fetch=server) == EXIT_RUNTIME

    out = capsys.readouterr().out
    assert "Status:     204" in out
    assert "Body bytes: 0" in out
    assert WIZARD_COOKIE in out
    assert server.endpoints == ["departments"]  # nothing further was tried


async def test_the_command_reports_partial_availability_as_a_success(tmp_path, capsys):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    server = RefusingServer(refuse_from="2026-08-27T00:00:00", days=THREE_DAYS)

    result = await run_api_availability(config, ctx, center="3242", fetch=server)

    out = capsys.readouterr().out
    # The run did its job: it read what it could and said what it could not.
    assert result == EXIT_OK
    assert "09:20-09:46" in out
    assert "ERROR: HTTP 429 Too Many Requests" in out
    assert "Status: partial" in out


async def test_the_command_paces_with_the_configured_interval(tmp_path):
    clock = FakeClock()
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    # The shipped default, rather than the 0 the test config uses.
    config = dataclasses.replace(
        config,
        app=dataclasses.replace(
            config.app, api=ApiConfig(slot_request_interval_seconds=2.0)
        ),
    )
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock)

    await run_api_availability(
        config, ctx, center="3242", fetch=server, sleep=clock.sleep, clock=clock.monotonic
    )

    assert clock.sleeps == [2.0, 2.0]


async def test_a_command_line_interval_overrides_the_configuration(tmp_path):
    clock = FakeClock()
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer(days=THREE_DAYS, slots={}, clock=clock)

    await run_api_availability(
        config,
        ctx,
        center="3242",
        slot_interval=5.0,
        fetch=server,
        sleep=clock.sleep,
        clock=clock.monotonic,
    )

    assert clock.sleeps == [5.0, 5.0]


@pytest.mark.parametrize("value", [-1.0, 61.0])
async def test_a_malformed_command_line_interval_is_refused_before_anything_runs(
    tmp_path, value
):
    page = ProbePage()
    config, ctx, auth = build_context(tmp_path, page)
    server = ApiServer()

    result = await run_api_availability(
        config, ctx, center="3242", slot_interval=value, fetch=server
    )

    assert result == EXIT_CONFIG
    assert auth.calls == 0 and server.requests == []


async def test_an_unknown_centre_is_refused_before_authentication(tmp_path):
    page = ProbePage()
    config, ctx, auth = build_context(tmp_path, page)
    server = ApiServer()

    result = await run_api_availability(config, ctx, center="9999", fetch=server)

    assert result == EXIT_CONFIG
    assert auth.calls == 0 and server.requests == []


# --------------------------------------------------------------------------- #
# The opt-in queue bootstrap
# --------------------------------------------------------------------------- #


async def test_without_the_flag_nothing_extra_is_navigated(tmp_path, capsys):
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE)
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", fetch=ApiServer())

    assert page.navigations == []  # the default path still stops at /cabinet
    assert page.locator_calls == []
    assert "Queue bootstrap" not in capsys.readouterr().out


async def test_the_flag_navigates_to_the_queue_page_exactly_once(tmp_path):
    events: list[str] = []
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE, events=events)
    config, ctx, _auth = build_context(tmp_path, page, events)
    server = ApiServer(events=events)

    await run_api_availability(
        config, ctx, center="3242", open_queue=True, fetch=server
    )

    assert page.navigations == [QUEUE_URL]
    # Authenticate, navigate, and only then talk to the API.
    assert events[:3] == ["authenticate", "goto", "fetch"]


async def test_the_bootstrap_touches_no_wizard_control(tmp_path):
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE)
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=ApiServer())

    # One goto and the cookie/UA reads. No button, no card, no date, no time.
    assert page.locator_calls == []
    assert [api for api, _a, _k in page.calls if api == "goto"] == ["goto"]


async def test_the_session_is_built_after_the_navigation(tmp_path):
    """Proved by what the first request carried, not by ordering alone.

    The queue cookie does not exist until the navigation mints it, so a session
    built beforehand could not possibly send it.
    """
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE, cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer()

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=server)

    assert server.cookies_seen[0][WIZARD_COOKIE] == MINTED_EQUEUE_VALUE


async def test_a_minted_cookie_is_reported_by_fingerprint(tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE, cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)

    assert (
        await run_api_availability(
            config, ctx, center="3242", open_queue=True, fetch=ApiServer()
        )
        == EXIT_OK
    )

    printed = capsys.readouterr()
    everything = "\n".join(
        [*(r.getMessage() for r in caplog.records), printed.out, printed.err]
    )
    assert "Queue bootstrap:" in printed.out
    assert f"  URL:       {QUEUE_URL}" in printed.out
    assert f"{WIZARD_COOKIE} before: absent" in printed.out
    assert f"{WIZARD_COOKIE} after:  present ({fingerprint(MINTED_EQUEUE_VALUE)})" in printed.out
    assert "the navigation created the queue session cookie" in printed.out
    for secret in (*SECRET_VALUES, MINTED_EQUEUE_VALUE):
        assert secret not in everything


async def test_a_cookie_that_was_already_there_is_reported_as_unchanged(tmp_path, capsys):
    page = BootstrapPage()  # the default jar already holds the queue session
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=ApiServer())

    out = capsys.readouterr().out
    assert f"{WIZARD_COOKIE} before: present ({fingerprint(EQUEUE_VALUE)})" in out
    assert "left it alone" in out


async def test_a_replaced_cookie_is_reported_as_changed(tmp_path, capsys):
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE)  # jar already has one; goto adds another
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=ApiServer())

    out = capsys.readouterr().out
    assert "before: present" in out and "after:  present" in out


async def test_a_cookie_that_never_appears_is_still_a_finding(tmp_path, capsys):
    page = BootstrapPage(cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=ApiServer())

    out = capsys.readouterr().out
    assert f"{WIZARD_COOKIE} before: absent" in out
    assert f"{WIZARD_COOKIE} after:  absent" in out
    assert "did not create the queue session cookie" in out


async def test_a_redirect_away_from_the_queue_page_is_reported(tmp_path, capsys):
    page = BootstrapPage(redirect_to=CABINET, cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)

    await run_api_availability(config, ctx, center="3242", open_queue=True, fetch=ApiServer())

    out = capsys.readouterr().out
    assert f"  final URL: {CABINET}" in out
    assert "HSC redirected away from the queue page" in out


async def test_a_bootstrapped_run_that_still_gets_204_stops_cleanly(tmp_path, capsys):
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE, cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)

    class NoContent(ApiServer):
        def __call__(self, session, url, timeout=(5, 60)):
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    server = NoContent()
    result = await run_api_availability(
        config, ctx, center="3242", open_queue=True, fetch=server
    )

    out = capsys.readouterr().out
    assert result == EXIT_RUNTIME
    assert "Queue bootstrap:" in out
    assert "Status:     204" in out
    assert "Queue session bootstrap did not make departments available." in out
    # Nothing else was tried: no other endpoint, no retry, no click.
    assert server.endpoints == ["departments"]
    assert page.locator_calls == []


async def test_a_204_without_the_flag_suggests_the_experiment(tmp_path, capsys):
    page = BootstrapPage(cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)

    class NoContent(ApiServer):
        def __call__(self, session, url, timeout=(5, 60)):
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    await run_api_availability(config, ctx, center="3242", fetch=NoContent())

    out = capsys.readouterr().out
    assert "Try `--open-queue`" in out
    assert "Queue session bootstrap did not make" not in out


async def test_a_bootstrapped_run_that_gets_json_reads_the_whole_sequence(tmp_path, capsys):
    page = BootstrapPage(mints=MINTED_EQUEUE_VALUE, cookies=cookies_without_queue_session())
    config, ctx, _auth = build_context(tmp_path, page)
    server = ApiServer()

    result = await run_api_availability(
        config, ctx, center="3242", open_queue=True, fetch=server
    )

    assert result == EXIT_OK
    assert server.endpoints == ["departments", "days", "slots", "slots"]
    out = capsys.readouterr().out
    # The bootstrap block sits inside the report, above the centre.
    assert out.index("Queue bootstrap:") < out.index("Service ID: 47")
    assert "08:26-08:52" in out
    assert "Status: bookable" in out


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #


API_SOURCES = sorted((SRC / "api").glob("*.py"))


@pytest.mark.parametrize("path", API_SOURCES, ids=lambda p: p.name)
def test_no_module_can_spell_a_mutating_request(path):
    """HTTP verbs, specifically. A session store deleting its own row is not one."""
    source = path.read_text(encoding="utf-8")
    assert not re.search(
        r"\b(session|requests|client|http|fetch)\.(post|put|patch|delete)\s*\(", source
    )
    assert "allow_redirects=True" not in source


def test_the_client_exposes_no_mutating_or_booking_method():
    forbidden = ("post", "put", "patch", "delete", "book", "reserve", "submit", "select")
    client = client_with(ApiServer())
    exposed = [name for name in dir(client) if not name.startswith("__")]
    assert not [name for name in exposed if any(word in name.lower() for word in forbidden)]
    # The whole surface: three measured reads, plus housekeeping.
    assert sorted(name for name in exposed if not name.startswith("_")) == [
        "base_url",
        "close",
        "days",
        "departments",
        "on_response",
        "require",
        "retry",
        "service_id",
        "session",
        "slots",
        "timeout",
    ]


def test_nothing_in_the_api_package_clicks_or_books():
    for path in API_SOURCES:
        source = path.read_text(encoding="utf-8")
        for forbidden in (".click(", "select_option", "set_input_files", ".press("):
            assert forbidden not in source


def test_the_ui_scanner_and_the_monitor_are_untouched():
    """The API path is additive: the browser scanner is still the only scanner."""
    from hsc_queue_monitor.flow.availability import AvailabilityScanner
    from hsc_queue_monitor.monitor.monitor import Monitor

    # Still the UI scanner: it walks the wizard with a browser, and knows
    # nothing about HTTP clients or the API package.
    assert hasattr(AvailabilityScanner, "scan") and Monitor is not None
    for path in (SRC / "flow" / "availability.py", SRC / "monitor" / "monitor.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("HscApiClient", "import requests", "from ..api", "from .api"):
            assert forbidden not in source


@pytest.mark.parametrize("path", API_SOURCES, ids=lambda p: p.name)
def test_no_internal_department_id_is_written_into_the_implementation(path):
    """The mapping lives in the response, never in the code.

    Live runs have resolved 3242 to both 2 and 100. A literal here — or a cached
    one on disk — would query whichever centre yesterday's response happened to
    mean.
    """
    source = path.read_text(encoding="utf-8")
    assert not re.search(r"department_?[iI]d\s*[=:]\s*\d", source)
    assert not re.search(r"\b(2|100)\b\s*#\s*department", source)
    # Nor is a resolved id ever written anywhere. Three modules persist at
    # all, and none persists a department id: session_store.py writes the
    # cookie jar, config_init.py writes the catalogue of *visible* centre
    # numbers, and session_dump.py writes the opt-in diagnostic session dump
    # (also cookies — never a department id).
    if path.name not in {"session_store.py", "config_init.py", "session_dump.py"}:
        for persisted in ("json.dump", "write_text", "open(", "pickle"):
            assert persisted not in source


def test_the_department_model_keeps_the_two_identities_apart():
    department = Department(department_id=2, display_name=NAME_3242, allow_online_count=1)
    assert department.department_id != int(CENTRE_3242.id)
    assert CENTRE_3242.id in department.display_name
