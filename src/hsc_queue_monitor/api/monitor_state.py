"""What the monitor knows about itself, between runs.

Every scheduled run is a fresh process. Without somewhere to write down what the
last one found, each run repeats the last one's mistake: asking a server that
refused us, with a session it has already rejected, five minutes later, forever.

So there is a second document — deliberately *not* inside the encrypted session
document, because these two things have different lifetimes, different secrecy
and different owners. This one holds no cookie, no token and no response body:
a status, a short reason, and three timestamps.

The states, and what each is for:

``READY``
    Normal. Monitoring may run.

``AUTH_REQUIRED``
    **Sticky.** HSC rejected the persisted session outright (401 or 403). No
    scheduled run may send another authenticated request until a human runs
    ``refresh-session`` locally and it *succeeds* — which is the only thing that
    clears this.

``RATE_LIMITED``
    Temporary. A 429 outlived the retry budget. Carries ``retry_after_at``, and
    runs before that moment skip the API entirely.

``SERVICE_UNAVAILABLE``
    Temporary. 5xx, timeouts or transport failures outlived the retry budget.
    The next run simply tries again.

The one rule worth stating out loud: **exhausted retries are never an
authentication problem.** An outage is not an expired login, and treating it as
one would pause monitoring — and, later, alert a person — for something that
would have fixed itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from ..models import HscMonitorError
from .session_store import SessionStoreError, safe_detail

logger = logging.getLogger(__name__)

#: One document, beside the session and never inside it.
STATE_DOCUMENT_ID: Final = "hsc-monitor-state"
STATE_SCHEMA_VERSION: Final = 1


class MonitorStatus(StrEnum):
    """Where the monitor stands. Stored as its value, so it reads plainly."""

    READY = "READY"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


#: States that stop a scheduled run before it sends anything.
BLOCKING_STATUSES: Final[frozenset[MonitorStatus]] = frozenset(
    {MonitorStatus.AUTH_REQUIRED, MonitorStatus.RATE_LIMITED}
)


@dataclass(frozen=True, slots=True)
class MonitorState:
    """The whole document, minus the bookkeeping."""

    status: MonitorStatus = MonitorStatus.READY
    reason: str = ""
    updated_at: datetime | None = None
    last_success_at: datetime | None = None
    retry_after_at: datetime | None = None

    @property
    def auth_required(self) -> bool:
        return self.status is MonitorStatus.AUTH_REQUIRED

    def waiting(self, *, now: datetime | None = None) -> bool:
        """Whether a rate-limit window is still open.

        Only ``RATE_LIMITED`` waits, and only while it has a future moment to
        wait for: a window with no end is not a window, and a run that cannot
        tell when to resume should resume.
        """
        if self.status is not MonitorStatus.RATE_LIMITED or self.retry_after_at is None:
            return False
        return self.retry_after_at > (now or datetime.now(UTC))

    def blocks(self, *, now: datetime | None = None) -> bool:
        """Whether this state forbids sending anything to HSC right now."""
        return self.auth_required or self.waiting(now=now)


@dataclass(frozen=True, slots=True)
class MonitorStateTransition:
    """One change of state, as an event.

    Exists so the notifier this project will grow can subscribe to *changes*
    rather than to every run: a second consecutive AUTH_REQUIRED is not news,
    and :attr:`changed` is what will keep it from being sent as though it were.
    """

    previous: MonitorStatus | None
    current: MonitorStatus
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.previous is not self.current

    @property
    def recovered(self) -> bool:
        return self.changed and self.current is MonitorStatus.READY

    def describe(self) -> str:
        was = self.previous.value if self.previous is not None else "unknown"
        detail = f" — {self.reason}" if self.reason else ""
        return f"{was} -> {self.current.value}{detail}"


class MonitorStateStore(Protocol):
    """Where the monitor's own state lives between runs."""

    def load(self) -> MonitorState | None: ...

    def save(self, state: MonitorState) -> None: ...

    def close(self) -> None: ...


class NullMonitorStateStore:
    """No persistence: every run starts believing it is READY."""

    def __init__(self) -> None:
        self.saved: list[MonitorState] = []

    def load(self) -> MonitorState | None:
        return None

    def save(self, state: MonitorState) -> None:
        self.saved.append(state)

    def close(self) -> None:
        return None


