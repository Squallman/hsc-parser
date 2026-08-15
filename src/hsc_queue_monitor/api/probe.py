"""One HTTP GET against the HSC API, carrying the browser's session.

The experiment is deliberately narrow:

    authenticate  ->  stop at /cabinet  ->  export the HSC cookies
                  ->  requests.get(...)  ->  report what changed

and everything it does *not* do is as much of the design as what it does:

* it never books, reserves, submits or otherwise mutates anything — GET only;
* it never writes a cookie back into the browser. The ``requests.Session`` is an
  isolated copy, so a failed experiment cannot break the working UI session;
* it never decodes, edits or forges a cookie value. Values are copied verbatim
  and only ever *reported* as a short one-way fingerprint;
* it never retries, and it never tries to look like something it is not. A 403
  or a 429 is an answer — "direct requests are not accepted with this HTTP
  client" — not an invitation to spoof a TLS fingerprint or mint an anti-bot
  token.

Cookies are the sensitive material here, so the boundary is structural:
:class:`CookieInfo` has no field that could hold a value, and the only function
that sees values at all hands them straight to ``requests``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from ..models import ApiProbeError

logger = logging.getLogger(__name__)

#: The only host this experiment will ever send a browser cookie to.
API_HOST = "eqn.hsc.gov.ua"
API_ORIGIN = f"https://{API_HOST}"
#: Cookies from this registrable domain (and its subdomains) are exported.
COOKIE_DOMAIN = "hsc.gov.ua"

#: Measured from a real browser HAR. Not a guess — see :mod:`.endpoints`.
DEFAULT_PATH = "/api/v2/equeue/departments?serviceId=47"

#: The wizard-state cookie. The HAR shows the site rewriting it on API
#: responses, so whether one GET advances it is the thing to watch.
WIZARD_COOKIE = "__Host-next.equeue-session"

#: Conservative: the shape of the browser's own XHR, and nothing more. No
#: Sec-Fetch-* headers, no hand-built Cookie header, no invented User-Agent —
#: the real one is read from the browser when it is available.
BROWSER_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://eqn.hsc.gov.ua/",
}

#: (connect, read). Redirects stay visible on purpose — a 302 into the login
#: page is the most informative thing this experiment can find.
#:
#: The read budget is 60s, not 30s, because 30s was measured to be too short:
#: a live ``/slots`` request timed out at it on an endpoint that had answered in
#: earlier runs. Waiting longer is the only honest response to a slow server —
#: a second request would be a duplicate of one that may still be in flight, and
#: this package does not retry.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 60
ALLOW_REDIRECTS = False

#: The whole budget for one request, unless a caller passes its own.
DEFAULT_TIMEOUT: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT)

#: The read budget for the single retry a 502 buys (see
#: :meth:`~.client.HscApiClient._get`). A fixed value, never doubled, never
#: derived from how many 502s have been seen — there is no such memory.
BAD_GATEWAY_RETRY_READ_TIMEOUT = 120

#: Long enough to tell two values apart, far too short to attack.
FINGERPRINT_CHARS = 8

#: How many JSON records are printed. The full body is never dumped by default.
DEFAULT_ITEMS = 5

_UA_SCRIPT = "() => navigator.userAgent"


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #


def resolve_url(raw: str | None) -> str:
    """Absolute ``https://eqn.hsc.gov.ua/…`` URL, or refuse.

    A relative path is the expected input; an absolute URL is accepted only when
    it is already on the API host. Everything else is rejected rather than
    normalised, because the cookies about to be attached belong to HSC and must
    never leave it — and a hostname is not the place to be clever.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return f"{API_ORIGIN}{DEFAULT_PATH}"

    if candidate.startswith("//"):
        raise ApiProbeError(
            f"{candidate!r} is protocol-relative, so the host it would reach "
            "depends on context. Pass a path such as "
            f"{DEFAULT_PATH!r}, or the full {API_ORIGIN} URL."
        )
    if candidate.startswith("/"):
        return f"{API_ORIGIN}{candidate}"

    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if not parts.scheme or not host:
        raise ApiProbeError(
            f"{candidate!r} is neither a path nor an absolute URL. Pass a path "
            f"starting with '/', e.g. {DEFAULT_PATH!r}."
        )
    if parts.scheme != "https" or host != API_HOST or parts.port not in (None, 443):
        raise ApiProbeError(
            f"Refusing to send HSC session cookies to {candidate!r}.\n"
            f"This diagnostic only talks to {API_ORIGIN} — browser cookies must "
            "never be forwarded to another host."
        )
    return candidate


