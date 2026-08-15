"""Safe metadata about the ``/api/`` calls the real page makes.

This is how :data:`~.endpoints.MEASURED_REQUESTS` grows without guessing: attach
it, click through the wizard by hand, and read back the exact methods and paths
the site used.

What is recorded, and nothing else:

* method;
* path, with sensitive query *values* redacted (parameter names are kept —
  "there was a code" is exactly what a diagnostic needs to say);
* status;
* content type.

What is never recorded, at any verbosity: request or response bodies, ``Cookie``
or ``Authorization`` headers, any other header, or any token. Only HSC's own API
is watched — ``host == eqn.hsc.gov.ua`` and ``path`` under ``/api/``.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..logging_config import REDACTED, SENSITIVE_QUERY_KEYS
from .probe import API_HOST

logger = logging.getLogger(__name__)

API_PREFIX = "/api/"

#: A manual click-through is short. This is a ring buffer, not a capture file.
MAX_RECORDS = 500


def safe_target(url: str) -> str:
    """``path?query`` with credential-shaped values replaced.

    Parameter *names* survive because they are the discovery: knowing the date
    endpoint takes ``departmentId`` is the finding, and knowing the value is not.
    """
    parts = urlsplit(url)
    path = parts.path or "/"
    if not parts.query:
        return path

    pairs = []
    for pair in parts.query.split("&"):
        key, separator, _value = pair.partition("=")
        if separator and key.lower() in SENSITIVE_QUERY_KEYS:
            pairs.append(f"{key}={REDACTED}")
        else:
            pairs.append(pair)
    return f"{path}?{'&'.join(pairs)}"


@dataclass(frozen=True, slots=True)
class ApiRecord:
    """One observed API response. Metadata only — there is nowhere to put a body."""

    method: str
    target: str
    status: int
    content_type: str

    def describe(self) -> str:
        return f"{self.method} {self.target} -> {self.status} {self.content_type}".rstrip()


class ApiObserver:
    """Watches one page and records HSC ``/api/`` responses.

    Passive by construction: it listens, and it never navigates, clicks or
    changes anything on the page.
    """

    def __init__(
        self,
        page: Any,
        *,
        host: str = API_HOST,
        prefix: str = API_PREFIX,
        on_record: Callable[[ApiRecord], None] | None = None,
        limit: int = MAX_RECORDS,
    ) -> None:
        self.page = page
        self.host = host.lower()
        self.prefix = prefix
        self._on_record = on_record
        self._records: deque[ApiRecord] = deque(maxlen=limit)
        self._started = False

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> None:
        if self._started:
            return
        self.page.on("response", self._on_response)
        self._started = True

    def stop(self) -> None:
        """Detach. Safe to call twice, and safe if the page is already closed."""
        if not self._started:
            return
        self._started = False
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:  # pragma: no cover - page already closed
            logger.debug("Could not detach the response listener")

    # ------------------------------------------------------------- listener --

    def watches(self, url: str) -> bool:
        parts = urlsplit(url)
        return (parts.hostname or "").lower() == self.host and (
            parts.path or ""
        ).startswith(self.prefix)

    def _on_response(self, response: Any) -> None:
        try:
            url = str(getattr(response, "url", ""))
            if not self.watches(url):
                return

            try:
                content_type = str(response.headers.get("content-type", ""))
            except Exception:  # pragma: no cover - headers unavailable
                content_type = "(unavailable)"

            record = ApiRecord(
                method=str(getattr(response.request, "method", "?")),
                target=safe_target(url),
                status=int(getattr(response, "status", 0) or 0),
                content_type=content_type.split(";")[0].strip(),
            )
        except Exception:  # pragma: no cover - never break the page
            logger.debug("Could not record an API response")
            return

        self._records.append(record)
        if self._on_record is not None:
            self._on_record(record)

    # ------------------------------------------------------------- reading ---

    @property
    def records(self) -> list[ApiRecord]:
        return list(self._records)

    def unique(self) -> list[tuple[ApiRecord, int]]:
        """Distinct calls with how often each was seen, in first-seen order."""
        counts: dict[ApiRecord, int] = {}
        for record in self._records:
            counts[record] = counts.get(record, 0) + 1
        return list(counts.items())

    def render(self) -> str:
        if not self._records:
            return (
                "No HSC /api/ responses were observed.\n"
                "Either nothing was clicked, or this screen is server-rendered."
            )
        lines = [f"Observed {len(self._records)} HSC /api/ response(s):", ""]
        lines += [
            f"  {record.describe()}" + (f"   x{count}" if count > 1 else "")
            for record, count in self.unique()
        ]
        return "\n".join(lines)
