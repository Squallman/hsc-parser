"""Browser-context API client.

Every request is executed by ``fetch()`` *inside* the authenticated page, so the
browser attaches cookies, anti-bot headers and rotating session values itself.
This module never builds a ``Cookie`` header, never reads token values and never
tries to defeat any protection mechanism.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from .config import Settings
from .models import (
    AvailableDate,
    AvailableSlot,
    Department,
    JsonDict,
    JsonValue,
    parse_departments,
)

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 500_000
SNIPPET_CHARS = 300

# Executed in the page: same-origin fetch with the browser's own credentials.
_FETCH_JS = """
async (payload) => {
  const init = {
    method: payload.method,
    credentials: "include",
    headers: payload.headers,
  };
  if (payload.body !== null && payload.body !== undefined) {
    init.body = payload.body;
  }
  let response;
  try {
    response = await fetch(payload.url, init);
  } catch (error) {
    return {
      status: 0,
      ok: false,
      networkError: true,
      contentType: "",
      text: String(error),
      truncated: false,
    };
  }
  let text = "";
  try {
    text = await response.text();
  } catch (error) {
    text = "";
  }
  return {
    status: response.status,
    ok: response.ok,
    networkError: false,
    contentType: response.headers.get("content-type") || "",
    text: text.slice(0, payload.maxChars),
    truncated: text.length > payload.maxChars,
  };
}
"""


class PageLike(Protocol):
    """Minimal slice of ``playwright.async_api.Page`` used by the client."""

    @property
    def url(self) -> str: ...

    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...


class ApiError(RuntimeError):
    """Base class for API failures."""


class AuthenticationRequiredError(ApiError):
    """401/403: the browser session is stale and must be refreshed by a human."""


class RateLimitedError(ApiError):
    """429 that survived the configured backoff attempts."""


class EndpointNotDiscoveredError(NotImplementedError):
    """Raised by availability methods whose real endpoints are still unknown."""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Result of a fetch executed inside the page."""

    url: str
    status: int
    ok: bool
    text: str = ""
    content_type: str = ""
    truncated: bool = False
    network_error: bool = False
    json: JsonValue | None = field(default=None)

    @property
    def path(self) -> str:
        parsed = urlsplit(self.url)
        return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    @property
    def snippet(self) -> str:
        """Short, safe excerpt of the body for error logs."""
        body = (self.text or "").strip().replace("\n", " ")
        if len(body) <= SNIPPET_CHARS:
            return body
        return body[:SNIPPET_CHARS] + "…"

    @classmethod
    def from_js(cls, url: str, payload: Any) -> ApiResponse:
        if not isinstance(payload, dict):
            return cls(url=url, status=0, ok=False, network_error=True, text=str(payload))
        text = payload.get("text") or ""
        parsed: JsonValue | None = None
        if text:
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        return cls(
            url=url,
            status=int(payload.get("status") or 0),
            ok=bool(payload.get("ok")),
            text=text,
            content_type=str(payload.get("contentType") or ""),
            truncated=bool(payload.get("truncated")),
            network_error=bool(payload.get("networkError")),
            json=parsed,
        )


