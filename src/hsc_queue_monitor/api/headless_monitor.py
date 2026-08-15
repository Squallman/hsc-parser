"""One availability scan over HTTP, with no browser anywhere in reach.

This is the path GitHub Actions runs. It loads the encrypted session another
process put in MongoDB, reads departments -> days -> slots once, writes the
refreshed jar back, prints what it found and exits. The schedule provides the
repetition; there is no loop in here.

The separation is the point, and it is structural rather than a matter of
discipline:

* this module imports :class:`~.client.HscApiClient`, the session store, the
  availability model and configuration — and nothing that can open a browser.
  ``playwright``, ``BrowserManager``, ``BrowserSessionProvider``, ``AuthManager``,
  ``QueuePage``, ``LoginPage`` and the native file selector are unreachable from
  here, and a test walks the import graph to keep it that way;
* there is therefore no fallback. When the stored session is missing, expired or
  refused — with 401 *or* 403, both measured — this run *stops and says so* and
  exits 3. It cannot authenticate, and pretending otherwise would be the one
  thing that turns a scheduled job into a way to leak a signing key onto a
  runner.

Recovering is a local act: ``refresh-session`` opens the browser on a machine
that has the MasterKey, writes a new session to MongoDB, and the next scheduled
run picks it up on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from ..models import HscMonitorError, TimeSlot
from .availability import Pacer
from .availability_snapshot import (
    AvailabilityDiff,
    AvailabilitySnapshotStore,
    NullAvailabilitySnapshotStore,
    diff_snapshots,
    render_diff,
    snapshot_of,
)
from .client import HscApiClient
from .monitor import CentreReading, read_centres
from .monitor_state import (
    MonitorState,
    MonitorStateStore,
    MonitorStateTransition,
    MonitorStatus,
    NullMonitorStateStore,
    record,
)
from .probe import (
    DEFAULT_TIMEOUT,
    KIND_FORBIDDEN,
    KIND_NETWORK_ERROR,
    KIND_RATE_LIMITED,
    KIND_TIMEOUT,
    KIND_UNAUTHORIZED,
    Fetch,
)
from .retry import RetryConfig
from .session_store import (
    PersistedSession,
    SessionPersister,
    SessionStore,
    jar_fingerprint,
    session_from_cookies,
)

logger = logging.getLogger(__name__)

#: Exit codes for the headless path. 0 and 2 keep the meanings the rest of the
#: CLI already gives them; 3 and 4 are new, and exist so a scheduled run can be
#: told apart from a configuration mistake by anything reading exit codes.
EXIT_OK: Final = 0
EXIT_CONFIG: Final = 2
#: The stored session is missing, expired or was refused. A human must run
#: ``refresh-session`` locally; no runner can fix this.
EXIT_AUTH_REQUIRED: Final = 3
#: The database could not be read at all, so there was nothing to try.
EXIT_PERSISTENCE: Final = 4
#: HSC was unwell — 5xx, timeouts, transport failures — past the retry budget.
#: Temporary, and explicitly *not* an authentication problem.
EXIT_SERVICE_UNAVAILABLE: Final = 6
#: A 429 outlived the retry budget, or a rate-limit window is still open.
EXIT_RATE_LIMITED: Final = 7

#: Which exit code each terminal state produces.
EXIT_FOR_STATUS: Final[dict[MonitorStatus, int]] = {
    MonitorStatus.READY: EXIT_OK,
    MonitorStatus.AUTH_REQUIRED: EXIT_AUTH_REQUIRED,
    MonitorStatus.RATE_LIMITED: EXIT_RATE_LIMITED,
    MonitorStatus.SERVICE_UNAVAILABLE: EXIT_SERVICE_UNAVAILABLE,
}

#: Failures that mean the service, not the session. Kept apart from the auth set
#: on purpose: an outage is not an expired login, and calling it one would pause
#: monitoring for something that fixes itself.
SERVICE_KINDS: Final[frozenset[str]] = frozenset({KIND_TIMEOUT, KIND_NETWORK_ERROR})

#: What a scheduled run cannot recover from, and must stop over.
#:
#: Measured: a persisted session about eleven minutes old was answered with
#: ``401 Unauthorized``, not 403 — so treating only 403 as "refresh me" would
#: have this job report PARTIAL every five minutes forever while never saying
#: the one thing a human needed to hear.
#:
#: Deliberately *not* the same set as the local monitor's
#: :data:`~.monitor.AUTH_RECOVERY_KINDS`. There, the set decides whether to open
#: a browser, and a 401 does not earn one. Here, nothing opens: the set decides
#: only whether this run exits 3 and asks for a local refresh. Different
#: question, different answer, so different set.
AUTH_REQUIRED_KINDS: Final[frozenset[str]] = frozenset(
    {KIND_UNAUTHORIZED, KIND_FORBIDDEN}
)

STATUS_OK = "OK"
STATUS_BOOKABLE = "BOOKABLE"
STATUS_PARTIAL = "PARTIAL"

REFRESH_COMMAND = "python -m hsc_queue_monitor.cli refresh-session"


def _auth_required(reason: str, *, detail: str = "") -> str:
    return "\n".join(
        [
            "",
            "AUTH REQUIRED",
            "",
            reason,
            *([detail] if detail else []),
            "Run locally:",
            "",
            f"  {REFRESH_COMMAND}",
            "",
            "That opens the browser on a machine that has the MasterKey, writes a",
            "fresh session to MongoDB, and the next scheduled run uses it.",
            "",
        ]
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _slots_by_date(reading: CentreReading) -> dict[date, list[TimeSlot]]:
    grouped: dict[date, list[TimeSlot]] = {}
    for (day, _start), slot in sorted(reading.state.items()):
        grouped.setdefault(day, []).append(slot)
    return grouped


def status_of(readings: Sequence[CentreReading]) -> str:
    """``BOOKABLE`` beats ``PARTIAL`` beats ``OK``.

    An incomplete read is never allowed to look like a clean empty one, which is
    why ``PARTIAL`` exists at all.
    """
    if any(reading.slot_count for reading in readings):
        return STATUS_BOOKABLE
    if any(not reading.complete for reading in readings):
        return STATUS_PARTIAL
    return STATUS_OK


def render_check(readings: Sequence[CentreReading]) -> str:
    """The whole report for one scan."""
    lines = ["", "HSC AVAILABILITY CHECK", ""]
    for reading in readings:
        if not reading.complete:
            lines.append(f"{reading.centre_id}: partial — {reading.detail}")
            continue
        if not reading.state:
            lines.append(f"{reading.centre_id}: no availability")
            continue

        lines.append(reading.centre_id)
        for day, slots in _slots_by_date(reading).items():
            lines.append(f"  {day.isoformat()}")
            lines += [f"    {slot.display_range}" for slot in slots]
        lines.append("")

    lines += ["", f"Status: {status_of(readings)}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HeadlessScan:
    """What one run produced: an exit code, the readings, and the state change.

    ``transition`` is the seam a notifier will attach to. It is exposed rather
    than acted on, and it carries ``changed`` precisely so a second consecutive
    AUTH_REQUIRED does not become a second message to a person.
    """

    code: int
    readings: tuple[CentreReading, ...] = ()
    transition: MonitorStateTransition | None = None
    #: What changed since the last complete scan. ``None`` when this run had no
    #: business comparing anything — a refusal, a partial read, or a snapshot
    #: that could not be stored.
    availability: AvailabilityDiff | None = None

    @property
    def ok(self) -> bool:
        return self.code == EXIT_OK


def classify(readings: Sequence[CentreReading]) -> tuple[MonitorStatus, str]:
    """What a finished scan means for the monitor's own state.

    The order is a priority order, and each step is a claim about evidence:

    * a 401 or 403 is HSC saying *this session* is not welcome — the only thing
      that can mean AUTH_REQUIRED;
    * a 429 that survived the retries is a rate limit, whatever else happened;
    * anything else incomplete is the service being unwell. That includes an
      unreadable schema and an empty 2xx: neither is an authentication problem,
      and neither is worth declaring success over.

    READY needs every centre read completely. A partial answer is not a smaller
    success — it is an unknown, and calling it READY would let the next run
    believe the last one worked.
    """
    refused = [r for r in readings if r.kinds & AUTH_REQUIRED_KINDS]
    if refused:
        return MonitorStatus.AUTH_REQUIRED, refused[0].detail

    limited = [r for r in readings if KIND_RATE_LIMITED in r.kinds]
    if limited:
        return MonitorStatus.RATE_LIMITED, limited[0].detail

    incomplete = [r for r in readings if not r.complete]
    if incomplete:
        return MonitorStatus.SERVICE_UNAVAILABLE, incomplete[0].detail

    return MonitorStatus.READY, ""


def run_headless_scan(
    store: SessionStore,
    centre_ids: Sequence[str],
    *,
    state_store: MonitorStateStore | None = None,
    snapshots: AvailabilitySnapshotStore | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry: RetryConfig | None = None,
    slot_interval: float = 0.0,
    max_dates: int = 0,
    fetch: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    emit: Callable[[str], None] = print,
) -> HeadlessScan:
    """Check the gate, read once, write back what changed, report.

    The gate comes first, before the session is even decrypted: a state that
    forbids sending anything forbids reading the cookies too, because there is
    nothing to do with them.
    """
    states = state_store if state_store is not None else NullMonitorStateStore()

    try:
        state = states.load()
    except HscMonitorError as exc:
        emit(f"\nPERSISTENCE ERROR\n\n{exc}\n")
        return HeadlessScan(EXIT_PERSISTENCE)

    blocked = _blocked(state, now=now(), emit=emit)
    if blocked is not None:
        return blocked

    try:
        stored = store.load()
    except HscMonitorError as exc:
        emit(f"\nPERSISTENCE ERROR\n\n{exc}\n")
        return HeadlessScan(EXIT_PERSISTENCE)

    if stored is None or stored.expired():
        # No usable session, and nothing here can make one. This is the same
        # conclusion a 401 reaches, so it is recorded the same way — and the
        # next run will not even get this far.
        reason = (
            "No HSC session is stored in MongoDB."
            if stored is None
            else "The stored HSC session expired at "
            f"{stored.queue_session_expires_at:%Y-%m-%d %H:%M:%S %Z}."
        )
        emit(_auth_required(reason))
        event = record(states, state, MonitorStatus.AUTH_REQUIRED, reason=reason, now=now())
        return HeadlessScan(EXIT_AUTH_REQUIRED, transition=event)

    logger.info("Loaded persisted HSC session updated %s ago", stored.age())
    client = _client_for(stored, timeout=timeout, retry=retry, fetch=fetch, sleep=sleep)
    persister = SessionPersister(
        store,
        created_at=stored.created_at,
        fingerprint=jar_fingerprint(stored.cookies),
    )
    # Attached before the first request, so a jar rotated on a *failed* attempt
    # is written back too.
    client.on_response = persister

    try:
        readings = read_centres(
            client,
            centre_ids,
            max_dates=max_dates,
            pacer=Pacer(slot_interval, sleep=sleep, clock=clock),
        )
    finally:
        client.close()

    status, reason = classify(readings)
    moment = now()
    event = record(
        states,
        state,
        status,
        reason=reason,
        retry_after_at=_retry_after_at(status, retry, moment),
        now=moment,
    )

    if status is MonitorStatus.AUTH_REQUIRED:
        # Nothing was retried and nothing is deleted: this process cannot tell a
        # dead session from a momentarily unhappy server, and the stored
        # document is the only evidence either way.
        emit(
            _auth_required(
                "Persisted HSC session is no longer accepted.",
                detail=f"HSC answered: {reason}",
            )
        )
        return HeadlessScan(EXIT_AUTH_REQUIRED, tuple(readings), event)

    if persister.degraded:
        emit(
            "Note: the refreshed session could not be written back. This scan is "
            "unaffected, but the next run may be working from an older jar.\n"
        )

    if status is not MonitorStatus.READY:
        # A state message, not an availability one: the per-centre detail is how
        # a reader tells a rate limit from an outage. The snapshot is untouched,
        # because an incomplete scan is an unknown and not an empty result.
        emit(render_check(readings))
        emit(_service_status(status, reason, retry_after_at=_state_retry_after(states)))
        return HeadlessScan(EXIT_FOR_STATUS[status], tuple(readings), event)

    return _compare(
        snapshots if snapshots is not None else NullAvailabilitySnapshotStore(),
        readings,
        centre_ids,
        event=event,
        now=moment,
        emit=emit,
    )


def _compare(
    snapshots: AvailabilitySnapshotStore,
    readings: Sequence[CentreReading],
    centre_ids: Sequence[str],
    *,
    event: MonitorStateTransition | None,
    now: datetime,
    emit: Callable[[str], None],
) -> HeadlessScan:
    """Diff a complete scan against the last one, persist, then speak.

    The order matters. The snapshot is written *before* the change is announced,
    so a crash between the two loses one message rather than repeating it on
    every run afterwards — at-most-once, chosen deliberately: a missed change
    shows up in the next run's diff, while a repeated one never stops.
    """
    current = snapshot_of(
        {reading.centre_id: _by_date(reading) for reading in readings}, updated_at=now
    )

    try:
        previous = snapshots.load()
    except HscMonitorError as exc:
        emit(_snapshot_failed("read", exc))
        return HeadlessScan(EXIT_PERSISTENCE, tuple(readings), event)

    difference = diff_snapshots(previous, current, centres=centre_ids)

    try:
        snapshots.save(current)
    except HscMonitorError as exc:
        # Nothing is emitted: the next run would emit it again, and a change
        # announced twice is worse than one announced late.
        emit(_snapshot_failed("persist", exc))
        return HeadlessScan(EXIT_PERSISTENCE, tuple(readings), event)

    if difference.baselined:
        logger.info(
            "Baseline established for %s; nothing reported as new",
            ", ".join(difference.baselined),
        )
    if difference.changed:
        emit(render_diff(difference))
    else:
        # Silence on purpose. A run that says "no changes" every five minutes
        # teaches its reader to stop looking.
        logger.info("Availability unchanged (%d slot(s))", current.slot_count)

    return HeadlessScan(EXIT_OK, tuple(readings), event, difference)


def _by_date(reading: CentreReading) -> dict[date, list[TimeSlot]]:
    return _slots_by_date(reading)


def _snapshot_failed(what: str, error: Exception) -> str:
    return (
        f"\nSNAPSHOT ERROR\n\nThe scan succeeded, but the availability snapshot "
        f"could not be {what}:\n{error}\n\nNo availability change was reported, and "
        "the stored snapshot was left as it was.\n"
    )


def _blocked(
    state: MonitorState | None, *, now: datetime, emit: Callable[[str], None]
) -> HeadlessScan | None:
    """The gate: what a persisted state forbids before anything is sent."""
    if state is None:
        return None

    if state.auth_required:
        emit(
            "\nAUTH REQUIRED\n\n"
            "Monitoring is paused because the persisted HSC session requires\n"
            "manual re-authentication.\n"
            + (f"\n{state.reason}\n" if state.reason else "")
            + f"\nRun locally:\n\n  {REFRESH_COMMAND}\n\n"
            "No request was sent, and the stored session was not read.\n"
        )
        # No transition: the state did not change, and a notifier must not be
        # given a second reason to say the same thing.
        return HeadlessScan(EXIT_AUTH_REQUIRED)

    if state.waiting(now=now):
        assert state.retry_after_at is not None  # waiting() implies it
        emit(
            f"\nHSC SERVICE STATUS: {MonitorStatus.RATE_LIMITED.value}\n\n"
            f"{state.reason or 'HSC rate limited an earlier run.'}\n"
            f"Retry after: {state.retry_after_at:%Y-%m-%d %H:%M:%S %Z}\n\n"
            "No request was sent.\n"
        )
        return HeadlessScan(EXIT_RATE_LIMITED)

    return None


def _service_status(
    status: MonitorStatus, reason: str, *, retry_after_at: datetime | None
) -> str:
    lines = [f"HSC SERVICE STATUS: {status.value}", "", reason or "(no detail)"]
    if retry_after_at is not None:
        lines.append(f"Retry after: {retry_after_at:%Y-%m-%d %H:%M:%S %Z}")
    lines += ["Monitoring will retry on the next scheduled run.", ""]
    return "\n".join(lines)


def _retry_after_at(
    status: MonitorStatus, retry: RetryConfig | None, now: datetime
) -> datetime | None:
    """When a rate-limited monitor may try again.

    Only a rate limit gets a window: a service failure is retried by the next
    scheduled run like any other, and inventing a wait for it would delay
    recovery for no reason.
    """
    if status is not MonitorStatus.RATE_LIMITED:
        return None
    config = retry or RetryConfig()
    return now + timedelta(seconds=config.max_retry_after_seconds)


def _state_retry_after(states: MonitorStateStore) -> datetime | None:
    try:
        current = states.load()
    except HscMonitorError:  # pragma: no cover - already reported by record()
        return None
    return current.retry_after_at if current else None


def _client_for(
    stored: PersistedSession,
    *,
    timeout: tuple[float, float],
    retry: RetryConfig | None,
    fetch: Fetch | None,
    sleep: Callable[[float], None],
) -> HscApiClient:
    return HscApiClient(
        session_from_cookies(stored.cookies, user_agent=stored.user_agent),
        timeout=timeout,
        retry=retry,
        fetch=fetch,
        sleep=sleep,
    )


#: Named so the AST test can assert what this path is allowed to touch. Adding a
#: name here is a deliberate act, and adding a browser one would fail the test
#: that reads it.
ALLOWED_DEPENDENCIES: Final[frozenset[str]] = frozenset(
    {
        "..models",
        ".availability",
        ".client",
        ".monitor",
        ".probe",
        ".session_store",
    }
)
