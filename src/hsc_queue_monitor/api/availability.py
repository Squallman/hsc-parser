"""Availability read straight from the HSC API: departments -> days -> slots.

A live validation of the same question the UI scanner answers by clicking, run
against the three measured endpoints. It is not the production path: the
monitor, the queue flow and :class:`~..flow.availability.AvailabilityScanner`
are untouched and still drive the browser.

Three rules shape everything here.

**Nothing is guessed.** The department record's ``id``/``name`` are measured, so
they are required by name. The ``days`` and ``slots`` schemas are not, so they
are *recognised* rather than assumed: a field is used as a date because its
value parses as one and because it is the only such field in every record. When
that does not hold, the run prints the structure it actually received and stops
cleanly, rather than inventing a field name that happens to be plausible.

**The centre number is not the department id.** ``3242`` is what the site shows
a person; ``100`` is what the API wants. The mapping is resolved from the
departments response every time, never hardcoded and never assumed equal.

**Read only.** Every call is a GET (see :class:`~.client.HscApiClient`), no
date or time is ever selected, and a refusal — 401, 403, 429 — ends the run
where it happened instead of starting a retry.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as clock_time
from http import HTTPStatus
from typing import Any

from ..logging_config import sanitize
from ..models import ApiProbeError, DateAvailability, TimeSlot, identifies_service_center
from .bootstrap import QueueBootstrap
from .client import ApiCall, ApiRequestFailed, HscApiClient
from .probe import (
    KIND_BAD_JSON,
    KIND_FORBIDDEN,
    KIND_NETWORK_ERROR,
    KIND_NO_CONTENT,
    KIND_NON_JSON,
    KIND_RATE_LIMITED,
    KIND_REDIRECT,
    KIND_TIMEOUT,
    KIND_UNAUTHORIZED,
    sequence_in,
    type_name,
)

logger = logging.getLogger(__name__)

#: How much of a scalar is shown when reporting an unfamiliar schema.
SAMPLE_CHARS = 40
#: How many keys / records a schema report shows.
SCHEMA_KEYS = 12

#: Measured on the departments response. Required by name because they *were*
#: measured — unlike anything in days/slots.
DEPARTMENT_ID_FIELD = "id"
DEPARTMENT_NAME_FIELD = "name"
DEPARTMENT_ONLINE_FIELD = "allowOnlineCount"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ApiSchemaUnknown(ApiProbeError):
    """A response did not have a shape this parser can read without guessing.

    Carries a safe structural summary so the run can print what it *did* get and
    stop, which is how the next measurement gets made.
    """

    def __init__(self, what: str, payload: Any, detail: str = "") -> None:
        self.what = what
        self.summary: tuple[str, ...] = tuple(schema_lines(payload))
        message = f"The {what.lower()} response is not in a shape this parser reads."
        if detail:
            message = f"{message} {detail}"
        super().__init__(f"{message}\n" + "\n".join(f"  {line}" for line in self.summary))


class DepartmentUnresolved(ApiProbeError):
    """The visible centre number did not identify exactly one department.

    Never resolved by falling back to "the first one that looks close": an
    availability report for the wrong centre is worse than no report.
    """

    def __init__(self, centre_id: str, matches: Sequence[str], sample: Sequence[str]) -> None:
        if matches:
            detail = "\n".join(f"  - {name}" for name in matches)
            message = (
                f"Centre {centre_id!r} matched {len(matches)} departments, so it is "
                f"not safe to pick one:\n{detail}\nNothing was queried."
            )
        else:
            listing = "\n".join(f"  - {name}" for name in sample[:20]) or "  (none)"
            message = (
                f"Centre {centre_id!r} does not appear in the departments the API "
                f"returned. A sample of what it did return:\n{listing}\n"
                "Check the `id:` in config/service_centers.yaml against the names "
                "above — the visible centre number is what is matched."
            )
        super().__init__(message)
        self.centre_id = centre_id
        self.matches = tuple(matches)


# --------------------------------------------------------------------------- #
# Safe schema reporting
# --------------------------------------------------------------------------- #


def _sample(value: Any) -> str:
    """A scalar, short and redacted. Containers are described, never dumped."""
    if isinstance(value, dict | list):
        return type_name(value)
    text = str(sanitize(str(value)))
    return text if len(text) <= SAMPLE_CHARS else f"{text[:SAMPLE_CHARS]}…"


def schema_lines(payload: Any) -> list[str]:
    """What the response looks like, without printing the response."""
    lines = [f"payload: {type_name(payload)}"]
    if isinstance(payload, dict):
        lines += [
            f"  {key}: {type_name(value)}"
            for key, value in list(payload.items())[:SCHEMA_KEYS]
        ]

    items = sequence_in(payload)
    if items is None:
        return lines

    lines.append(f"list length: {len(items)}")
    if not items:
        return lines

    first = items[0]
    if isinstance(first, Mapping):
        lines.append("first item:")
        lines += [
            f"  {key}: {type_name(value)} = {_sample(value)}"
            for key, value in list(first.items())[:SCHEMA_KEYS]
        ]
    else:
        lines.append(f"first item: {type_name(first)} = {_sample(first)}")
    return lines


# --------------------------------------------------------------------------- #
# Value recognition
# --------------------------------------------------------------------------- #


def read_date(value: Any) -> date | None:
    """``2026-08-26`` or ``2026-08-26T00:00:00`` -> a date. Anything else, None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def read_clock(value: Any) -> clock_time | None:
    """``09:20``, ``09:20:00`` or ``2026-08-26T09:20:00`` -> a time.

    A colon is required, so a date-only string can never be mistaken for a
    midnight slot.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if ":" not in text:
        return None
    try:
        return datetime.fromisoformat(text).time()
    except ValueError:
        pass
    try:
        return clock_time.fromisoformat(text)
    except ValueError:
        return None


#: ``08:26:00`` or ``08:26`` and nothing else. Strict on purpose: an offset, a
#: fractional second or a full timestamp in this field would mean the schema has
#: changed, and that is a thing to report rather than to absorb.
_CLOCK = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


def read_strict_clock(value: Any) -> clock_time | None:
    """``08:26:00`` -> ``time(8, 26)``. Anything else at all, ``None``."""
    if not isinstance(value, str) or not _CLOCK.fullmatch(value.strip()):
        return None
    try:
        return clock_time.fromisoformat(value.strip())
    except ValueError:  # pragma: no cover - the pattern already rejects these
        return None


Reader = Callable[[Any], object | None]


def single_field(records: Sequence[Mapping[str, Any]], reader: Reader, what: str) -> str:
    """The one field that carries a readable value in *every* record.

    This is the whole "do not guess field names" rule in one function: the field
    is identified by what its values are, and only when the answer is
    unambiguous. Two candidate fields, or none, is a schema to report — not a
    coin to toss.
    """
    common: set[str] | None = None
    for record in records:
        readable = {key for key, value in record.items() if reader(value) is not None}
        common = readable if common is None else (common & readable)
        if not common:
            break

    if common is None or len(common) != 1:
        found = sorted(common or ())
        raise ApiSchemaUnknown(
            what,
            list(records),
            f"Fields that could carry it: {found or 'none'} — "
            "exactly one is needed to read it without guessing.",
        )
    return common.pop()


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Department:
    """One service centre as the API knows it.

    :attr:`department_id` is the API's internal identity and is *not* assumed to
    equal the number a person sees on the card.
    """

    department_id: int
    display_name: str
    allow_online_count: int | None = None


def _list_of(payload: Any, what: str) -> Sequence[Any]:
    items = sequence_in(payload)
    if items is None:
        raise ApiSchemaUnknown(what, payload, "No list of records could be found in it.")
    return items


def parse_departments(payload: Any) -> list[Department]:
    """Every department in the response, in the order the API listed them.

    One parser for this endpoint, used both by a scan resolving a single centre
    and by ``init-config`` cataloguing all of them — a second reading of the
    same JSON is a second thing to keep true.
    """
    records = _list_of(payload, "Departments")
    if not all(isinstance(record, Mapping) for record in records):
        raise ApiSchemaUnknown("Departments", payload, "The list does not hold objects.")

    typed: list[Mapping[str, Any]] = list(records)
    if not all(
        DEPARTMENT_ID_FIELD in record and DEPARTMENT_NAME_FIELD in record
        for record in typed
    ):
        raise ApiSchemaUnknown(
            "Departments",
            payload,
            f"Every record needs {DEPARTMENT_ID_FIELD!r} and "
            f"{DEPARTMENT_NAME_FIELD!r}; these were measured on the live response.",
        )

    departments: list[Department] = []
    for record in typed:
        try:
            department_id = int(record[DEPARTMENT_ID_FIELD])
        except (TypeError, ValueError) as exc:
            raise ApiSchemaUnknown(
                "Departments",
                payload,
                f"{DEPARTMENT_ID_FIELD!r} is not a number on every record.",
            ) from exc
        online = record.get(DEPARTMENT_ONLINE_FIELD)
        departments.append(
            Department(
                department_id=department_id,
                display_name=str(record[DEPARTMENT_NAME_FIELD]),
                allow_online_count=online if isinstance(online, int) else None,
            )
        )
    return departments


def resolve_department(payload: Any, centre_id: str) -> Department:
    """Find the department whose *name* carries the visible centre number."""
    departments = parse_departments(payload)
    matches = [
        department
        for department in departments
        if identifies_service_center(department.display_name, centre_id)
    ]
    if len(matches) != 1:
        raise DepartmentUnresolved(
            centre_id,
            [department.display_name for department in matches],
            [department.display_name for department in departments],
        )
    return matches[0]


def parse_days(payload: Any) -> list[date]:
    """Every date the API returned, de-duplicated and in order."""
    items = _list_of(payload, "Days")
    if not items:
        return []

    if all(isinstance(item, str) for item in items):
        found = [read_date(item) for item in items]
        if any(day is None for day in found):
            raise ApiSchemaUnknown("Days", payload, "Not every entry is a date.")
        return sorted({day for day in found if day is not None})

    if not all(isinstance(item, Mapping) for item in items):
        raise ApiSchemaUnknown("Days", payload, "The list mixes objects and scalars.")

    records: list[Mapping[str, Any]] = list(items)
    field = single_field(records, read_date, "Days")
    return sorted({day for record in records if (day := read_date(record[field]))})


#: The measured ``/slots`` record: ``{"startTime": "08:26:00",
#: "stopTime": "08:52:00"}``. Both fields are required by name because both were
#: measured; nothing else in the record is read, and no other field is inferred.
SLOT_START_FIELD = "startTime"
SLOT_STOP_FIELD = "stopTime"


def _measured_slots(records: Sequence[Mapping[str, Any]], payload: Any) -> list[TimeSlot]:
    """The measured window schema, all-or-nothing.

    Once *any* record mentions the measured fields the whole list is held to
    them: a record missing ``stopTime``, or carrying a time that does not parse
    strictly, fails the payload rather than being skipped. Partially parsing a
    list of appointment times would report less availability than the site has
    and give no sign that it had done so.
    """
    windows: list[TimeSlot] = []
    for record in records:
        start = read_strict_clock(record.get(SLOT_START_FIELD))
        stop = read_strict_clock(record.get(SLOT_STOP_FIELD))
        if start is None or stop is None:
            missing = [
                field
                for field in (SLOT_START_FIELD, SLOT_STOP_FIELD)
                if read_strict_clock(record.get(field)) is None
            ]
            raise ApiSchemaUnknown(
                "Slots",
                payload,
                f"Every record needs {SLOT_START_FIELD!r} and {SLOT_STOP_FIELD!r} as "
                f"HH:MM:SS; one record has an unusable {' and '.join(missing)}. "
                "Nothing is skipped — a partial read would under-report availability.",
            )
        windows.append(
            TimeSlot(time=start, text=start.strftime("%H:%M"), end_time=stop)
        )
    return sorted(windows, key=lambda slot: slot.time)


def parse_slots(payload: Any) -> list[TimeSlot]:
    """Every time the API returned for one date, in order.

    Times only. Nothing here decides that a slot is *bookable* from an unmeasured
    boolean — the endpoint is asked for one date's slots and its answer is
    reported as given.
    """
    items = _list_of(payload, "Slots")
    if not items:
        # A date with nothing free. An answer, not a schema problem.
        return []

    if any(
        isinstance(item, Mapping) and (SLOT_START_FIELD in item or SLOT_STOP_FIELD in item)
        for item in items
    ):
        if not all(isinstance(item, Mapping) for item in items):
            raise ApiSchemaUnknown("Slots", payload, "The list mixes objects and scalars.")
        return _measured_slots(list(items), payload)

    if all(isinstance(item, str) for item in items):
        found = [(item, read_clock(item)) for item in items]
        if any(clock is None for _text, clock in found):
            raise ApiSchemaUnknown("Slots", payload, "Not every entry is a time.")
        return sorted(
            (TimeSlot(time=clock, text=text) for text, clock in found if clock),
            key=lambda slot: slot.time,
        )

    if not all(isinstance(item, Mapping) for item in items):
        raise ApiSchemaUnknown("Slots", payload, "The list mixes objects and scalars.")

    records: list[Mapping[str, Any]] = list(items)
    field = single_field(records, read_clock, "Slots")
    slots = [
        TimeSlot(time=clock, text=str(record[field]))
        for record in records
        if (clock := read_clock(record[field]))
    ]
    return sorted(slots, key=lambda slot: slot.time)


# --------------------------------------------------------------------------- #
# The sequence
# --------------------------------------------------------------------------- #


#: Failures that end a centre's scan rather than being recorded and stepped
#: over. An explicit refusal (401/403/429) or a broken transport means the next
#: date would be a worse idea, not a better one — and after HSC has said "too
#: many requests", continuing down the list is precisely what it asked us not to
#: do. Everything else is treated as a fact about *that date* and the scan moves
#: on, because one odd response is not evidence about the next one.
STOP_KINDS: frozenset[str] = frozenset(
    {
        KIND_RATE_LIMITED,
        KIND_UNAUTHORIZED,
        KIND_FORBIDDEN,
        KIND_REDIRECT,
        KIND_TIMEOUT,
        KIND_NETWORK_ERROR,
    }
)

STATUS_BOOKABLE = "bookable"
STATUS_PARTIAL = "partial"
STATUS_NO_DATES = "no-dates"
STATUS_NO_TIMES = "no-times"


def http_reason(status: int) -> str:
    """``429`` -> ``Too Many Requests``. Empty for a status with no standard name."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def describe_failure(call: ApiCall) -> str:
    """A short, safe description of why one call did not produce JSON.

    Statuses get their standard name — ``HTTP 429 Too Many Requests`` — so the
    report says what happened without quoting a body or a header.
    """
    outcome = call.outcome
    if outcome.status is None:
        return f"{outcome.kind}: {outcome.error}" if outcome.error else outcome.kind

    described = f"HTTP {outcome.status} {http_reason(outcome.status)}".strip()
    # For a status that does not itself explain the problem, name the shape too.
    if outcome.kind in {KIND_NON_JSON, KIND_BAD_JSON, KIND_NO_CONTENT}:
        described = f"{described} ({outcome.kind})"
    return described


