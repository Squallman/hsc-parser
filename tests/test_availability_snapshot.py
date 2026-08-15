"""Availability diffing: say what changed, and only what changed.

The dangerous case is not the happy one. It is a temporary API failure being
mistaken for "everything disappeared" — twenty slots reported gone because a
gateway hiccuped. Several tests here exist for nothing but that, and they are
the reason the snapshot is only ever replaced after a *complete* scan.

The second theme is silence. A first run, an unchanged run and a newly enabled
centre all produce no user-facing output at all, because a change detector that
speaks every five minutes is one nobody reads.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from test_api_availability import ApiServer, TimingOutServer
from test_api_monitor import (
    CENTRE_A,
    CENTRE_B,
    EMPTY_DAYS,
    SLOT_0826,
    SLOT_0918,
    FakeStore,
    ForbiddenServer,
    days_for,
    stored_session,
)

from hsc_queue_monitor.api.availability_snapshot import (
    SNAPSHOT_DOCUMENT_ID,
    SNAPSHOT_SCHEMA_VERSION,
    AvailabilityDiff,
    AvailabilitySnapshot,
    AvailableSlot,
    MongoAvailabilitySnapshotStore,
    NullAvailabilitySnapshotStore,
    SlotKey,
    SnapshotVersionUnsupported,
    diff_snapshots,
    render_diff,
    snapshot_of,
)
from hsc_queue_monitor.api.headless_monitor import (
    EXIT_OK,
    EXIT_PERSISTENCE,
    run_headless_scan,
)
from hsc_queue_monitor.api.monitor_state import STATE_DOCUMENT_ID
from hsc_queue_monitor.api.session_store import DOCUMENT_ID, SessionStoreError
from hsc_queue_monitor.models import TimeSlot

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
AUG_26 = date(2026, 8, 26)
AUG_27 = date(2026, 8, 27)

SLOT_0910 = {"startTime": "09:10:00", "stopTime": "09:36:00"}
SLOT_1010 = {"startTime": "10:10:00", "stopTime": "10:36:00"}


def slot(centre: str, day: date, start: str, end: str | None = None) -> AvailableSlot:
    return AvailableSlot(
        centre=centre,
        date=day,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end) if end else None,
    )


def snapshot(**centres: list[AvailableSlot]) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(centres={k: tuple(v) for k, v in centres.items()})


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
    server: ApiServer | None = None,
    store: FakeStore | None = None,
    centres: tuple[str, ...] = (CENTRE_A,),
    **kwargs: Any,
) -> tuple[Any, Printed, FakeStore, ApiServer]:
    session_store = store if store is not None else FakeStore(stored=stored_session())
    api = server if server is not None else ApiServer(days=EMPTY_DAYS)
    printed = Printed()
    result = run_headless_scan(
        session_store,
        centres,
        state_store=session_store.states,
        snapshots=session_store.snapshots,
        fetch=api,
        emit=printed,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        **kwargs,
    )
    return result, printed, session_store, api


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_a_slot_is_identified_by_centre_date_and_start():
    first = slot(CENTRE_A, AUG_26, "08:26", "08:52")
    same_window_moved = slot(CENTRE_A, AUG_26, "08:26", "09:00")

    assert first.key == same_window_moved.key  # the end time is metadata
    assert first.key == SlotKey(centre=CENTRE_A, date=AUG_26, start_time=time(8, 26))
    assert first.key != slot(CENTRE_B, AUG_26, "08:26").key
    assert first.key != slot(CENTRE_A, AUG_27, "08:26").key
    assert first.key != slot(CENTRE_A, AUG_26, "09:26").key


def test_an_end_time_change_alone_is_not_a_change():
    previous = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "08:52")]})
    current = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "09:00")]})

    assert not diff_snapshots(previous, current, centres=[CENTRE_A]).changed


def test_a_slot_displays_its_window():
    assert slot(CENTRE_A, AUG_26, "08:26", "08:52").display == "08:26-08:52"
    assert slot(CENTRE_A, AUG_26, "08:26").display == "08:26"


def test_the_internal_department_id_plays_no_part():
    """Snapshots are keyed by what a person books, not by what the API calls it."""
    import inspect

    from hsc_queue_monitor.api import availability_snapshot

    source = inspect.getsource(availability_snapshot)
    for forbidden in ("department_id", "departmentId"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# The diff
# --------------------------------------------------------------------------- #


def test_a_first_sight_of_a_centre_is_a_baseline_not_news():
    current = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "08:52")]})

    difference = diff_snapshots(None, current, centres=[CENTRE_A])

    assert not difference.changed
    assert difference.baselined == (CENTRE_A,)


def test_nothing_changes_when_nothing_changed():
    slots = [slot(CENTRE_A, AUG_26, "08:26", "08:52"), slot(CENTRE_A, AUG_26, "09:18")]
    previous = snapshot(**{CENTRE_A: slots})
    current = snapshot(**{CENTRE_A: list(reversed(slots))})  # order is not identity

    assert not diff_snapshots(previous, current, centres=[CENTRE_A]).changed


def test_empty_to_empty_is_not_a_change():
    previous = snapshot(**{CENTRE_A: []})
    current = snapshot(**{CENTRE_A: []})

    assert not diff_snapshots(previous, current, centres=[CENTRE_A]).changed


def test_one_added_slot():
    previous = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "08:52")]})
    current = snapshot(
        **{
            CENTRE_A: [
                slot(CENTRE_A, AUG_26, "08:26", "08:52"),
                slot(CENTRE_A, AUG_26, "09:18", "09:44"),
            ]
        }
    )

    difference = diff_snapshots(previous, current, centres=[CENTRE_A])

    assert difference.changed
    assert [s.display for s in difference.added] == ["09:18-09:44"]
    assert difference.removed == ()


def test_one_removed_slot():
    previous = snapshot(
        **{
            CENTRE_A: [
                slot(CENTRE_A, AUG_26, "08:26", "08:52"),
                slot(CENTRE_A, AUG_26, "09:18", "09:44"),
            ]
        }
    )
    current = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "08:52")]})

    difference = diff_snapshots(previous, current, centres=[CENTRE_A])

    assert [s.display for s in difference.removed] == ["09:18-09:44"]
    assert difference.added == ()


def test_additions_and_removals_together():
    previous = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")]})
    current = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_27, "10:10")]})

    difference = diff_snapshots(previous, current, centres=[CENTRE_A])

    assert [s.display for s in difference.added] == ["10:10"]
    assert [s.display for s in difference.removed] == ["08:26"]


def test_changes_across_dates_and_centres_are_sorted():
    previous = snapshot(**{CENTRE_A: [], CENTRE_B: []})
    current = snapshot(
        **{
            CENTRE_B: [slot(CENTRE_B, AUG_26, "07:00")],
            CENTRE_A: [
                slot(CENTRE_A, AUG_27, "09:00"),
                slot(CENTRE_A, AUG_26, "10:00"),
                slot(CENTRE_A, AUG_26, "08:00"),
            ],
        }
    )

    added = diff_snapshots(previous, current, centres=[CENTRE_A, CENTRE_B]).added

    # Centre, then date, then start — deterministic, so the report is stable.
    assert [(s.centre, s.date.isoformat(), s.display) for s in added] == [
        (CENTRE_A, "2026-08-26", "08:00"),
        (CENTRE_A, "2026-08-26", "10:00"),
        (CENTRE_A, "2026-08-27", "09:00"),
        (CENTRE_B, "2026-08-26", "07:00"),
    ]


# --------------------------------------------------------------------------- #
# Configuration changes are not availability changes
# --------------------------------------------------------------------------- #


def test_a_newly_monitored_centre_is_baselined_silently():
    previous = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")]})
    current = snapshot(
        **{
            CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")],
            CENTRE_B: [slot(CENTRE_B, AUG_26, "07:00"), slot(CENTRE_B, AUG_27, "08:00")],
        }
    )

    difference = diff_snapshots(previous, current, centres=[CENTRE_A, CENTRE_B])

    assert not difference.changed  # two slots appeared, and neither is news
    assert difference.baselined == (CENTRE_B,)


def test_a_centre_that_is_no_longer_monitored_emits_no_removals():
    previous = snapshot(
        **{
            CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")],
            CENTRE_B: [slot(CENTRE_B, AUG_26, "07:00")],
        }
    )
    current = snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")]})

    difference = diff_snapshots(previous, current, centres=[CENTRE_A])

    assert not difference.changed  # disabling a centre is not an availability event


def test_the_same_centres_diff_normally_while_another_comes_and_goes():
    previous = snapshot(
        **{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")], CENTRE_B: []}
    )
    current = snapshot(
        **{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26"), slot(CENTRE_A, AUG_26, "09:18")]}
    )

    difference = diff_snapshots(previous, current, centres=[CENTRE_A])

    assert [s.display for s in difference.added] == ["09:18"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_changed_block_reads_like_the_example():
    difference = AvailabilityDiff(
        added=(
            slot(CENTRE_A, AUG_26, "09:18", "09:44"),
            slot(CENTRE_A, AUG_26, "10:10", "10:36"),
        ),
        removed=(slot(CENTRE_A, AUG_27, "08:26", "08:52"),),
    )

    rendered = render_diff(difference)

    assert "HSC AVAILABILITY CHANGED" in rendered
    expected = (
        "New slots:\n  3242\n    2026-08-26\n      + 09:18-09:44\n      + 10:10-10:36"
    )
    assert expected in rendered
    assert "Removed slots:\n  3242\n    2026-08-27\n      - 08:26-08:52" in rendered


def test_only_additions_omits_the_removed_section():
    rendered = render_diff(AvailabilityDiff(added=(slot(CENTRE_A, AUG_26, "09:18"),)))

    assert "New slots:" in rendered
    assert "Removed slots:" not in rendered


def test_only_removals_omits_the_new_section():
    rendered = render_diff(AvailabilityDiff(removed=(slot(CENTRE_A, AUG_26, "09:18"),)))

    assert "Removed slots:" in rendered
    assert "New slots:" not in rendered


# --------------------------------------------------------------------------- #
# Persistence
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
        assert upsert is True, "the snapshot must be written with upsert semantics"
        self.replacements += 1
        self.documents[str(query["_id"])] = dict(document)


def test_the_snapshot_round_trips_through_its_own_document():
    collection = FakeCollection()
    store = MongoAvailabilitySnapshotStore(collection, now=lambda: NOW)
    store.save(
        snapshot(
            **{
                CENTRE_A: [
                    slot(CENTRE_A, AUG_26, "08:26", "08:52"),
                    slot(CENTRE_A, AUG_27, "09:18"),
                ],
                CENTRE_B: [],
            }
        )
    )

    loaded = store.load()

    assert loaded is not None
    assert set(loaded.centres) == {CENTRE_A, CENTRE_B}
    assert loaded.centres[CENTRE_B] == ()  # present and empty, not absent
    assert [s.display for s in loaded.centres[CENTRE_A]] == ["08:26-08:52", "09:18"]
    assert loaded.updated_at == NOW


def test_the_snapshot_lives_in_its_own_document():
    collection = FakeCollection()
    MongoAvailabilitySnapshotStore(collection, now=lambda: NOW).save(snapshot())

    assert set(collection.documents) == {SNAPSHOT_DOCUMENT_ID}
    assert SNAPSHOT_DOCUMENT_ID not in {DOCUMENT_ID, STATE_DOCUMENT_ID}
    document = collection.documents[SNAPSHOT_DOCUMENT_ID]
    assert set(document) == {"_id", "version", "updated_at", "centres"}
    assert document["version"] == SNAPSHOT_SCHEMA_VERSION


def test_the_snapshot_document_holds_nothing_but_availability():
    collection = FakeCollection()
    store = MongoAvailabilitySnapshotStore(collection, now=lambda: NOW)
    store.save(snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26", "08:52")]}))

    rendered = str(collection.documents[SNAPSHOT_DOCUMENT_ID])
    for forbidden in ("cookie", "token", "mongodb", "department", "Set-Cookie"):
        assert forbidden.lower() not in rendered.lower()


def test_the_whole_snapshot_is_replaced_at_once():
    collection = FakeCollection()
    store = MongoAvailabilitySnapshotStore(collection, now=lambda: NOW)

    store.save(snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")]}))
    store.save(snapshot(**{CENTRE_A: []}))

    assert collection.replacements == 2
    assert len(collection.documents) == 1
    assert collection.documents[SNAPSHOT_DOCUMENT_ID]["centres"] == {CENTRE_A: {}}


def test_a_future_version_fails_safely():
    collection = FakeCollection()
    store = MongoAvailabilitySnapshotStore(collection, now=lambda: NOW)
    store.save(snapshot(**{CENTRE_A: []}))
    collection.documents[SNAPSHOT_DOCUMENT_ID]["version"] = SNAPSHOT_SCHEMA_VERSION + 1

    with pytest.raises(SnapshotVersionUnsupported, match="will not be read or replaced"):
        store.load()


def test_a_database_failure_is_reported():
    class Broken:
        def find_one(self, _query: dict[str, Any]) -> None:
            raise RuntimeError("no primary")

        def replace_one(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("no primary")

    store = MongoAvailabilitySnapshotStore(Broken())

    with pytest.raises(SessionStoreError):
        store.load()
    with pytest.raises(SessionStoreError):
        store.save(snapshot())


def test_the_null_store_makes_every_run_a_baseline():
    store = NullAvailabilitySnapshotStore()
    store.save(snapshot(**{CENTRE_A: [slot(CENTRE_A, AUG_26, "08:26")]}))

    assert store.load() is None


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def with_slots(*entries: dict[str, str], day: str = "2026-08-26") -> ApiServer:
    return ApiServer(days=days_for(day), slots={f"{day}T00:00:00": list(entries)})


def test_the_first_run_saves_a_baseline_and_says_nothing():
    result, printed, store, _server = scan(server=with_slots(SLOT_0826, SLOT_0918))

    assert result.code == EXIT_OK
    assert printed.text.strip() == ""
    assert store.snapshots.saved[-1].slot_count == 2
    assert result.availability is not None and not result.availability.changed


def test_an_unchanged_second_run_says_nothing():
    store = FakeStore(stored=stored_session())
    scan(server=with_slots(SLOT_0826, SLOT_0918), store=store)

    _result, printed, _store, _server = scan(
        server=with_slots(SLOT_0826, SLOT_0918), store=store
    )

    assert printed.text.strip() == ""
    assert len(store.snapshots.saved) == 2  # written both times, announced neither


def test_a_new_slot_is_the_only_thing_printed():
    store = FakeStore(stored=stored_session())
    scan(server=with_slots(SLOT_0826), store=store)

    result, printed, _store, _server = scan(
        server=with_slots(SLOT_0826, SLOT_0918), store=store
    )

    assert "HSC AVAILABILITY CHANGED" in printed.text
    assert "+ 09:18-09:44" in printed.text
    assert "08:26-08:52" not in printed.text  # the unchanged slot is not news
    assert result.availability is not None and result.availability.changed


def test_a_removed_slot_is_the_only_thing_printed():
    store = FakeStore(stored=stored_session())
    scan(server=with_slots(SLOT_0826, SLOT_0918, SLOT_1010), store=store)

    _result, printed, _store, _server = scan(server=with_slots(SLOT_0826), store=store)

    assert "Removed slots:" in printed.text
    assert "- 09:18-09:44" in printed.text
    assert "- 10:10-10:36" in printed.text
    assert "New slots:" not in printed.text


def test_the_live_shape_of_the_thing():
    """Run 1 baseline, run 2 silence, run 3 one addition, run 4 two removals."""
    store = FakeStore(stored=stored_session())
    many = [SLOT_0826, SLOT_0910, SLOT_0918]

    _r1, first, _s, _a = scan(server=with_slots(*many), store=store)
    _r2, second, _s, _a = scan(server=with_slots(*many), store=store)
    _r3, third, _s, _a = scan(server=with_slots(*many, SLOT_1010), store=store)
    _r4, fourth, _s, _a = scan(server=with_slots(SLOT_0826, SLOT_1010), store=store)

    assert first.text.strip() == ""
    assert second.text.strip() == ""
    assert third.text.count("+") == 1 and "10:10-10:36" in third.text
    assert fourth.text.count("-") >= 2
    assert "09:10-09:36" in fourth.text and "09:18-09:44" in fourth.text
    assert "New slots:" not in fourth.text


# --------------------------------------------------------------------------- #
# An incomplete scan must never look like an empty one
# --------------------------------------------------------------------------- #


def stocked(store: FakeStore) -> FakeStore:
    """Give the store a previous snapshot with something in it."""
    scan(server=with_slots(SLOT_0826, SLOT_0918), store=store)
    return store


@pytest.mark.parametrize(
    ("name", "server"),
    [
        ("429", lambda: ApiServer(statuses={"departments": 429}, content_type="text/html")),
        ("502", lambda: ApiServer(statuses={"departments": 502}, content_type="text/html")),
        ("403", lambda: ForbiddenServer(days=EMPTY_DAYS)),
        ("401", lambda: ApiServer(statuses={"departments": 401}, content_type="text/html")),
        ("timeout", lambda: TimingOutServer(timeout_from="", days=days_for("2026-08-26"))),
        ("schema", lambda: ApiServer(days={"unexpected": True})),
    ],
)
def test_an_incomplete_scan_never_reports_removals(name, server):
    """The failure this whole design exists to prevent."""
    store = stocked(FakeStore(stored=stored_session()))
    before = store.snapshots.snapshot
    saves = len(store.snapshots.saved)

    result, printed, _store, _api = scan(server=server(), store=store)

    assert "AVAILABILITY CHANGED" not in printed.text
    assert "Removed" not in printed.text
    assert result.availability is None  # nothing was compared at all
    assert store.snapshots.snapshot is before  # and nothing was replaced
    assert len(store.snapshots.saved) == saves


def test_a_partial_centre_does_not_replace_the_snapshot():
    """One centre readable, one refused: still not a complete picture."""
    store = FakeStore(stored=stored_session())
    scan(server=with_slots(SLOT_0826), store=store, centres=(CENTRE_A,))
    before = store.snapshots.snapshot

    class HalfRefusing(ApiServer):
        """Answers for the first centre, refuses every attempt for the second."""

        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> Any:
            response = super().__call__(session, url, timeout)
            # 4641 resolves to internal department 3242 in the shared fixture.
            if self.endpoints[-1] == "days" and self.queries[-1]["departmentId"] == ["3242"]:
                from test_api_probe import FakeHttpResponse

                return FakeHttpResponse(500, {"Content-Type": "text/html"}, b"<html>")
            return response

    result, printed, _store, _api = scan(
        server=HalfRefusing(days=EMPTY_DAYS), store=store, centres=(CENTRE_A, CENTRE_B)
    )

    assert result.availability is None
    assert store.snapshots.snapshot is before
    assert "AVAILABILITY CHANGED" not in printed.text


# --------------------------------------------------------------------------- #
# Persistence failures
# --------------------------------------------------------------------------- #


def test_a_snapshot_that_cannot_be_written_announces_nothing():
    """Otherwise the next run announces the same change again."""
    store = stocked(FakeStore(stored=stored_session()))
    before = store.snapshots.snapshot
    store.snapshots.fails_save = True

    result, printed, _store, _api = scan(
        server=with_slots(SLOT_0826, SLOT_0918, SLOT_1010), store=store
    )

    assert result.code == EXIT_PERSISTENCE
    assert result.availability is None
    assert "AVAILABILITY CHANGED" not in printed.text
    assert "SNAPSHOT ERROR" in printed.text
    assert store.snapshots.snapshot is before  # the old one is intact


def test_a_snapshot_that_cannot_be_read_announces_nothing():
    store = FakeStore(stored=stored_session())
    store.snapshots.fails_load = True

    result, printed, _store, _api = scan(server=with_slots(SLOT_0826), store=store)

    assert result.code == EXIT_PERSISTENCE
    assert result.availability is None
    assert store.snapshots.saved == []  # nothing written on a failed read
    assert "SNAPSHOT ERROR" in printed.text


def test_the_snapshot_is_written_before_the_change_is_announced():
    """At-most-once: a crash between the two loses a message, never repeats it."""
    store = stocked(FakeStore(stored=stored_session()))
    order: list[str] = []

    original = store.snapshots.save

    def watched(snapshot: Any) -> None:
        order.append("saved")
        original(snapshot)

    store.snapshots.save = watched  # type: ignore[method-assign]

    def emit(text: str) -> None:
        order.append("emitted")

    run_headless_scan(
        store,
        [CENTRE_A],
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=with_slots(SLOT_0826, SLOT_0918, SLOT_1010),
        emit=emit,
        sleep=lambda _s: None,
        now=lambda: NOW,
    )

    assert order == ["saved", "emitted"]


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def test_the_snapshot_module_reaches_no_browser_and_no_notifier():
    from pathlib import Path

    from test_headless_monitor import BROWSER_NAMES, import_closure

    src = Path(__file__).resolve().parents[1] / "src" / "hsc_queue_monitor"
    closure = import_closure(src / "api" / "availability_snapshot.py")

    for path, imported in closure.items():
        for name in imported:
            assert not any(b in name.lower() for b in BROWSER_NAMES), path.name

    # Identifiers, not prose: a docstring may say "what a person books".
    from test_api_monitor import identifiers

    used = identifiers((src / "api" / "availability_snapshot.py").read_text(encoding="utf-8"))
    for forbidden in ("telegram", "notif", "book", "reserve", "cookie"):
        assert not [name for name in used if forbidden in name.lower()]
    # It borrows one error type from the session store, and nothing else.
    assert {n.lower() for n in used if "session" in n.lower()} == {
        "session_store",
        "sessionstoreerror",
    }


def test_the_three_documents_stay_three():
    assert len({DOCUMENT_ID, STATE_DOCUMENT_ID, SNAPSHOT_DOCUMENT_ID}) == 3


def test_a_snapshot_is_built_from_what_the_scan_read():
    built = snapshot_of(
        {
            CENTRE_A: {AUG_26: [TimeSlot(time(8, 26), "08:26", time(8, 52))]},
            CENTRE_B: {},
        },
        updated_at=NOW,
    )

    assert set(built.centres) == {CENTRE_A, CENTRE_B}
    assert built.centres[CENTRE_A][0].display == "08:26-08:52"
    assert built.centres[CENTRE_B] == ()
    assert built.slot_count == 1


def test_an_unchanged_run_is_logged_but_not_printed(caplog):
    caplog.set_level(logging.INFO)
    store = stocked(FakeStore(stored=stored_session()))

    _result, printed, _store, _api = scan(
        server=with_slots(SLOT_0826, SLOT_0918), store=store
    )

    assert printed.text.strip() == ""
    assert "Availability unchanged (2 slot(s))" in caplog.text
