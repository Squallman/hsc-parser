"""The one retry policy, and the one place that decides what is worth retrying.

There is exactly one retry owner in this project — :meth:`HscApiClient._get` —
and this module is what it consults. That is deliberate: two retry layers
multiply, so three attempts at one level and three at another are nine requests
at a server that has already said it is busy. A test asserts there is only one.

What is retried is narrow and explicit. A 429 or a 5xx is the server saying
"not now"; a timeout or a dropped connection is nobody saying anything. Those
are worth asking again, bounded, with a widening gap. A 401, a 403, a 400 or a
body that is not the JSON it claimed to be are *answers* — asking again would
produce the same one, so the attempt budget is spent on nothing.

The backoff is deterministic. A scheduled job that always takes the same shape
is easier to reason about than one that is a little different every time, and
there is no thundering herd here to spread out: one runner, one scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

from .probe import (
    KIND_NETWORK_ERROR,
    KIND_RATE_LIMITED,
    KIND_TIMEOUT,
    ProbeOutcome,
)

logger = logging.getLogger(__name__)

#: Statuses worth asking again about. Everything else is an answer.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: Outcome kinds worth asking again about, where there is no status at all.
RETRYABLE_KINDS: Final[frozenset[str]] = frozenset({KIND_TIMEOUT, KIND_NETWORK_ERROR})

DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_INITIAL_BACKOFF: Final = 2.0
DEFAULT_MAX_BACKOFF: Final = 15.0
DEFAULT_MULTIPLIER: Final = 2.0
#: However long the server asks us to wait, a scheduled job may not sit on it
#: forever — the next run is five minutes away and will try again anyway.
DEFAULT_MAX_RETRY_AFTER: Final = 60.0


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """How many attempts, how far apart. Bounded on every axis."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF
    multiplier: float = DEFAULT_MULTIPLIER
    #: The cap applied to a server's own ``Retry-After``.
    max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER

    def backoff_for(self, attempt: int) -> float:
        """The wait *after* ``attempt``. Deterministic, and capped.

        With the defaults: 2s after the first attempt, 4s after the second, and
        no third wait because there is no fourth attempt.
        """
        grown = self.initial_backoff_seconds * (self.multiplier ** max(attempt - 1, 0))
        return min(grown, self.max_backoff_seconds)


def is_retryable(outcome: ProbeOutcome) -> bool:
    """Whether asking again could plausibly produce a different answer."""
    if outcome.status is None:
        return outcome.kind in RETRYABLE_KINDS
    return outcome.status in RETRYABLE_STATUSES


def read_retry_after(value: str, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds, whether it came as seconds or as a date.

    ``None`` when the header is absent or unusable — the caller then falls back
    to its own backoff rather than guessing what the server meant.
    """
    text = (value or "").strip()
    if not text:
        return None

    try:
        return max(float(int(text)), 0.0)
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # pragma: no cover - RFC dates carry a zone
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def wait_for(
    outcome: ProbeOutcome,
    attempt: int,
    config: RetryConfig,
    *,
    now: datetime | None = None,
) -> float:
    """How long to wait before the next attempt, and why.

    A 429 with a usable ``Retry-After`` is honoured up to the configured cap:
    the server knows its own limits better than this policy does, but a
    scheduled run is not going to sleep through its whole window either.
    """
    if outcome.kind == KIND_RATE_LIMITED:
        asked = read_retry_after(outcome.retry_after, now=now)
        if asked is not None:
            return min(asked, config.max_retry_after_seconds)
    return config.backoff_for(attempt)
