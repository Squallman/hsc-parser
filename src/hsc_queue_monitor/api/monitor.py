"""A dry-run monitor over the read-only API path.

Watches one to five centres by polling the measured endpoints and printing what
*changed* — a console experiment sitting beside the browser monitor, not a
replacement for it. Nothing here books or notifies: there is no Telegram and no
appointment is ever taken. The one thing it does write is the HTTP session
itself, encrypted, so a restart inside the queue session's 900-second life does
not have to open a browser again (see :mod:`.session_store`).

Four properties this module exists to guarantee.

**A failure is never a disappearance.** If a centre's read is incomplete — a
refusal, a timeout, an unreadable schema — its previous availability is kept and
*not* diffed. Reporting "all slots gone" because HSC answered 429 would be worse
than reporting nothing, so a centre's state is replaced only after a complete
read of that centre.

**One recovery, never a loop, and only for 403.** A 403 is the only outcome
that buys a browser: one re-authenticate + bootstrap + fresh client, and one
repeat of the scan, per cycle. If that does not work the cycle is reported as
failed and the monitor waits for the next one. Nothing else — 401, 429, 500,
502, 204, timeouts, redirects, unreadable schemas — opens Chromium at all.

**Chromium is not running during polling.** The provider opens the browser,
authenticates, mints the queue session, copies the cookies into a
``requests.Session`` and closes the browser again, all inside one call. From
then on the monitor is an HTTP client and nothing else.

**One scan at a time.** Cycles are strictly sequential and paced from scan
*start*, so a slow scan shortens the wait instead of pushing the schedule out —
and never overlaps the next one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as clock_time
from typing import Any, Protocol

from ..models import HscMonitorError, TimeSlot
from .availability import (
    ApiScan,
    ApiSchemaUnknown,
    Department,
    DepartmentUnresolved,
    Pacer,
    describe_failure,
    resolve_department,
    scan_department,
)
from .bootstrap import QueueBootstrap
from .client import ApiRequestFailed, HscApiClient
from .probe import KIND_FORBIDDEN, CookieInfo
from .session_store import (
    NullSessionStore,
    PersistedSession,
    SessionPersister,
    SessionStore,
    jar_fingerprint,
)

logger = logging.getLogger(__name__)

#: The one outcome that buys a browser. Deliberately narrow: opening Chromium
#: is the most expensive and most disruptive thing this monitor can do, so it
#: happens only for the status that actually means "this identity is not
#: accepted". Everything else — 401, 429, 500, 502, 204, a timeout, a redirect,
#: an unreadable schema — is reported, keeps the previous state, and waits for
#: the next cycle.
AUTH_RECOVERY_KINDS: frozenset[str] = frozenset({KIND_FORBIDDEN})

#: A slot's identity within a centre. The end time is metadata: two appointments
#: never share a start, so the start is enough to say "the same slot".
SlotKey = tuple[date, clock_time]
CentreState = Mapping[SlotKey, TimeSlot]


def state_of(scan: ApiScan) -> dict[SlotKey, TimeSlot]:
    """Every free time in a completed scan, keyed by (date, start)."""
    return {
        (day.date, slot.time): slot for day in scan.availability for slot in day.slots
    }


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


# --------------------------------------------------------------------------- #
# One centre, one cycle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CentreReading:
    """What one cycle learned about one centre.

    ``complete`` is the only thing that decides whether this may replace the
    remembered state. An incomplete reading carries no slots — not because none
    were found, but because what was found cannot be trusted as the whole
    picture.
    """

    centre_id: str
    complete: bool
    state: CentreState = field(default_factory=dict)
    detail: str = ""
    dates: int = 0
    needs_auth_recovery: bool = False
    #: Every outcome kind this centre's calls produced. Recorded rather than
    #: interpreted, because the two operational paths judge them differently: a
    #: 401 is worth a browser to nobody, but it *is* worth stopping a scheduled
    #: run over. See :data:`.headless_monitor.AUTH_REQUIRED_KINDS`.
    kinds: frozenset[str] = frozenset()

    @property
    def slot_count(self) -> int:
        return len(self.state)

    def summary(self) -> str:
        """The compact baseline line."""
        if not self.complete:
            return f"partial — {self.detail}"
        if not self.state:
            return "no availability"
        days = len({day for day, _start in self.state})
        return f"{self.slot_count} slot(s) across {days} date(s)"

    def short(self) -> str:
        """The even-more-compact steady-state line."""
        return f"partial — {self.detail}" if not self.complete else f"{self.slot_count} slots"


def read_centres(
    client: HscApiClient,
    centre_ids: Sequence[str],
    *,
    max_dates: int = 0,
    pacer: Pacer | None = None,
) -> list[CentreReading]:
    """One cycle's reads: departments once, then days/slots per centre.

    Blocking — call it off the event loop. The departments response is fetched
    a single time and every centre is resolved from it, because asking again per
    centre would be the same question twice.
    """
    pacer = pacer if pacer is not None else Pacer(0.0)

    try:
        departments = client.require(client.departments())
    except ApiRequestFailed as failure:
        detail = describe_failure(failure.call)
        broken = failure.call.outcome.kind in AUTH_RECOVERY_KINDS
        # Nothing can be read this cycle; every centre keeps what it had.
        return [
            CentreReading(
                centre_id=centre_id,
                complete=False,
                detail=detail,
                needs_auth_recovery=broken,
                kinds=frozenset({failure.call.outcome.kind}),
            )
            for centre_id in centre_ids
        ]

    payload = departments.outcome.payload
    return [
        _read_centre(client, payload, centre_id, max_dates=max_dates, pacer=pacer)
        for centre_id in centre_ids
    ]


def _read_centre(
    client: HscApiClient,
    departments_payload: Any,
    centre_id: str,
    *,
    max_dates: int,
    pacer: Pacer,
) -> CentreReading:
    try:
        department: Department = resolve_department(departments_payload, centre_id)
    except (DepartmentUnresolved, ApiSchemaUnknown) as exc:
        return CentreReading(centre_id=centre_id, complete=False, detail=_first_line(exc))

    try:
        scan = scan_department(
            client, department, requested=centre_id, max_dates=max_dates, pacer=pacer
        )
    except ApiRequestFailed as failure:
        return CentreReading(
            centre_id=centre_id,
            complete=False,
            detail=describe_failure(failure.call),
            needs_auth_recovery=failure.call.outcome.kind in AUTH_RECOVERY_KINDS,
            kinds=frozenset({failure.call.outcome.kind}),
        )
    except ApiSchemaUnknown as unknown:
        # A days response this parser cannot read. For a one-shot report that is
        # worth stopping over; for a monitor it is this centre's cycle, and the
        # centre keeps whatever it last knew.
        return CentreReading(
            centre_id=centre_id, complete=False, detail=_first_line(unknown)
        )

    kinds = frozenset(call.outcome.kind for call in scan.calls)
    if not scan.complete:
        failed = scan.failed_dates
        return CentreReading(
            centre_id=centre_id,
            complete=False,
            detail=failed[0].error if failed else (scan.stopped or "incomplete"),
            dates=len(scan.dates),
            needs_auth_recovery=bool(kinds & AUTH_RECOVERY_KINDS),
            kinds=kinds,
        )

    return CentreReading(
        centre_id=centre_id,
        complete=True,
        state=state_of(scan),
        dates=len(scan.dates),
        kinds=kinds,
    )


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CentreDiff:
    """What appeared and what went away for one centre, since the last complete read."""

    centre_id: str
    added: tuple[tuple[date, TimeSlot], ...] = ()
    removed: tuple[tuple[date, TimeSlot], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def diff_states(centre_id: str, previous: CentreState, current: CentreState) -> CentreDiff:
    added = tuple(
        (day, slot) for (day, _start), slot in current.items() if (day, _start) not in previous
    )
    removed = tuple(
        (day, slot) for (day, _start), slot in previous.items() if (day, _start) not in current
    )
    return CentreDiff(
        centre_id=centre_id,
        added=tuple(sorted(added, key=lambda pair: (pair[0], pair[1].time))),
        removed=tuple(sorted(removed, key=lambda pair: (pair[0], pair[1].time))),
    )


@dataclass(frozen=True, slots=True)
class CycleReport:
    """One cycle, ready to print."""

    readings: tuple[CentreReading, ...]
    diffs: tuple[CentreDiff, ...] = ()
    baseline: bool = False
    recovered: bool = False
    failed: str = ""
    #: The database refused a write. Says nothing about the HSC session, which
    #: is why it is a note rather than a failure.
    degraded: bool = False

    @property
    def added(self) -> tuple[CentreDiff, ...]:
        return tuple(diff for diff in self.diffs if diff.added)

    @property
    def removed(self) -> tuple[CentreDiff, ...]:
        return tuple(diff for diff in self.diffs if diff.removed)

    @property
    def changed(self) -> bool:
        return any(diff.changed for diff in self.diffs)

    @property
    def partial(self) -> tuple[CentreReading, ...]:
        return tuple(reading for reading in self.readings if not reading.complete)


def _blocks(diffs: Iterable[CentreDiff], sign: str) -> list[str]:
    """centre -> date -> ``+ 08:26-08:52``, grouped so a day is named once."""
    lines: list[str] = []
    for diff in diffs:
        lines.append(f"  {diff.centre_id}")
        current_day: date | None = None
        for day, slot in diff.added if sign == "+" else diff.removed:
            if day != current_day:
                lines.append(f"    {day.isoformat()}")
                current_day = day
            lines.append(f"      {sign} {slot.display_range}")
    lines.append("")
    return lines


def render_cycle(report: CycleReport, moment: datetime, *, retained: Sequence[str] = ()) -> str:
    """The console block for one cycle. Compact unless something changed."""
    stamp = moment.strftime("%H:%M:%S")

    if report.failed:
        lines = [f"{stamp} CYCLE FAILED — {report.failed}"]
        lines += [f"  {reading.centre_id}: {reading.short()}" for reading in report.readings]
        if retained:
            lines.append("  previous availability retained")
        return "\n".join(lines)

    # Partial centres are listed once, below, with their retained state — so the
    # summary lines here cover the centres that were actually read.
    complete = [reading for reading in report.readings if reading.complete]

    if report.baseline:
        lines = [f"{stamp} BASELINE"]
        lines += [f"  {reading.centre_id}: {reading.summary()}" for reading in complete]
    elif report.changed:
        added, removed = report.added, report.removed
        if added and not removed:
            lines = [f"{stamp} NEW AVAILABILITY", "", *_blocks(added, "+")]
        elif removed and not added:
            lines = [f"{stamp} AVAILABILITY REMOVED", "", *_blocks(removed, "-")]
        else:
            lines = [
                f"{stamp} CHANGES",
                "",
                "NEW AVAILABILITY",
                "",
                *_blocks(added, "+"),
                "AVAILABILITY REMOVED",
                "",
                *_blocks(removed, "-"),
            ]
    elif complete:
        lines = [f"{stamp} no changes"]
        lines += [f"  {reading.centre_id}: {reading.short()}" for reading in complete]
    else:
        # Nothing was read completely, so "no changes" would claim more than
        # this cycle knows.
        lines = [f"{stamp} no complete read"]

    for reading in report.partial:
        lines.append(f"  {reading.centre_id}: {reading.short()}")
        if reading.centre_id in retained:
            lines.append("  previous availability retained")
    if report.recovered:
        lines.append("  (session was re-established once during this cycle)")
    if report.degraded:
        lines.append("  (session persistence is degraded — the HSC session is unaffected)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The monitor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ApiSession:
    """An HTTP client, and the browser work that produced it.

    The client holds a ``requests.Session`` and nothing else — no page, no
    context, no browser — which is what lets Chromium be closed before polling
    even starts.
    """

    client: HscApiClient
    cookies: tuple[CookieInfo, ...] = ()
    bootstrap: QueueBootstrap | None = None


class ApiSessionProvider(Protocol):
    """How the monitor gets — and re-gets — an authenticated API session.

    ``create_api_session`` opens, uses and closes the browser *inside* itself,
    so the monitor never holds a Playwright object and cannot keep one alive by
    accident. ``restore_api_session`` builds the same thing from a jar that was
    persisted earlier, and opens nothing at all.
    """

    async def create_api_session(self) -> ApiSession: ...

    def restore_api_session(self, persisted: PersistedSession) -> ApiSession: ...


class ApiMonitor:
    """Polls the API for a few centres and prints what changed."""

    def __init__(
        self,
        provider: ApiSessionProvider,
        centre_ids: Sequence[str],
        *,
        interval: float,
        slot_interval: float,
        max_dates: int = 0,
        store: SessionStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        slot_sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
        emit: Callable[[str], None] = print,
    ) -> None:
        self.provider = provider
        #: Where the jar sleeps between runs. Without MongoDB configured this is
        #: a store that remembers nothing, so the monitor behaves exactly as it
        #: did before persistence existed.
        self._store: SessionStore = store if store is not None else NullSessionStore()
        self.centre_ids = tuple(centre_ids)
        self.interval = float(interval)
        self.slot_interval = float(slot_interval)
        self.max_dates = max_dates
        self._sleep = sleep
        self._clock = clock
        self._slot_sleep = slot_sleep
        self._now = now
        self._emit = emit

        self.client: HscApiClient | None = None
        self._state: dict[str, CentreState] = {}
        self._cycles = 0
        self._recoveries = 0
        #: Owns "has the jar changed, and may it be written" for this monitor.
        self._persister = SessionPersister(self._store)

    @property
    def store(self) -> SessionStore:
        return self._store

    @store.setter
    def store(self, store: SessionStore) -> None:
        """Kept in step with the persister, so the two can never disagree."""
        self._store = store
        self._persister.store = store

    # ------------------------------------------------------------- session --

    async def start(self) -> None:
        """Pick up a persisted session if there is a usable one; else bootstrap.

        A stored jar is *tried*, never trusted: the first ordinary departments
        call is the validation, and if it comes back 403 the normal one-time
        recovery replaces it. The only thing the stored expiry buys is skipping
        an attempt that is already known to be pointless.
        """
        restored = await asyncio.to_thread(self._load_persisted)
        if restored is not None:
            self._persister.adopt(
                created_at=restored.created_at,
                fingerprint=jar_fingerprint(restored.cookies),
            )
            self._adopt(self.provider.restore_api_session(restored), persist=False)
            logger.info("Loaded persisted HSC session updated %s ago", restored.age())
            return

        session = await self.provider.create_api_session()
        self._adopt(session)

    def _load_persisted(self) -> PersistedSession | None:
        """Blocking. Any failure here is a reason to open a browser, not to stop."""
        try:
            stored = self.store.load()
        except HscMonitorError as exc:
            logger.warning(
                "Could not read the stored HSC session (%s); starting a fresh one",
                _first_line(exc),
            )
            return None

        if stored is None:
            return None
        if stored.expired():
            logger.info("Persisted session expired; browser bootstrap required")
            with suppress(HscMonitorError):
                self.store.delete()
            return None
        return stored

    def _adopt(self, session: ApiSession, *, persist: bool = True) -> None:
        self.client = session.client
        # Every response is a chance for HSC to hand back a new queue cookie.
        self.client.on_response = self._on_response
        if session.bootstrap is not None:
            # Fingerprints are worth a line at startup and at recovery. Never
            # per cycle.
            self._emit("\n".join(session.bootstrap.render()))
        logger.info(
            "API session ready with %d HSC cookies: %s",
            len(session.cookies),
            ", ".join(info.name for info in session.cookies),
        )
        if persist:
            # A new jar: whatever was stored is stale, so this always writes.
            self._persister.adopt()
            self._persister(session.client.session)

    # ---------------------------------------------------------- persistence --

    @property
    def _on_response(self) -> SessionPersister:
        """The hook the client calls after every response."""
        return self._persister

    @property
    def persistence_degraded(self) -> bool:
        """True when the database refused a write. The HSC session is unaffected."""
        return self._persister.degraded

    async def _recover(self) -> bool:
        """One controlled re-authentication, for a 403 and nothing else.

        The refused session is dropped from the database *and* closed before a
        new one is built, so nothing — this process or the next — can pick those
        cookies up again.
        """
        logger.warning("HTTP 403 received; rebuilding authenticated HSC API session")
        with suppress(HscMonitorError):
            self.store.delete()
        self._persister.adopt()
        self.close()

        try:
            session = await self.provider.create_api_session()
        except HscMonitorError as exc:
            logger.warning("Recovery failed: %s", _first_line(exc))
            return False
        self._adopt(session)
        self._recoveries += 1
        return True

    def close(self) -> None:
        """Drop the HTTP session and the store. The browser is already closed."""
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # pragma: no cover - best effort teardown
                logger.debug("Could not close the API session")
            self.client = None
        try:
            self.store.close()
        except Exception:  # pragma: no cover - best effort teardown
            logger.debug("Could not close the session store")

    # --------------------------------------------------------------- cycle --

    async def _read(self) -> list[CentreReading]:
        if self.client is None:  # pragma: no cover - start() always runs first
            raise HscMonitorError("The API monitor was run before start()")
        client = self.client
        # Blocking and strictly sequential; one thread, so scans cannot overlap.
        return await asyncio.to_thread(
            read_centres,
            client,
            self.centre_ids,
            max_dates=self.max_dates,
            pacer=Pacer(self.slot_interval, sleep=self._slot_sleep, clock=self._clock),
        )

    async def cycle(self) -> CycleReport:
        """One complete pass over every centre, with at most one recovery."""
        readings = await self._read()
        recovered = False

        if any(reading.needs_auth_recovery for reading in readings):
            if await self._recover():
                readings = await self._read()
                recovered = True
            else:
                return CycleReport(
                    readings=tuple(readings),
                    failed="session recovery failed",
                    recovered=False,
                )

        baseline = self._cycles == 0
        diffs = self._apply(readings)
        self._cycles += 1
        return CycleReport(
            readings=tuple(readings),
            diffs=tuple(diffs),
            baseline=baseline,
            recovered=recovered,
            degraded=self.persistence_degraded,
        )

    def _apply(self, readings: Sequence[CentreReading]) -> list[CentreDiff]:
        """Diff and remember — but only for centres that were read completely."""
        diffs: list[CentreDiff] = []
        for reading in readings:
            if not reading.complete:
                # The rule this monitor is built around: an incomplete read is
                # never evidence that anything went away.
                continue
            previous = self._state.get(reading.centre_id)
            self._state[reading.centre_id] = reading.state
            if previous is None:
                continue  # first sight of this centre: a baseline, not a change
            diff = diff_states(reading.centre_id, previous, reading.state)
            if diff.changed:
                diffs.append(diff)
        return diffs

    def retained(self) -> list[str]:
        """Centres whose remembered availability is still standing."""
        return [centre for centre in self.centre_ids if centre in self._state]

    # ---------------------------------------------------------------- loop --

    async def run(self, *, once: bool = False) -> None:
        """Poll until interrupted. One scan at a time, paced from scan start."""
        await self.start()
        try:
            while True:
                started = self._clock()
                report = await self.cycle()
                self._emit(render_cycle(report, self._now(), retained=self.retained()))
                if once:
                    return
                # Measured between scan *starts*: a slow scan shortens the wait
                # rather than pushing the schedule out, and never overlaps.
                remaining = self.interval - (self._clock() - started)
                if remaining > 0:
                    await self._sleep(remaining)
        finally:
            self.close()

    @property
    def cycles(self) -> int:
        return self._cycles

    @property
    def recoveries(self) -> int:
        return self._recoveries

    @property
    def state(self) -> Mapping[str, CentreState]:
        return dict(self._state)