class MongoMonitorStateStore:
    """One small document, replaced whole on every write.

    Takes a *collection* rather than a URI, so it shares the connection the
    session store already opened: two documents, one client, one place that
    knows the credentials.
    """

    def __init__(
        self,
        collection: Any,
        *,
        document_id: str = STATE_DOCUMENT_ID,
        now: Any = None,
    ) -> None:
        self._collection = collection
        self.document_id = document_id
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def load(self) -> MonitorState | None:
        try:
            document = self._collection.find_one({"_id": self.document_id})
        except Exception as exc:
            raise SessionStoreError(
                f"Could not read the monitor state: {safe_detail(exc)}"
            ) from exc

        if not document:
            return None
        if document.get("version") != STATE_SCHEMA_VERSION:
            logger.warning(
                "Ignoring monitor state written by schema version %r",
                document.get("version"),
            )
            return None

        try:
            status = MonitorStatus(str(document.get("status", "")))
        except ValueError:
            logger.warning("Ignoring monitor state with unknown status %r", document.get("status"))
            return None

        return MonitorState(
            status=status,
            reason=str(document.get("reason") or ""),
            updated_at=_as_utc(document.get("updated_at")),
            last_success_at=_as_utc(document.get("last_success_at")),
            retry_after_at=_as_utc(document.get("retry_after_at")),
        )

    def save(self, state: MonitorState) -> None:
        document = {
            "_id": self.document_id,
            "version": STATE_SCHEMA_VERSION,
            "status": state.status.value,
            "reason": state.reason,
            "updated_at": self._now(),
            "last_success_at": state.last_success_at,
            "retry_after_at": state.retry_after_at,
        }
        try:
            self._collection.replace_one(
                {"_id": self.document_id}, document, upsert=True
            )
        except Exception as exc:
            raise SessionStoreError(
                f"Could not persist the monitor state: {safe_detail(exc)}"
            ) from exc

    def close(self) -> None:
        """The session store owns the client, so there is nothing to close."""
        return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def transition_to(
    previous: MonitorState | None,
    status: MonitorStatus,
    *,
    reason: str = "",
    retry_after_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[MonitorState, MonitorStateTransition]:
    """The next state, and the event describing how it was reached."""
    moment = now or datetime.now(UTC)
    was = previous.status if previous is not None else None
    succeeded = status is MonitorStatus.READY

    state = MonitorState(
        status=status,
        reason="" if succeeded else reason,
        updated_at=moment,
        last_success_at=moment if succeeded else (previous.last_success_at if previous else None),
        retry_after_at=None if succeeded else retry_after_at,
    )
    return state, MonitorStateTransition(previous=was, current=status, reason=reason)


def record(
    store: MonitorStateStore,
    previous: MonitorState | None,
    status: MonitorStatus,
    *,
    reason: str = "",
    retry_after_at: datetime | None = None,
    now: datetime | None = None,
) -> MonitorStateTransition | None:
    """Work out the next state, write it, and return the event — if it landed.

    ``None`` when the write failed, and that is the whole point: the persisted
    document is the source of truth, so a state that was not written did not
    happen. Returning an event anyway would tell the caller — and, later, a
    notifier — that something changed when the next run will plainly see that it
    did not.

    The caller decides what a failure means. A completed scan treats it as a
    lost note; ``refresh-session`` treats it as an incomplete refresh, because
    an uncleared AUTH_REQUIRED keeps monitoring paused.
    """
    state, event = transition_to(
        previous, status, reason=reason, retry_after_at=retry_after_at, now=now
    )
    try:
        store.save(state)
    except HscMonitorError as exc:
        logger.warning("Could not persist monitor state %s: %s", status.value, exc)
        return None

    if event.changed:
        logger.info("Monitor state: %s", event.describe())
    return event


def refreshed(previous: MonitorState | None, *, now: datetime | None = None) -> MonitorState:
    """The state a successful local ``refresh-session`` leaves behind."""
    return replace(
        previous or MonitorState(),
        status=MonitorStatus.READY,
        reason="",
        retry_after_at=None,
        updated_at=now or datetime.now(UTC),
    )


