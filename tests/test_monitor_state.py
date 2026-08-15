"""The persisted monitor state: the gate, the transitions, and what they mean.

Two properties carry the weight here.

**A service outage is never an authentication problem.** If exhausted retries
could produce AUTH_REQUIRED, one bad afternoon at HSC would pause monitoring
until a human noticed — and, once a notifier exists, would tell them their login
had expired when it had not. Several tests exist only to keep those two paths
apart.

**A sticky state stays sticky, and says nothing twice.** AUTH_REQUIRED blocks
every later run before it sends anything, and produces no transition the second
time, which is what will keep a future Telegram layer from repeating itself
every five minutes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_api_availability import ApiServer, TimingOutServer
from test_api_monitor import (
    CENTRE_A,
    EMPTY_DAYS,
    SLOT_0826,
    FakeStateStore,
    FakeStore,
    ForbiddenServer,
    days_for,
    stored_session,
)
from test_api_probe import FakeHttpResponse

from hsc_queue_monitor.api.headless_monitor import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_PERSISTENCE,
    EXIT_RATE_LIMITED,
    EXIT_SERVICE_UNAVAILABLE,
    classify,
    run_headless_scan,
)
from hsc_queue_monitor.api.monitor import CentreReading
from hsc_queue_monitor.api.monitor_state import (
    STATE_DOCUMENT_ID,
    STATE_SCHEMA_VERSION,
    MongoMonitorStateStore,
    MonitorState,
    MonitorStateTransition,
    MonitorStatus,
    NullMonitorStateStore,
    record,
    refreshed,
    transition_to,
)
from hsc_queue_monitor.api.session_store import DOCUMENT_ID, SessionStoreError

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


class Printed:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def __call__(self, text: str) -> None:
        self.blocks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)


def scan(
    *,
    state: MonitorState | None = None,
    session: Any = "default",
    server: ApiServer | None = None,
    **kwargs: Any,
) -> tuple[Any, Printed, FakeStore, ApiServer]:
    store = FakeStore(stored=stored_session() if session == "default" else session)
    store.states.state = state
    api = server if server is not None else ApiServer(days=EMPTY_DAYS)
    printed = Printed()
    result = run_headless_scan(
        store,
        [CENTRE_A],
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=api,
        emit=printed,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        **kwargs,
    )
    return result, printed, store, api


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_auth_required_stops_the_run_before_anything_is_sent():
    result, printed, store, server = scan(
        state=MonitorState(status=MonitorStatus.AUTH_REQUIRED, reason="HTTP 401")
    )

    assert result.code == EXIT_AUTH_REQUIRED
    assert server.requests == []  # zero HSC calls
    assert "AUTH REQUIRED" in printed.text
    assert "Monitoring is paused" in printed.text
    assert "refresh-session" in printed.text


def test_auth_required_does_not_even_read_the_session():
    """The gate is before the decryption, because there is nothing to decrypt for."""

    class Exploding(FakeStore):
        def load(self) -> Any:
            raise AssertionError("the session was read while AUTH_REQUIRED")

    store = Exploding(stored=stored_session())
    store.states.state = MonitorState(status=MonitorStatus.AUTH_REQUIRED)
    server = ApiServer(days=EMPTY_DAYS)

    result = run_headless_scan(
        store,
        [CENTRE_A],
        state_store=store.states,
        fetch=server,
        emit=lambda _text: None,
    )

    assert result.code == EXIT_AUTH_REQUIRED
    assert server.requests == []


def test_a_blocked_run_reports_no_transition():
    """So a notifier is not handed the same news twice."""
    result, _printed, store, _server = scan(
        state=MonitorState(status=MonitorStatus.AUTH_REQUIRED)
    )

    assert result.transition is None
    assert store.states.saved == []  # nothing rewritten either


def test_a_rate_limit_window_is_waited_out():
    result, printed, _store, server = scan(
        state=MonitorState(
            status=MonitorStatus.RATE_LIMITED,
            reason="HTTP 429 after 3 attempts",
            retry_after_at=NOW + timedelta(seconds=30),
        )
    )

    assert result.code == EXIT_RATE_LIMITED
    assert server.requests == []
    assert "RATE_LIMITED" in printed.text
    assert "Retry after: 2026-08-15 12:00:30 UTC" in printed.text


def test_monitoring_resumes_once_the_window_has_passed():
    result, _printed, store, server = scan(
        state=MonitorState(
            status=MonitorStatus.RATE_LIMITED,
            retry_after_at=NOW - timedelta(seconds=1),
        )
    )

    assert result.code == EXIT_OK
    assert server.endpoints == ["departments", "days"]
    assert store.state is not None and store.state.status is MonitorStatus.READY


def test_a_rate_limit_with_no_window_does_not_block_forever():
    result, _printed, _store, server = scan(
        state=MonitorState(status=MonitorStatus.RATE_LIMITED)
    )

    assert result.code == EXIT_OK
    assert server.requests  # a window it cannot see the end of is not a window


def test_service_unavailable_never_blocks():
    result, _printed, _store, server = scan(
        state=MonitorState(status=MonitorStatus.SERVICE_UNAVAILABLE, reason="HTTP 502")
    )

    assert result.code == EXIT_OK
    assert server.endpoints == ["departments", "days"]


def test_ready_monitors_normally():
    result, _printed, store, server = scan(state=MonitorState(status=MonitorStatus.READY))

    assert result.code == EXIT_OK
    assert server.endpoints == ["departments", "days"]
    assert store.state is not None and store.state.status is MonitorStatus.READY


def test_no_state_at_all_monitors_normally():
    result, _printed, _store, server = scan(state=None)

    assert result.code == EXIT_OK
    assert server.endpoints == ["departments", "days"]


def test_an_unreadable_state_document_stops_before_the_session():
    store = FakeStore(stored=stored_session())
    store.states.fails = True
    server = ApiServer(days=EMPTY_DAYS)
    printed = Printed()

    result = run_headless_scan(
        store, [CENTRE_A], state_store=store.states, fetch=server, emit=printed
    )

    assert result.code == EXIT_PERSISTENCE
    assert server.requests == []
    assert "PERSISTENCE ERROR" in printed.text


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def reading(**kwargs: Any) -> CentreReading:
    return CentreReading(centre_id=CENTRE_A, **kwargs)


def test_a_refusal_is_the_only_thing_that_means_auth_required():
    for kind in ("unauthorized", "forbidden"):
        status, reason = classify(
            [reading(complete=False, detail=f"HTTP {kind}", kinds=frozenset({kind}))]
        )
        assert status is MonitorStatus.AUTH_REQUIRED
        assert reason


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("rate-limited", MonitorStatus.RATE_LIMITED),
        ("http-error", MonitorStatus.SERVICE_UNAVAILABLE),
        ("timeout", MonitorStatus.SERVICE_UNAVAILABLE),
        ("network-error", MonitorStatus.SERVICE_UNAVAILABLE),
        ("no-content", MonitorStatus.SERVICE_UNAVAILABLE),
        ("bad-json", MonitorStatus.SERVICE_UNAVAILABLE),
    ],
)
def test_nothing_but_a_refusal_becomes_auth_required(kind, expected):
    """The rule this whole design exists to keep: an outage is not a login."""
    status, _reason = classify(
        [reading(complete=False, detail=f"HTTP {kind}", kinds=frozenset({kind}))]
    )
    assert status is expected
    assert status is not MonitorStatus.AUTH_REQUIRED


def test_a_refusal_outranks_a_rate_limit():
    status, _reason = classify(
        [
            reading(complete=False, kinds=frozenset({"rate-limited"})),
            reading(complete=False, kinds=frozenset({"forbidden"})),
        ]
    )
    assert status is MonitorStatus.AUTH_REQUIRED


def test_a_rate_limit_outranks_a_server_failure():
    status, _reason = classify(
        [
            reading(complete=False, kinds=frozenset({"http-error"})),
            reading(complete=False, kinds=frozenset({"rate-limited"})),
        ]
    )
    assert status is MonitorStatus.RATE_LIMITED


def test_ready_needs_every_centre_read_completely():
    """A partial answer is an unknown, not a smaller success."""
    assert classify([reading(complete=True), reading(complete=True)])[0] is MonitorStatus.READY
    assert (
        classify([reading(complete=True), reading(complete=False, detail="x")])[0]
        is MonitorStatus.SERVICE_UNAVAILABLE
    )


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


def test_a_transition_knows_whether_it_is_news():
    was = MonitorState(status=MonitorStatus.AUTH_REQUIRED)
    _state, first = transition_to(was, MonitorStatus.AUTH_REQUIRED)
    _state, second = transition_to(was, MonitorStatus.READY)

    assert not first.changed  # the same state twice is not an event
    assert second.changed and second.recovered
    assert "AUTH_REQUIRED -> READY" in second.describe()


def test_success_clears_the_reason_and_the_window():
    was = MonitorState(
        status=MonitorStatus.RATE_LIMITED,
        reason="HTTP 429",
        retry_after_at=NOW + timedelta(minutes=1),
        last_success_at=NOW - timedelta(hours=1),
    )

    state, event = transition_to(was, MonitorStatus.READY, now=NOW)

    assert state.status is MonitorStatus.READY
    assert state.reason == ""
    assert state.retry_after_at is None
    assert state.last_success_at == NOW
    assert event.changed


def test_a_failure_keeps_the_last_success_it_knew_about():
    was = MonitorState(status=MonitorStatus.READY, last_success_at=NOW - timedelta(minutes=5))

    state, _event = transition_to(
        was, MonitorStatus.SERVICE_UNAVAILABLE, reason="HTTP 502", now=NOW
    )

    assert state.last_success_at == NOW - timedelta(minutes=5)
    assert state.reason == "HTTP 502"


def test_a_state_that_was_not_written_did_not_happen(caplog):
    """The persisted document is the source of truth, so no write, no event."""
    caplog.set_level(logging.WARNING)
    store = FakeStateStore(fails=True)

    event = record(store, None, MonitorStatus.READY)

    assert event is None  # never a transition a notifier could act on
    assert "Could not persist monitor state" in caplog.text


def test_a_refresh_leaves_ready_behind():
    was = MonitorState(status=MonitorStatus.AUTH_REQUIRED, reason="HTTP 401")

    state = refreshed(was, now=NOW)

    assert state.status is MonitorStatus.READY
    assert state.reason == ""
    assert state.retry_after_at is None


# --------------------------------------------------------------------------- #
# What ends up in the document
# --------------------------------------------------------------------------- #


class FakeCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.replacements = 0

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        found = self.documents.get(str(query["_id"]))
        return dict(found) if found else None

    def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], upsert: bool = False
    ) -> None:
        assert upsert is True, "state must be written with upsert semantics"
        self.replacements += 1
        self.documents[str(query["_id"])] = dict(document)


def test_the_state_lives_in_its_own_document():
    collection = FakeCollection()
    store = MongoMonitorStateStore(collection, now=lambda: NOW)

    store.save(MonitorState(status=MonitorStatus.AUTH_REQUIRED, reason="HTTP 401"))

    assert set(collection.documents) == {STATE_DOCUMENT_ID}
    assert STATE_DOCUMENT_ID != DOCUMENT_ID  # never the session's document
    document = collection.documents[STATE_DOCUMENT_ID]
    assert document["version"] == STATE_SCHEMA_VERSION
    assert document["status"] == "AUTH_REQUIRED"
    assert document["updated_at"] == NOW


def test_the_state_document_holds_nothing_secret():
    collection = FakeCollection()
    store = MongoMonitorStateStore(collection, now=lambda: NOW)

    store.save(MonitorState(status=MonitorStatus.SERVICE_UNAVAILABLE, reason="HTTP 502"))

    assert set(collection.documents[STATE_DOCUMENT_ID]) == {
        "_id",
        "version",
        "status",
        "reason",
        "updated_at",
        "last_success_at",
        "retry_after_at",
    }
    rendered = str(collection.documents[STATE_DOCUMENT_ID])
    for forbidden in ("cookie", "token", "mongodb", "key", "Set-Cookie"):
        assert forbidden.lower() not in rendered.lower()


def test_the_state_round_trips():
    collection = FakeCollection()
    store = MongoMonitorStateStore(collection, now=lambda: NOW)
    store.save(
        MonitorState(
            status=MonitorStatus.RATE_LIMITED,
            reason="HTTP 429 after 3 attempts",
            last_success_at=NOW - timedelta(minutes=10),
            retry_after_at=NOW + timedelta(seconds=60),
        )
    )

    loaded = store.load()

    assert loaded is not None
    assert loaded.status is MonitorStatus.RATE_LIMITED
    assert loaded.reason == "HTTP 429 after 3 attempts"
    assert loaded.retry_after_at == NOW + timedelta(seconds=60)
    assert loaded.waiting(now=NOW)


def test_a_state_written_by_another_version_is_ignored():
    collection = FakeCollection()
    store = MongoMonitorStateStore(collection, now=lambda: NOW)
    store.save(MonitorState(status=MonitorStatus.READY))
    collection.documents[STATE_DOCUMENT_ID]["version"] = STATE_SCHEMA_VERSION + 1

    assert store.load() is None


def test_an_unknown_status_is_ignored_rather_than_guessed():
    collection = FakeCollection()
    store = MongoMonitorStateStore(collection, now=lambda: NOW)
    store.save(MonitorState(status=MonitorStatus.READY))
    collection.documents[STATE_DOCUMENT_ID]["status"] = "PROBABLY_FINE"

    assert store.load() is None


def test_a_database_failure_is_reported_not_swallowed():
    class Broken:
        def find_one(self, _query: dict[str, Any]) -> None:
            raise RuntimeError("no primary")

        def replace_one(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("no primary")

    store = MongoMonitorStateStore(Broken())

    with pytest.raises(SessionStoreError):
        store.load()
    with pytest.raises(SessionStoreError):
        store.save(MonitorState())


def test_the_null_store_remembers_nothing():
    store = NullMonitorStateStore()
    store.save(MonitorState(status=MonitorStatus.AUTH_REQUIRED))
    store.close()

    assert store.load() is None


# --------------------------------------------------------------------------- #
# End to end through the scan
# --------------------------------------------------------------------------- #


def test_a_401_writes_auth_required_and_the_next_run_sends_nothing():
    server = ApiServer(statuses={"departments": 401}, content_type="text/html")
    result, _printed, store, _server = scan(server=server)

    assert result.code == EXIT_AUTH_REQUIRED
    assert result.transition is not None and result.transition.changed
    assert store.state is not None and store.state.status is MonitorStatus.AUTH_REQUIRED
    assert len(server.requests) == 1  # an answer, so never retried

    # The next scheduled run, with that state already written.
    again = ApiServer(days=EMPTY_DAYS)
    printed = Printed()
    second = run_headless_scan(
        store, [CENTRE_A], state_store=store.states, fetch=again, emit=printed
    )

    assert second.code == EXIT_AUTH_REQUIRED
    assert again.requests == []
    assert second.transition is None  # not news, so nothing to notify about


def test_a_403_writes_auth_required_too():
    result, _printed, store, _server = scan(server=ForbiddenServer(days=EMPTY_DAYS))

    assert result.code == EXIT_AUTH_REQUIRED
    assert store.state is not None and store.state.status is MonitorStatus.AUTH_REQUIRED


def test_a_missing_session_is_auth_required_and_sticks():
    result, printed, store, server = scan(session=None)

    assert result.code == EXIT_AUTH_REQUIRED
    assert server.requests == []
    assert "refresh-session" in printed.text
    assert store.state is not None and store.state.status is MonitorStatus.AUTH_REQUIRED


def test_an_exhausted_429_becomes_rate_limited_with_a_window():
    server = ApiServer(statuses={"departments": 429}, content_type="text/html")
    result, _printed, store, _server = scan(server=server)

    assert result.code == EXIT_RATE_LIMITED
    assert store.state is not None
    assert store.state.status is MonitorStatus.RATE_LIMITED
    assert store.state.retry_after_at == NOW + timedelta(seconds=60)
    assert store.state.status is not MonitorStatus.AUTH_REQUIRED


def test_an_exhausted_outage_becomes_service_unavailable_and_recovers():
    server = ApiServer(statuses={"departments": 502}, content_type="text/html")
    result, _printed, store, _server = scan(server=server)

    assert result.code == EXIT_SERVICE_UNAVAILABLE
    assert store.state is not None
    assert store.state.status is MonitorStatus.SERVICE_UNAVAILABLE
    assert "502" in store.state.reason

    # Not sticky: the next run simply tries again, and succeeds.
    healthy = ApiServer(days=EMPTY_DAYS)
    second = run_headless_scan(
        store,
        [CENTRE_A],
        state_store=store.states,
        fetch=healthy,
        emit=lambda _text: None,
        now=lambda: NOW,
    )

    assert second.code == EXIT_OK
    assert healthy.requests  # it did try
    assert store.state.status is MonitorStatus.READY
    assert second.transition is not None and second.transition.recovered


def test_an_exhausted_timeout_becomes_service_unavailable():
    server = TimingOutServer(timeout_from="", days=days_for("2026-08-26"))
    result, _printed, store, _server = scan(server=server)

    assert result.code == EXIT_SERVICE_UNAVAILABLE
    assert store.state is not None
    assert store.state.status is MonitorStatus.SERVICE_UNAVAILABLE


def test_a_successful_scan_records_when_it_happened():
    result, _printed, store, _server = scan(
        server=ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]})
    )

    assert result.code == EXIT_OK
    assert store.state is not None
    assert store.state.status is MonitorStatus.READY
    assert store.state.last_success_at == NOW
    assert store.state.reason == ""


def test_the_cookie_jar_is_still_persisted_across_a_retry():
    """Session persistence and state persistence are separate concerns."""

    class FlakyOnce(ApiServer):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.attempts = 0

        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                super().__call__(session, url, timeout)  # rotates the cookie
                return FakeHttpResponse(
                    502, {"Content-Type": "application/json"}, b'"gateway"'
                )
            return super().__call__(session, url, timeout)

    result, _printed, store, _server = scan(server=FlakyOnce(days=EMPTY_DAYS))

    assert result.code == EXIT_OK
    assert store.saves, "the rotated jar was not written back"
    assert store.state is not None and store.state.status is MonitorStatus.READY


def test_no_telegram_exists_anywhere_in_the_state_machine():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "hsc_queue_monitor" / "api"
    for name in ("monitor_state.py", "headless_monitor.py", "retry.py"):
        source = (src / name).read_text(encoding="utf-8")
        assert "telegram" not in source.lower()
        assert "Notifier" not in source
    # The seam a notifier will use exists, and is only a description.
    assert MonitorStateTransition(previous=None, current=MonitorStatus.READY).changed
