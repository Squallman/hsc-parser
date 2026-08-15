"""What was free last time, so this run can say only what changed.

A scheduled job that reprints its whole answer every five minutes is a job
nobody reads. This module is the difference between "here is the availability"
and "this appeared, that went away" — and, once a notifier exists, between one
message and two hundred and eighty a day.

Three rules make the difference safe to act on.

**Only a complete scan may replace the snapshot.** A partial read is an unknown,
not an empty result. If a timeout could overwrite the snapshot, the next run
would compare twenty slots against nothing and announce that twenty slots
disappeared — the single worst thing a change detector can do.

**A centre seen for the first time is a baseline, not news.** That covers the
first run of all, and equally a centre the operator has just enabled: neither is
a hundred and thirty-nine new appointments, and reporting them as such would
teach the reader to ignore the report.

**Persist, then emit.** The snapshot is written before the change is announced,
so a crash in between loses a message rather than repeating it forever. That is
a deliberate trade: at-most-once, chosen because a missed change is visible on
the next run and a repeated one is not visible at all.

What is stored is the logical availability and nothing else — no cookie, no
token, no internal department id, no response body.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any, Final, Protocol

from ..models import TimeSlot
from .session_store import SessionStoreError

logger = logging.getLogger(__name__)

#: The third document, beside the encrypted session and the monitor state.
SNAPSHOT_DOCUMENT_ID: Final = "hsc-availability-snapshot"
SNAPSHOT_SCHEMA_VERSION: Final = 1


class SnapshotVersionUnsupported(SessionStoreError):
    """A snapshot written by a newer version of this project.

    Raised rather than ignored: guessing at a shape we do not know would either
    invent changes or hide them, and both are worse than saying so and leaving
    the document alone.
    """


# --------------------------------------------------------------------------- #
# What a slot is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, order=True, slots=True)
class SlotKey:
    """A slot's identity: which centre, which day, when it starts.

    Not the end time, which is metadata — a window that moved its end is the
    same appointment. Not the internal department id either: that changes
    between runs and is not what a person books.
    """

    centre: str
    date: date
    start_time: clock_time


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """One free appointment, as it is remembered between runs."""

    centre: str
    date: date
    start_time: clock_time
    end_time: clock_time | None = None

    @property
    def key(self) -> SlotKey:
        return SlotKey(centre=self.centre, date=self.date, start_time=self.start_time)

    @property
    def display(self) -> str:
        """``08:26-08:52``, or just ``08:26`` when no end was reported."""
        start = self.start_time.strftime("%H:%M")
        if self.end_time is None:
            return start
        return f"{start}-{self.end_time.strftime('%H:%M')}"

    @classmethod
    def from_time_slot(cls, centre: str, day: dt.date, slot: TimeSlot) -> AvailableSlot:
        return cls(centre=centre, date=day, start_time=slot.time, end_time=slot.end_time)


def _centre_order(centre: str) -> tuple[int, str]:
    return (int(centre), "") if centre.isdigit() else (10**9, centre)


def sort_key(slot: AvailableSlot) -> tuple[tuple[int, str], date, clock_time]:
    """Centre, then date, then start. The order every report is printed in."""
    return (_centre_order(slot.centre), slot.date, slot.start_time)


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    """Every monitored centre, and what was free in it.

    A centre with an empty tuple is *present and empty*, which is a different
    thing from a centre that is absent: the first has a baseline, the second
    has never been read.
    """

    centres: Mapping[str, tuple[AvailableSlot, ...]] = field(default_factory=dict)
    updated_at: datetime | None = None

    @property
    def slots(self) -> tuple[AvailableSlot, ...]:
        return tuple(slot for centre in self.centres.values() for slot in centre)

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    def by_key(self, centres: Iterable[str] | None = None) -> dict[SlotKey, AvailableSlot]:
        wanted = set(centres) if centres is not None else set(self.centres)
        return {
            slot.key: slot
            for centre, slots in self.centres.items()
            if centre in wanted
            for slot in slots
        }


def snapshot_of(
    readings: Mapping[str, Mapping[date, Sequence[TimeSlot]]],
    *,
    updated_at: datetime | None = None,
) -> AvailabilitySnapshot:
    """Build a snapshot from what a scan read, centre by centre."""
    centres = {
        centre: tuple(
            sorted(
                (
                    AvailableSlot.from_time_slot(centre, day, slot)
                    for day, slots in days.items()
                    for slot in slots
                ),
                key=sort_key,
            )
        )
        for centre, days in readings.items()
    }
    return AvailabilitySnapshot(centres=centres, updated_at=updated_at)


# --------------------------------------------------------------------------- #
# The diff
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AvailabilityDiff:
    """What appeared and what went away. The seam a notifier will attach to."""

    added: tuple[AvailableSlot, ...] = ()
    removed: tuple[AvailableSlot, ...] = ()
    #: Centres seen for the first time this run. Their slots are a baseline and
    #: deliberately appear in neither list.
    baselined: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def diff_snapshots(
    previous: AvailabilitySnapshot | None,
    current: AvailabilitySnapshot,
    *,
    centres: Sequence[str],
) -> AvailabilityDiff:
    """Compare, but only the centres this run was actually asked about.

    A centre missing from ``previous`` is baselined silently — first sight of a
    centre is not an event, whether that is the first run of all or a centre the
    operator enabled an hour ago. A centre missing from ``centres`` is ignored
    entirely: it is not being monitored, so its old slots are not news either.
    """
    known = set(previous.centres) if previous is not None else set()
    monitored = [centre for centre in centres if centre in current.centres]

    baselined = tuple(centre for centre in monitored if centre not in known)
    comparable = [centre for centre in monitored if centre in known]

    before = previous.by_key(comparable) if previous is not None else {}
    after = current.by_key(comparable)

    added = tuple(sorted((s for k, s in after.items() if k not in before), key=sort_key))
    removed = tuple(sorted((s for k, s in before.items() if k not in after), key=sort_key))
    return AvailabilityDiff(added=added, removed=removed, baselined=baselined)


def render_diff(diff: AvailabilityDiff) -> str:
    """One compact block. Never the whole current list."""
    lines = ["", "HSC AVAILABILITY CHANGED", ""]
    if diff.added:
        lines += _group("New slots:", diff.added, "+")
    if diff.removed:
        lines += _group("Removed slots:", diff.removed, "-")
    return "\n".join(lines)


def _group(title: str, slots: Sequence[AvailableSlot], sign: str) -> list[str]:
    lines = [title]
    centre: str | None = None
    day: date | None = None
    for slot in slots:
        if slot.centre != centre:
            centre, day = slot.centre, None
            lines.append(f"  {slot.centre}")
        if slot.date != day:
            day = slot.date
            lines.append(f"    {slot.date.isoformat()}")
        lines.append(f"      {sign} {slot.display}")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class AvailabilitySnapshotStore(Protocol):
    """Where the last complete availability lives between runs."""

    def load(self) -> AvailabilitySnapshot | None: ...

    def save(self, snapshot: AvailabilitySnapshot) -> None: ...

    def close(self) -> None: ...


class NullAvailabilitySnapshotStore:
    """No persistence: every run is a first run, and nothing is ever a change."""

    def __init__(self) -> None:
        self.saved: list[AvailabilitySnapshot] = []

    def load(self) -> AvailabilitySnapshot | None:
        return None

    def save(self, snapshot: AvailabilitySnapshot) -> None:
        self.saved.append(snapshot)

    def close(self) -> None:
        return None


class MongoAvailabilitySnapshotStore:
    """One document, replaced whole. Never a slot at a time.

    Takes a collection rather than a URI, so it shares the connection the
    session store opened: three documents, one client.
    """

    def __init__(
        self,
        collection: Any,
        *,
        document_id: str = SNAPSHOT_DOCUMENT_ID,
        now: Any = None,
    ) -> None:
        self._collection = collection
        self.document_id = document_id
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def load(self) -> AvailabilitySnapshot | None:
        try:
            document = self._collection.find_one({"_id": self.document_id})
        except Exception as exc:
            raise SessionStoreError(
                f"Could not read the availability snapshot: {type(exc).__name__}"
            ) from exc

        if not document:
            return None

        version = document.get("version")
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotVersionUnsupported(
                f"The stored availability snapshot is version {version!r}, and this "
                f"build understands version {SNAPSHOT_SCHEMA_VERSION}.\nIt will not "
                "be read or replaced: comparing against a shape this code does not "
                "know would invent changes or hide them."
            )

        centres: dict[str, tuple[AvailableSlot, ...]] = {}
        for centre, days in (document.get("centres") or {}).items():
            slots: list[AvailableSlot] = []
            for raw_day, entries in (days or {}).items():
                day = _read_date(raw_day)
                if day is None:
                    continue
                slots += [
                    slot
                    for entry in entries or []
                    if (slot := _read_slot(str(centre), day, entry)) is not None
                ]
            centres[str(centre)] = tuple(sorted(slots, key=sort_key))

        return AvailabilitySnapshot(
            centres=centres, updated_at=_as_utc(document.get("updated_at"))
        )

    def save(self, snapshot: AvailabilitySnapshot) -> None:
        document = {
            "_id": self.document_id,
            "version": SNAPSHOT_SCHEMA_VERSION,
            "updated_at": self._now(),
            "centres": {
                centre: _by_date(slots) for centre, slots in snapshot.centres.items()
            },
        }
        try:
            self._collection.replace_one(
                {"_id": self.document_id}, document, upsert=True
            )
        except Exception as exc:
            raise SessionStoreError(
                f"Could not persist the availability snapshot: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        """The session store owns the client, so there is nothing to close."""
        return None


def _by_date(slots: Sequence[AvailableSlot]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for slot in sorted(slots, key=sort_key):
        entry = {"start": slot.start_time.strftime("%H:%M")}
        if slot.end_time is not None:
            entry["end"] = slot.end_time.strftime("%H:%M")
        grouped.setdefault(slot.date.isoformat(), []).append(entry)
    return grouped


def _read_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        logger.warning("Ignoring an unreadable date in the availability snapshot")
        return None


def _read_slot(centre: str, day: date, entry: Any) -> AvailableSlot | None:
    if not isinstance(entry, Mapping):
        return None
    start = _read_clock(entry.get("start"))
    if start is None:
        return None
    return AvailableSlot(
        centre=centre, date=day, start_time=start, end_time=_read_clock(entry.get("end"))
    )


def _read_clock(value: Any) -> clock_time | None:
    if not isinstance(value, str):
        return None
    try:
        return clock_time.fromisoformat(value)
    except ValueError:
        return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
