"""A small read-only client for the measured HSC JSON API.

Three endpoints, all measured from the real frontend, all GET::

    /api/v2/equeue/departments ?serviceId
    /api/v2/equeue/days        ?serviceId &departmentId
    /api/v2/equeue/slots       ?serviceId &departmentId &date

There is deliberately no ``post``, ``put``, ``patch`` or ``delete`` on this
class and no helper anywhere in the package that could build one: reading
availability never needs a mutating verb, and a client that cannot express one
cannot be talked into booking an appointment.

The session is supplied, not created per call, and the *same* object is used for
the whole sequence — the site rewrites ``__Host-next.equeue-session`` as the
wizard state advances, and each response's cookies have to be carried into the
next request for the sequence to mean anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final
from urllib.parse import urlencode

import requests

from ..models import ApiProbeError
from .probe import (
    API_ORIGIN,
    DEFAULT_TIMEOUT,
    KIND_RATE_LIMITED,
    WIZARD_COOKIE,
    CookieInfo,
    CookieSource,
    Fetch,
    ProbeOutcome,
    build_session,
    describe_cookies,
    display_url,
    hsc_cookies,
    http_get,
    perform,
    read_browser_cookies,
    read_user_agent,
)
from .retry import RetryConfig, is_retryable, wait_for

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The one measured service
# --------------------------------------------------------------------------- #
#
# HSC identifies "which queue" with a numeric serviceId. Exactly one has been
# measured, so exactly one is named here. Nothing derives, increments or guesses
# another: an unmeasured serviceId would silently query a different queue and
# report its availability as if it were ours.

#: Practical exam, on a service centre vehicle, category A — serviceId 47,
#: measured from the frontend's own ``departments`` call.
PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A: Final[int] = 47

#: The service every request in this package uses, in one place.
DEFAULT_SERVICE_ID: Final[int] = PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A

DEPARTMENTS_PATH: Final = "/api/v2/equeue/departments"
DAYS_PATH: Final = "/api/v2/equeue/days"
SLOTS_PATH: Final = "/api/v2/equeue/slots"

#: Labels used in reports and in the cookie-state table.
LABEL_DEPARTMENTS = "departments"
LABEL_DAYS = "days"


def slots_label(day: date) -> str:
    return f"slots {day.isoformat()}"


def slot_date_param(day: date) -> str:
    """Local midnight, exactly as measured: ``2026-08-26T00:00:00``.

    No timezone suffix and no offset — the observed requests carry neither, and
    adding one would be inventing a parameter the API was never seen to take.
    """
    return f"{day.isoformat()}T00:00:00"


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ApiCall:
    """One completed request: what it was, and what came back.

    ``outcome`` carries the status, the parsed payload and the cookie
    fingerprint diff — never a cookie value, and never the raw body.
    """

    label: str
    url: str
    outcome: ProbeOutcome
    #: Physical requests made, including retries. One unless something was
    #: retryable.
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.outcome.ok

    @property
    def target(self) -> str:
        return display_url(self.url)

    @property
    def session_cookie_changed(self) -> bool:
        change = self.outcome.change_for(WIZARD_COOKIE)
        return change is not None and change.state != "unchanged"

    def cookie_state(self) -> str:
        """``equeue-session changed: yes|no|absent`` — fingerprints only."""
        change = self.outcome.change_for(WIZARD_COOKIE)
        if change is None:
            return "equeue-session changed: absent"
        moved = "yes" if change.state != "unchanged" else "no"
        detail = (
            f" ({change.before} -> {change.after})"
            if change.state == "changed"
            else f" ({change.state})"
            if change.state != "unchanged"
            else ""
        )
        return f"equeue-session changed: {moved}{detail}"


class ApiRequestFailed(ApiProbeError):
    """A call did not return JSON. Carries the outcome so it can be reported.

    Deliberately terminal: a 403 or a 429 ends the run here rather than starting
    a retry, a backoff loop or a second identity.
    """

    def __init__(self, call: ApiCall) -> None:
        super().__init__(
            f"{call.label}: {call.target} did not return JSON "
            f"({call.outcome.kind}). {call.outcome.verdict}"
        )
        self.call = call


class HscApiClient:
    """The measured endpoints, and nothing else.

    Holds a ``requests.Session`` rather than making one, because the whole
    sequence has to share a cookie jar.
    """

    def __init__(
        self,
        session: requests.Session,
        *,
        base_url: str = API_ORIGIN,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        retry: RetryConfig | None = None,
        service_id: int = DEFAULT_SERVICE_ID,
        fetch: Fetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: The only retry policy in the project. Nothing above or below this
        #: class may add another: two layers multiply into a burst.
        self.retry = retry if retry is not None else RetryConfig()
        self.service_id = service_id
        self._fetch: Fetch = fetch if fetch is not None else http_get
        self._sleep = sleep
        #: Called with the session after every response, so an owner can notice
        #: that HSC rewrote the jar. The client itself does not know or care
        #: what that owner does about it — persistence lives elsewhere.
        self.on_response: Callable[[requests.Session], None] | None = None

    # ------------------------------------------------------------ endpoints --

    def departments(self) -> ApiCall:
        return self._get(LABEL_DEPARTMENTS, DEPARTMENTS_PATH, {})

    def days(self, department_id: int) -> ApiCall:
        return self._get(LABEL_DAYS, DAYS_PATH, {"departmentId": department_id})

    def slots(self, department_id: int, day: date) -> ApiCall:
        return self._get(
            slots_label(day),
            SLOTS_PATH,
            {"departmentId": department_id, "date": slot_date_param(day)},
        )

    # -------------------------------------------------------------- request --

    def _params(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        """Every request's query string, built in exactly one place.

        ``serviceId`` is added here and nowhere else, so no endpoint can drift
        onto a different queue.
        """
        return {"serviceId": self.service_id, **extra}

    def _get(self, label: str, path: str, extra: Mapping[str, Any]) -> ApiCall:
        """Every request goes through here, and so does the only retry there is.

        A transient answer — 429, 500, 502, 503, 504 — or no answer at all (a
        timeout, a dropped connection) is asked again, up to
        ``retry.max_attempts`` in total, waiting a widening amount in between. A
        429 that carries a usable ``Retry-After`` is waited out for as long as
        the server asked, up to the configured cap.

        Everything else is final on the first attempt: a 401, a 403 or a body
        that is not the JSON it claimed to be will say exactly the same thing
        the second time, and the attempt would cost the server for nothing.
        """
        url = f"{self.base_url}{path}?{urlencode(self._params(extra))}"

        attempts = 1
        outcome = self._perform(url, attempt=attempts)
        for attempt in range(1, self.retry.max_attempts):
            if not is_retryable(outcome):
                break
            delay = wait_for(outcome, attempt, self.retry)
            logger.info(
                "%s; retry %d/%d in %.1fs",
                "rate limited"
                if outcome.kind == KIND_RATE_LIMITED
                else "transient failure",
                attempt + 1,
                self.retry.max_attempts,
                delay,
            )
            self._sleep(delay)
            attempts = attempt + 1
            outcome = self._perform(url, attempt=attempts)

        return ApiCall(label=label, url=url, outcome=outcome, attempts=attempts)

    def _perform(self, url: str, *, attempt: int) -> ProbeOutcome:
        """One request, timed and logged. Never a body, a header or a cookie."""
        started = time.monotonic()
        outcome = perform(self.session, url, fetch=self._fetch, timeout=self.timeout)
        logger.info(
            "GET %s -> %s (%.1fs)%s",
            display_url(url),
            outcome.status if outcome.status is not None else outcome.kind,
            time.monotonic() - started,
            f" [attempt {attempt}]" if attempt > 1 else "",
        )
        if self.on_response is not None:
            # HSC rewrites the queue cookie on its responses — including the
            # ones that failed — so this is the moment the jar may have changed.
            self.on_response(self.session)
        return outcome

    def require(self, call: ApiCall) -> ApiCall:
        """Return the call, or raise if it did not produce JSON."""
        if not call.ok:
            raise ApiRequestFailed(call)
        return call

    def close(self) -> None:
        """Drop the connection pool. The cookies go with it — nothing persists."""
        self.session.close()


# --------------------------------------------------------------------------- #
# The bridge from the authenticated browser
# --------------------------------------------------------------------------- #


async def client_for(
    page: CookieSource,
    *,
    base_url: str = API_ORIGIN,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry: RetryConfig | None = None,
    service_id: int = DEFAULT_SERVICE_ID,
    fetch: Fetch | None = None,
) -> tuple[HscApiClient, list[CookieInfo]]:
    """Build a client from the live BrowserContext's HSC cookies.

    Returns the client and the *safe* description of what was copied: names,
    scopes and fingerprints. Values exist only inside the session's cookie jar —
    they are never returned, logged, printed or written to disk, and nothing
    ever copies them back into Playwright.
    """
    cookies = hsc_cookies(await read_browser_cookies(page))
    if not cookies:
        raise ApiProbeError(
            "No hsc.gov.ua cookies were found in the browser context, so the "
            "authenticated session cannot be bridged to an HTTP client."
        )

    session = build_session(cookies, user_agent=await read_user_agent(page))
    return (
        HscApiClient(
            session,
            base_url=base_url,
            timeout=timeout,
            retry=retry,
            service_id=service_id,
            fetch=fetch,
        ),
        describe_cookies(cookies),
    )