class HscApiClient:
    """Talks to the HSC queue API through an authenticated Playwright page."""

    def __init__(
        self,
        page: PageLike,
        settings: Settings,
        *,
        max_attempts: int = 4,
        base_backoff_seconds: float = 5.0,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._page = page
        self._settings = settings
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff_seconds
        self._sleep = sleep
        #: Set when the API answered 401/403 — a human must re-authenticate.
        self.session_suspect = False

    # -- low level ----------------------------------------------------------
    def build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self._settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}{'&' if '?' in url else '?'}{urlencode(clean)}"
        return url

    async def _fetch(
        self,
        url: str,
        *,
        method: str,
        body: str | None,
        headers: dict[str, str],
    ) -> ApiResponse:
        payload = {
            "url": url,
            "method": method,
            "headers": headers,
            "body": body,
            "maxChars": MAX_RESPONSE_CHARS,
        }
        raw = await self._page.evaluate(_FETCH_JS, payload)
        return ApiResponse.from_js(url, raw)

    async def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: JsonDict | None = None,
    ) -> ApiResponse:
        """Perform a request, retrying 429/5xx with exponential backoff.

        401/403 return immediately (no aggressive retry) after flagging the
        session as needing manual refresh.
        """
        url = self.build_url(path, params)
        headers = {"Accept": "application/json, text/plain, */*"}
        body: str | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, ensure_ascii=False)

        response = ApiResponse(url=url, status=0, ok=False, network_error=True)
        for attempt in range(1, self._max_attempts + 1):
            response = await self._fetch(url, method=method, body=body, headers=headers)
            log_path = _short_path(url)

            if response.network_error:
                logger.warning("%s %s -> network error: %s", method, log_path, response.snippet)
            else:
                logger.info("%s %s -> %s", method, log_path, response.status)

            if response.ok:
                self.session_suspect = False
                return response

            if response.status in (401, 403):
                self.session_suspect = True
                logger.warning(
                    "%s %s -> %s: browser session may need to be refreshed / "
                    "re-authenticated (run `login` again). Body: %s",
                    method,
                    log_path,
                    response.status,
                    response.snippet,
                )
                return response

            if response.status == 429 or response.status >= 500 or response.network_error:
                if attempt >= self._max_attempts:
                    break
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "%s %s -> %s, backing off %.1fs (attempt %d/%d). Body: %s",
                    method,
                    log_path,
                    response.status or "network-error",
                    delay,
                    attempt,
                    self._max_attempts,
                    response.snippet,
                )
                await self._sleep(delay)
                continue

            logger.warning(
                "%s %s -> %s. Body: %s", method, log_path, response.status, response.snippet
            )
            return response

        return response

    def _backoff_delay(self, attempt: int) -> float:
        return self._base_backoff * float(2 ** (attempt - 1)) + random.uniform(0, 1.0)

    async def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: JsonDict | None = None,
    ) -> JsonValue:
        """Like :meth:`request` but raises on failure and returns parsed JSON."""
        response = await self.request(path, method=method, params=params, json_body=json_body)
        if response.status in (401, 403):
            raise AuthenticationRequiredError(
                f"{_short_path(response.url)} -> {response.status}: session not authenticated"
            )
        if response.status == 429:
            raise RateLimitedError(f"{_short_path(response.url)} -> 429: rate limited")
        if not response.ok:
            raise ApiError(
                f"{_short_path(response.url)} -> {response.status or 'network-error'}: "
                f"{response.snippet}"
            )
        if response.json is None:
            raise ApiError(
                f"{_short_path(response.url)} -> {response.status}: response is not JSON "
                f"(content-type={response.content_type!r}): {response.snippet}"
            )
        return response.json

    # -- known endpoints ----------------------------------------------------
    async def get_departments(self, service_id: int | None = None) -> list[Department]:
        """GET /api/v2/equeue/departments?serviceId={service_id}."""
        resolved = service_id if service_id is not None else self._settings.service_id
        payload = await self.request_json(
            "/api/v2/equeue/departments", params={"serviceId": resolved}
        )
        departments = parse_departments(payload)
        logger.info("Parsed %d department(s) for serviceId=%s", len(departments), resolved)
        return departments

    # -- endpoints pending discovery ---------------------------------------
    # The frontend flow is: service -> department -> date -> time. The date and
    # slot endpoints have NOT been observed yet, so nothing is guessed here.
    # Run `python -m hsc_queue_monitor.cli inspect`, walk the booking flow by
    # hand, then implement the two methods below from data/network-events.jsonl.
    async def get_available_dates(
        self,
        department_id: int,
        *,
        service_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[AvailableDate]:
        """Available dates for a department. **Requires endpoint discovery.**

        Implementation sketch once the endpoint is known::

            payload = await self.request_json(
                "/api/v2/equeue/<discovered-path>",
                params={"serviceId": service_id, "departmentId": department_id},
            )
            return [
                d
                for record in unwrap_list(payload)
                if (d := AvailableDate.from_api(record,
                                                department_id=department_id,
                                                service_id=service_id))
            ]
        """
        raise EndpointNotDiscoveredError(
            "get_available_dates() is not implemented yet: the dates endpoint under "
            "/api/v2/equeue/ has not been observed. Run "
            "`python -m hsc_queue_monitor.cli inspect`, navigate service -> department -> "
            "date, then implement this method from data/network-events.jsonl."
        )

    async def get_available_slots(
        self,
        department_id: int,
        date: str,
        *,
        service_id: int | None = None,
    ) -> list[AvailableSlot]:
        """Available time slots for a department/date. **Requires discovery.**

        Same procedure as :meth:`get_available_dates`; parse the discovered
        payload with :meth:`AvailableSlot.from_api`.
        """
        raise EndpointNotDiscoveredError(
            "get_available_slots() is not implemented yet: the time-slot endpoint under "
            "/api/v2/equeue/ has not been observed. Run "
            "`python -m hsc_queue_monitor.cli inspect`, navigate service -> department -> "
            "date -> time, then implement this method from data/network-events.jsonl."
        )


def _short_path(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
