"""The API calls that have actually been measured.

Nothing here is inferred, extrapolated or guessed. An entry earns its place only
after the exact method and URL have been *observed* — from a browser HAR, or
from ``api-probe --observe`` watching the real page — because a guessed URL that
happens to 404 teaches nothing, and a guessed URL that happens to work teaches
something false about what the site expects.

The list starts with one entry. To extend it, run::

    python -m hsc_queue_monitor.cli api-observe

click through category A / centre 3242 / a date by hand, and copy the lines it
prints into :data:`MEASURED_REQUESTS`.

GET only, by construction: :func:`require_read_only` refuses anything else, so a
sequence can never turn into a booking.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ApiProbeError
from .probe import DEFAULT_PATH


@dataclass(frozen=True, slots=True)
class MeasuredRequest:
    """One observed API call: what it is, and where the observation came from."""

    name: str
    path: str
    #: Kept explicit so the read-only guard has something to check, and so a
    #: future measurement of a POST can be *recorded* without being executed.
    method: str = "GET"
    #: Where this was measured. Not decoration — it is what separates this list
    #: from a list of guesses.
    evidence: str = ""

    def describe(self) -> str:
        return f"{self.method} {self.path}"


#: In wizard order. Today: the one call the browser HAR proves.
MEASURED_REQUESTS: tuple[MeasuredRequest, ...] = (
    MeasuredRequest(
        name="departments",
        path=DEFAULT_PATH,
        evidence=(
            "Browser HAR: the frontend issues this on load with "
            "Accept: application/json and Referer: https://eqn.hsc.gov.ua/"
        ),
    ),
    # Next, once measured rather than assumed:
    #   selecting category A
    #   selecting centre 3242
    #   selecting a date
)


def require_read_only(request: MeasuredRequest) -> MeasuredRequest:
    """Refuse anything that could change server state.

    The guard is here rather than at the call site so that adding a measured
    POST to the list above is a safe, non-executing act of documentation.
    """
    if request.method.upper() != "GET":
        raise ApiProbeError(
            f"{request.name} is a {request.method} request. This diagnostic only "
            "ever issues GET: it exists to read state, never to book, reserve or "
            "submit anything."
        )
    return request