def display_url(url: str) -> str:
    """The path+query, so output stays readable and copy-pasteable."""
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    return f"{parts.path or '/'}{'?' + parts.query if parts.query else ''}"


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #


def fingerprint(value: str) -> str:
    """A short one-way SHA-256 prefix. The only form a value is ever shown in."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return digest[:FINGERPRINT_CHARS]


def is_hsc_cookie_domain(domain: str) -> bool:
    """Whether a cookie domain belongs to HSC (``hsc.gov.ua`` or a subdomain)."""
    host = (domain or "").strip().lower().lstrip(".")
    return host == COOKIE_DOMAIN or host.endswith(f".{COOKIE_DOMAIN}")


def hsc_cookies(raw: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The HSC cookies out of a Playwright cookie dump, values untouched.

    Every other domain in the persistent profile — including ID.GOV.UA, which
    has no business in an HSC API call — is dropped here, once, so no later
    step has to remember to.
    """
    kept: list[dict[str, Any]] = []
    for cookie in raw:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if not name or not is_hsc_cookie_domain(domain):
            continue
        kept.append(
            {
                "name": name,
                "value": str(cookie.get("value") or ""),
                "domain": domain,
                "path": str(cookie.get("path") or "/"),
                "secure": bool(cookie.get("secure", False)),
            }
        )
    return kept


@dataclass(frozen=True, slots=True)
class CookieInfo:
    """What may be said about one cookie out loud.

    There is deliberately no ``value`` field: this is the type that reaches the
    terminal and the logs, so it cannot carry one even by accident.
    """

    name: str
    domain: str
    path: str
    fingerprint: str

    def describe(self) -> str:
        return f"{self.name}  ({self.domain}{self.path}, {self.fingerprint})"


def describe_cookies(cookies: Iterable[Mapping[str, Any]]) -> list[CookieInfo]:
    """Names, scopes and fingerprints — never values."""
    return [
        CookieInfo(
            name=str(cookie.get("name") or ""),
            domain=str(cookie.get("domain") or ""),
            path=str(cookie.get("path") or "/"),
            fingerprint=fingerprint(str(cookie.get("value") or "")),
        )
        for cookie in cookies
    ]


def build_session(
    cookies: Iterable[Mapping[str, Any]], *, user_agent: str | None = None
) -> requests.Session:
    """A fresh session carrying the browser's HSC cookies and its request shape.

    ``requests`` builds the ``Cookie`` header itself from the jar — the browser's
    raw header is never copied, so nothing has to be parsed, split or re-encoded.
    """
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    if user_agent:
        # The browser's own identity, not an invented one.
        session.headers["User-Agent"] = user_agent

    for cookie in cookies:
        session.cookies.set(
            str(cookie["name"]),
            str(cookie.get("value") or ""),
            domain=str(cookie.get("domain") or API_HOST),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure", False)),
        )
    return session


def session_fingerprints(session: requests.Session) -> dict[str, str]:
    """``name -> fingerprint`` for everything currently in the jar."""
    return {cookie.name: fingerprint(cookie.value or "") for cookie in session.cookies}


@dataclass(frozen=True, slots=True)
class CookieChange:
    """How one cookie differed before and after a request."""

    name: str
    state: str  # added | removed | changed | unchanged
    before: str = ""
    after: str = ""

    @property
    def moved(self) -> bool:
        return self.state != "unchanged"

    def describe(self) -> str:
        match self.state:
            case "added":
                detail = f"(new {self.after})"
            case "removed":
                detail = f"(was {self.before})"
            case "changed":
                detail = f"({self.before} -> {self.after})"
            case _:
                detail = f"({self.after})"
        return f"{self.name:34s} {self.state:9s} {detail}"


def compare_fingerprints(
    before: Mapping[str, str], after: Mapping[str, str]
) -> tuple[CookieChange, ...]:
    """Fingerprint diff of two jar snapshots. Values are never compared."""
    changes: list[CookieChange] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is None:
            state = "added"
        elif new is None:
            state = "removed"
        elif old != new:
            state = "changed"
        else:
            state = "unchanged"
        changes.append(
            CookieChange(name=name, state=state, before=old or "", after=new or "")
        )
    return tuple(changes)


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