class Pacer:
    """A minimum interval between successive requests, measured monotonically.

    Not backoff and not a retry: the same request is never made twice. This only
    decides how long to wait before the *next* date, and it subtracts whatever
    the previous request already spent, so a slow response costs no extra delay.
    """

    def __init__(
        self,
        interval: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = max(float(interval), 0.0)
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> float:
        """Block until the interval has elapsed since :meth:`mark`. Returns the wait."""
        if self.interval <= 0 or self._last is None:
            return 0.0  # never before the first request
        remaining = self.interval - (self._clock() - self._last)
        if remaining <= 0:
            return 0.0  # the request itself already covered the interval
        logger.info("Waiting %.2fs before next slots request", remaining)
        self._sleep(remaining)
        return remaining

    def mark(self) -> None:
        """Record that a request is starting now."""
        self._last = self._clock()


@dataclass(frozen=True, slots=True)
class ApiScan:
    """One centre, read end to end through the API."""

    service_id: int
    requested: str
    department: Department
    dates: tuple[date, ...]
    availability: tuple[DateAvailability, ...]
    calls: tuple[ApiCall, ...]
    #: Dates the API returned that were never asked about, because --max-dates
    #: capped the run or a refusal ended it.
    skipped_dates: int = 0
    #: Why the scan stopped early, in a few words. Empty on the normal path.
    stopped: str = ""
    #: Set when a slots response could not be read. The schema block is printed
    #: alongside the partial results rather than instead of them.
    schema_stop: ApiSchemaUnknown | None = None

    @property
    def slot_count(self) -> int:
        return sum(len(day.slots) for day in self.availability)

    @property
    def bookable(self) -> bool:
        """At least one date with at least one time. The same rule as the UI scan."""
        return any(day.has_slots for day in self.availability)

    @property
    def failed_dates(self) -> tuple[DateAvailability, ...]:
        return tuple(day for day in self.availability if day.error)

    @property
    def complete(self) -> bool:
        """Whether every date the API offered was actually read."""
        return not self.failed_dates and self.skipped_dates == 0

    @property
    def status(self) -> str:
        """What this scan learned, in one word.

        ``partial`` exists so an incomplete answer can never be mistaken for a
        complete one: if a date was refused, "bookable" would overstate what was
        read and "no-times" would understate it.
        """
        if not self.dates:
            return STATUS_NO_DATES
        if self.failed_dates or self.skipped_dates:
            return STATUS_PARTIAL
        return STATUS_BOOKABLE if self.bookable else STATUS_NO_TIMES


def scan_centre(
    client: HscApiClient,
    centre_id: str,
    *,
    max_dates: int = 0,
    slot_interval: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ApiScan:
    """departments -> resolve -> days -> one slots call per date. Blocking.

    Every call shares ``client.session``, so each response's ``Set-Cookie``
    travels into the next request — which is what makes the cookie-state table
    meaningful. ``max_dates`` of 0 means every date the API returned;
    ``slot_interval`` of 0 disables pacing.
    """
    departments = client.require(client.departments())
    department = resolve_department(departments.outcome.payload, centre_id)
    scan = scan_department(
        client,
        department,
        requested=centre_id,
        max_dates=max_dates,
        pacer=Pacer(slot_interval, sleep=sleep, clock=clock),
    )
    # The departments call belongs to this scan's story too.
    return replace(scan, calls=(departments, *scan.calls))


def scan_department(
    client: HscApiClient,
    department: Department,
    *,
    requested: str = "",
    max_dates: int = 0,
    pacer: Pacer | None = None,
) -> ApiScan:
    """days -> one slots call per date, for an already-resolved department.

    Separated from :func:`scan_centre` so a monitor watching several centres can
    resolve them all from *one* departments response instead of asking again per
    centre — the same reason the ``pacer`` is passed in rather than created here:
    the interval belongs to the server, not to one centre's turn.

    A date that fails is recorded as a failed date, never as a missing one, and
    never costs the dates already read: partial availability is the normal
    outcome of a scan that met a refusal, and discarding it would throw away
    exactly the information the run was for.
    """
    calls: list[ApiCall] = []
    pacer = pacer if pacer is not None else Pacer(0.0)
    centre_id = requested or str(department.department_id)
    logger.info(
        "Centre %s is department id %d (%s)",
        centre_id,
        department.department_id,
        department.display_name,
    )

    days = client.require(client.days(department.department_id))
    calls.append(days)
    dates = parse_days(days.outcome.payload)

    wanted = dates if max_dates <= 0 else dates[:max_dates]
    availability: list[DateAvailability] = []
    stopped = "--max-dates" if len(wanted) < len(dates) else ""
    schema_stop: ApiSchemaUnknown | None = None

    for day in wanted:
        # Strictly sequential, one request per date, never concurrent and never
        # repeated. The wait is before the request and only between dates.
        pacer.wait()
        pacer.mark()
        try:
            slots = client.require(client.slots(department.department_id, day))
        except ApiRequestFailed as failure:
            calls.append(failure.call)
            error = describe_failure(failure.call)
            availability.append(DateAvailability(date=day, slots=(), error=error))
            if failure.call.outcome.kind == KIND_RATE_LIMITED:
                logger.warning(
                    "HSC rate limit reached while reading slots for %s; preserving "
                    "partial results and stopping centre scan.",
                    day.isoformat(),
                )
            if failure.call.outcome.kind in STOP_KINDS:
                stopped = error
                break
            logger.info("Slots for %s could not be read (%s); continuing", day, error)
            continue

        calls.append(slots)
        try:
            parsed = tuple(parse_slots(slots.outcome.payload))
        except ApiSchemaUnknown as unknown:
            # Systematic, not per-date: every other date would answer in the
            # same shape. Record it, keep what was read, and stop.
            schema_stop = unknown
            availability.append(
                DateAvailability(
                    date=day, slots=(), error=f"{unknown.what} response not recognised"
                )
            )
            stopped = "unreadable slots schema"
            break
        availability.append(DateAvailability(date=day, slots=parsed))

    return ApiScan(
        service_id=client.service_id,
        requested=centre_id,
        department=department,
        dates=tuple(dates),
        availability=tuple(availability),
        calls=tuple(calls),
        skipped_dates=len(dates) - len(availability),
        stopped=stopped,
        schema_stop=schema_stop,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_cookie_state(calls: Iterable[ApiCall]) -> list[str]:
    """The per-request session-cookie table. Fingerprints only, never values."""
    lines = ["Cookie state:"]
    for call in calls:
        lines += [f"  {call.label}:", f"    {call.cookie_state()}"]
    return lines


def render_api_availability(scan: ApiScan, *, bootstrap: QueueBootstrap | None = None) -> str:
    """The report for one centre, ready to print."""
    department = scan.department
    lines = ["", "API AVAILABILITY", ""]
    if bootstrap is not None:
        lines += [*bootstrap.render(), ""]
    lines += [
        f"Service ID: {scan.service_id}",
        "",
        "Centre:",
        f"  requested:   {scan.requested}",
        # Resolved from this run's departments response. Never persisted, never
        # assumed equal to the number above.
        f"  internal id: {department.department_id}",
        f"  name:        {department.display_name}",
    ]
    if department.allow_online_count is not None:
        lines.append(f"  allowOnlineCount: {department.allow_online_count}")

    lines += ["", "Dates:"]
    lines += [f"  {day.isoformat()}" for day in scan.dates] or ["  (none)"]
    if scan.skipped_dates:
        lines.append(
            f"  … {scan.skipped_dates} not queried ({scan.stopped or '--max-dates'})"
        )

    lines += ["", "Slots:", ""]
    if not scan.availability:
        lines.append("  (no date was queried)")
    for day in scan.availability:
        lines.append(f"  {day.date.isoformat()}")
        if day.error:
            # Kept in the report rather than dropped: a date that was refused is
            # a different thing from a date with nothing free.
            lines.append(f"    ERROR: {day.error}")
        else:
            lines += [f"    {slot.display_range}" for slot in day.slots] or ["    (none)"]
        lines.append("")

    lines += render_cookie_state(scan.calls)
    lines += [
        "",
        f"Status: {scan.status}",
        f"{scan.slot_count} free time(s) across "
        f"{len(scan.availability) - len(scan.failed_dates)} date(s) read"
        + (f", {len(scan.failed_dates)} refused" if scan.failed_dates else "")
        + (f", {scan.skipped_dates} not queried" if scan.skipped_dates else "")
        + ".",
    ]
    return "\n".join(lines)


def render_schema_stop(error: ApiSchemaUnknown) -> str:
    """What to print when a response shape is not readable yet."""
    return "\n".join(
        [
            "",
            f"{error.what} response schema:",
            *(f"  {line}" for line in error.summary),
            "",
            "Stopping here. No field name is guessed: add support once the shape "
            "above has been measured.",
        ]
    )