class HttpResponse(Protocol):
    """The slice of ``requests.Response`` this module reads. Never ``.text``."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def content(self) -> bytes: ...


#: ``(session, url, timeout) -> response``. The timeout is a *parameter* rather
#: than something bound into the fetch, because exactly one request — the single
#: retry a 502 buys — needs a different budget, and it must not be possible for
#: that budget to stick to anything else.
Fetch = Callable[[requests.Session, str, tuple[float, float]], HttpResponse]


def http_get(
    session: requests.Session,
    url: str,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> HttpResponse:
    """The one real network call. GET only, ever.

    There is no sibling for POST/PUT/PATCH/DELETE anywhere in this package, and
    that is the point: a diagnostic that cannot spell a mutating verb cannot
    accidentally book an appointment.
    """
    return session.get(url, timeout=timeout, allow_redirects=ALLOW_REDIRECTS)


#: Outcome kinds, in the order the diagnostic cares about them.
KIND_JSON = "json"
KIND_REDIRECT = "redirect"
KIND_UNAUTHORIZED = "unauthorized"
KIND_FORBIDDEN = "forbidden"
KIND_RATE_LIMITED = "rate-limited"
KIND_NO_CONTENT = "no-content"
KIND_NON_JSON = "non-json"
KIND_BAD_JSON = "bad-json"
KIND_HTTP_ERROR = "http-error"
KIND_TIMEOUT = "timeout"
KIND_NETWORK_ERROR = "network-error"

#: Kinds that mean the request never produced an HTTP response at all.
NO_RESPONSE_KINDS = frozenset({KIND_TIMEOUT, KIND_NETWORK_ERROR})

_VERDICTS: dict[str, str] = {
    KIND_JSON: (
        "The API answered with JSON to a plain HTTP client carrying the "
        "browser's cookies. Availability may be readable without the UI."
    ),
    KIND_REDIRECT: (
        "The API redirected instead of answering. The exported cookies are not "
        "enough on their own for this endpoint — most likely the session state "
        "the wizard builds up is missing."
    ),
    KIND_UNAUTHORIZED: (
        "401: the session cookies were not accepted. Nothing is retried and no "
        "token is reconstructed — the browser session stays untouched."
    ),
    KIND_FORBIDDEN: (
        "403: direct requests are not accepted with this HTTP client. No "
        "bot/WAF workaround is attempted, and none should be added."
    ),
    KIND_RATE_LIMITED: (
        "429: rate limited. Stopping here — retrying would be exactly the "
        "behaviour the status is asking us not to repeat."
    ),
    KIND_NO_CONTENT: (
        "The endpoint accepted the request and returned an empty body. Measured "
        f"alongside this: {WIZARD_COOKIE} was not among the exported cookies in "
        "the run that first produced it, so the likeliest reading is that the "
        "session carries no equeue wizard state yet — the cookie is created by "
        "opening the queue in the browser, not by signing in. Open the queue "
        "wizard once in the Chromium window and re-run to test that reading."
    ),
    KIND_NON_JSON: (
        "The response was not JSON. The body is not dumped; the content type "
        "above is what there is to go on."
    ),
    KIND_BAD_JSON: (
        "The response claimed to be JSON but could not be parsed. The body is "
        "not dumped."
    ),
    KIND_HTTP_ERROR: "The API answered with an error status. Nothing is retried.",
    KIND_TIMEOUT: (
        "The request timed out, so nothing at all is known about whether the "
        "endpoint would have accepted it."
    ),
    KIND_NETWORK_ERROR: "The request never reached the server.",
}


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What one GET produced. Metadata, structure and fingerprints only."""

    url: str
    kind: str
    status: int | None = None
    content_type: str = ""
    redirect: str = ""
    #: ``Retry-After`` exactly as the server sent it, if it sent one. Parsing is
    #: the retry policy's business, not this module's.
    retry_after: str = ""
    body_bytes: int = 0
    payload: Any = None
    json_error: str = ""
    error: str = ""
    changes: tuple[CookieChange, ...] = ()

    @property
    def ok(self) -> bool:
        """A parsed JSON body on a 2xx — the only "the API answered" case."""
        return self.kind == KIND_JSON

    @property
    def responded(self) -> bool:
        """Whether an HTTP response arrived, whatever its status."""
        return self.kind not in NO_RESPONSE_KINDS

    @property
    def verdict(self) -> str:
        return _VERDICTS.get(self.kind, "Unclassified outcome.")

    def change_for(self, name: str) -> CookieChange | None:
        return next((c for c in self.changes if c.name == name), None)

    @property
    def moved_cookies(self) -> tuple[CookieChange, ...]:
        return tuple(change for change in self.changes if change.moved)


def parse_json(content: bytes) -> tuple[Any, str]:
    """Parse a body, or say why not. The body itself is never returned as text."""
    if not content:
        return None, "empty body"
    try:
        return json.loads(content.decode("utf-8")), ""
    except UnicodeDecodeError as exc:
        return None, f"body is not UTF-8 ({exc.reason})"
    except json.JSONDecodeError as exc:
        # The message carries a position, never the surrounding bytes.
        return None, f"invalid JSON at line {exc.lineno} column {exc.colno}"


def _classify(
    status: int, content_type: str, payload: Any, json_error: str, body_bytes: int = 0
) -> str:
    if status in (401, 403, 429):
        return {401: KIND_UNAUTHORIZED, 403: KIND_FORBIDDEN, 429: KIND_RATE_LIMITED}[status]
    if 300 <= status < 400:
        return KIND_REDIRECT
    # A 2xx with nothing in it is not a broken response and not an HTML
    # interstitial — it is the server saying "nothing here", which for this API
    # is a state answer and deserves to be reported as one.
    if status < 400 and body_bytes == 0:
        return KIND_NO_CONTENT
    is_json_type = "json" in content_type
    if payload is not None or (is_json_type and not json_error):
        return KIND_JSON if status < 400 else KIND_HTTP_ERROR
    if status >= 400:
        return KIND_HTTP_ERROR
    return KIND_BAD_JSON if is_json_type else KIND_NON_JSON


def perform(
    session: requests.Session,
    url: str,
    *,
    fetch: Fetch = http_get,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> ProbeOutcome:
    """Issue one GET and describe it. Blocking — call it off the event loop.

    Exactly one request: this function has no notion of trying again. The one
    place a request is ever repeated is :meth:`~.client.HscApiClient._get`, and
    only for a 502.

    The cookie jar is fingerprinted on both sides of the call, which is how the
    experiment can say whether merely *reading* an endpoint advances the site's
    wizard state.
    """
    before = session_fingerprints(session)

    try:
        response = fetch(session, url, timeout)
    except requests.Timeout as exc:
        return ProbeOutcome(
            url=url, kind=KIND_TIMEOUT, error=f"{type(exc).__name__} after "
            f"{timeout[0]:g}s/{timeout[1]:g}s (connect/read)",
            changes=compare_fingerprints(before, session_fingerprints(session)),
        )
    except requests.RequestException as exc:
        return ProbeOutcome(
            url=url, kind=KIND_NETWORK_ERROR, error=f"{type(exc).__name__}: {exc}",
            changes=compare_fingerprints(before, session_fingerprints(session)),
        )

    content = response.content or b""
    headers = response.headers
    content_type = str(headers.get("Content-Type", "")).split(";")[0].strip().lower()
    payload, json_error = parse_json(content)
    status = int(response.status_code)

    return ProbeOutcome(
        url=url,
        kind=_classify(status, content_type, payload, json_error, len(content)),
        status=status,
        content_type=content_type,
        # A Location on this host is a path or an HSC URL; it carries no
        # credential of ours, and it is the whole point of not following it.
        redirect=str(headers.get("Location", "")),
        retry_after=str(headers.get("Retry-After", "")),
        body_bytes=len(content),
        payload=payload,
        json_error=json_error,
        changes=compare_fingerprints(before, session_fingerprints(session)),
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def type_name(value: Any) -> str:
    match value:
        case bool():
            return "bool"
        case dict():
            return f"object({len(value)} keys)"
        case list():
            return f"list[{len(value)}]"
        case str():
            return "string"
        case int() | float():
            return "number"
        case None:
            return "null"
        case _:  # pragma: no cover - JSON has no other types
            return type(value).__name__


#: Keys a payload may wrap its list under. Recognising a wrapper is not the same
#: as guessing a *field* name: the list is still identified by being a list.
WRAPPER_KEYS = ("data", "items", "result", "results", "departments", "days", "slots")


def sequence_in(payload: Any) -> Sequence[Any] | None:
    """The list a payload is, or the single list it obviously wraps.

    ``None`` when the payload is something else, or when it wraps more than one
    list and picking between them would be a guess.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    candidates = [
        value for key in WRAPPER_KEYS if isinstance(value := payload.get(key), list)
    ]
    return candidates[0] if len(candidates) == 1 else None


def records_in(payload: Any) -> Sequence[Mapping[str, Any]] | None:
    """The list of *objects* in a payload, if it obviously has one.

    Anything less obvious falls through to the generic structure summary rather
    than being guessed at.
    """
    items = sequence_in(payload)
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    return items


#: Fields printed for a department. Everything else in the record is ignored —
#: this is a structural summary, not a dump.
DEPARTMENT_FIELDS = ("id", "name", "allowOnlineCount")
#: The field that makes a record recognisably a service centre rather than some
#: other list of objects. Only used to choose a heading.
DEPARTMENT_MARKER = "allowOnlineCount"


def summarize_payload(payload: Any, *, items: int = DEFAULT_ITEMS) -> list[str]:
    """A safe structural summary. Never the whole body."""
    records = records_in(payload)
    if records is None:
        return [f"JSON: {type_name(payload)}", *_describe_keys(payload)]

    heading = "Departments" if DEPARTMENT_MARKER in records[0] else "Records"
    lines = [f"{heading}: {len(records)}", ""]
    shown = records[: max(items, 0)]
    for record in shown:
        if any(field in record for field in DEPARTMENT_FIELDS):
            lines.append(
                "  "
                + "  ".join(
                    f"{field}={record.get(field)!s}"
                    for field in DEPARTMENT_FIELDS
                    if field in record
                )
            )
        else:
            lines.append(f"  keys: {', '.join(sorted(map(str, record))[:8])}")
    remaining = len(records) - len(shown)
    if remaining > 0:
        lines.append(f"  … {remaining} more (not printed)")
    return lines


def _describe_keys(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [
        f"  {key}: {type_name(value)}" for key, value in list(payload.items())[:12]
    ]


def render_outcome(outcome: ProbeOutcome, *, items: int = DEFAULT_ITEMS) -> str:
    """The report for one request, ready to print."""
    lines = [
        "",
        f"URL:        {display_url(outcome.url)}",
        f"Status:     {outcome.status if outcome.status is not None else '<none>'}",
        f"Type:       {outcome.content_type or '<none>'}",
        f"Redirect:   {outcome.redirect or '<none>'}",
        f"Body bytes: {outcome.body_bytes}",
    ]
    if outcome.error:
        lines.append(f"Error:      {outcome.error}")
    if outcome.json_error and outcome.kind != KIND_JSON:
        lines.append(f"JSON:       not parsed ({outcome.json_error})")

    if outcome.payload is not None:
        lines += ["", *summarize_payload(outcome.payload, items=items)]

    lines += ["", "Cookie changes:"]
    moved = outcome.moved_cookies
    if moved:
        lines += [f"  {change.describe()}" for change in moved]
    else:
        lines.append("  (none — no cookie in the isolated session changed)")

    wizard = outcome.change_for(WIZARD_COOKIE)
    lines.append(
        f"\n{WIZARD_COOKIE} -> "
        + (wizard.state if wizard is not None else "not present in this session")
    )
    lines += ["", outcome.verdict, ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Browser side
# --------------------------------------------------------------------------- #


class CookieSource(Protocol):
    """The slice of Playwright's ``Page`` this module needs."""

    @property
    def context(self) -> Any: ...

    async def evaluate(self, expression: str) -> Any: ...


async def read_browser_cookies(page: CookieSource) -> list[dict[str, Any]]:
    """Cookies straight from the live BrowserContext — never from disk.

    Read-only in both directions: nothing is written back to the context here or
    anywhere else in this package.
    """
    cookies = await page.context.cookies()
    return [dict(cookie) for cookie in cookies]


async def read_user_agent(page: CookieSource) -> str | None:
    """The browser's own User-Agent, or ``None`` if it cannot be read."""
    try:
        agent = await page.evaluate(_UA_SCRIPT)
    except Exception:  # pragma: no cover - page closed mid-read
        logger.debug("Could not read the browser User-Agent")
        return None
    text = str(agent or "").strip()
    return text or None
