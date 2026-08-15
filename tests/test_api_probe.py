"""The direct-API experiment: browser session -> requests.Session -> one GET.

Nothing here touches the network or a browser. The fakes reproduce the two
things the experiment depends on — a Playwright BrowserContext that hands out
cookies, and an HTTP client that answers and sets its own cookies — so the
safety properties can be tested as hard as the feature:

* the authentication path runs first, and nothing in the wizard is clicked;
* cookies leave the browser for exactly one host and never come back;
* a cookie *value* never reaches a log, a print or a result object;
* a 403 or a 429 ends the experiment instead of starting a workaround.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import FakePage, FakeResponse

from hsc_queue_monitor import cli as cli_module
from hsc_queue_monitor.api import endpoints as endpoints_module
from hsc_queue_monitor.api import probe as probe_module
from hsc_queue_monitor.api.endpoints import MEASURED_REQUESTS, MeasuredRequest, require_read_only
from hsc_queue_monitor.api.observer import ApiObserver, safe_target
from hsc_queue_monitor.api.probe import (
    API_ORIGIN,
    CONNECT_TIMEOUT,
    DEFAULT_PATH,
    READ_TIMEOUT,
    WIZARD_COOKIE,
    CookieInfo,
    build_session,
    compare_fingerprints,
    describe_cookies,
    fingerprint,
    hsc_cookies,
    http_get,
    is_hsc_cookie_domain,
    perform,
    render_outcome,
    resolve_url,
    session_fingerprints,
)
from hsc_queue_monitor.cli import EXIT_CONFIG, EXIT_OK, EXIT_RUNTIME, main, run_api_probe
from hsc_queue_monitor.config import (
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SelectorRegistry,
    load_secrets,
)
from hsc_queue_monitor.flow.steps import FlowContext
from hsc_queue_monitor.models import ApiProbeError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"

CABINET = "https://eqn.hsc.gov.ua/cabinet"
DEPARTMENTS_URL = f"{API_ORIGIN}{DEFAULT_PATH}"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) TestChrome/131.0.0.0"

#: Invented stand-ins for live session material. They exist to be asserted
#: *absent* from every line this feature produces.
ACCESS_TOKEN_VALUE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.NEVER-LOG-ME"
EQUEUE_VALUE = "wizard-state-step-1-NEVER-LOG-ME"
EQUEUE_VALUE_AFTER = "wizard-state-step-2-NEVER-LOG-ME"
CSRF_VALUE = "csrf-NEVER-LOG-ME"
IDGOV_VALUE = "idgov-NEVER-LOG-ME"

BROWSER_COOKIES: list[dict[str, Any]] = [
    {
        "name": "__Secure-auth.access-token",
        "value": ACCESS_TOKEN_VALUE,
        "domain": "eqn.hsc.gov.ua",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "name": WIZARD_COOKIE,
        "value": EQUEUE_VALUE,
        "domain": "eqn.hsc.gov.ua",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "name": "__Host-next-auth.csrf-token",
        "value": CSRF_VALUE,
        "domain": ".hsc.gov.ua",
        "path": "/",
        "secure": True,
    },
    # Must never be exported: a different site's session has no business in an
    # HSC API call.
    {"name": "SESSION", "value": IDGOV_VALUE, "domain": "id.gov.ua", "path": "/"},
    {"name": "_ga", "value": "ga-value", "domain": ".google-analytics.com", "path": "/"},
]

SECRET_VALUES = (
    ACCESS_TOKEN_VALUE,
    EQUEUE_VALUE,
    EQUEUE_VALUE_AFTER,
    CSRF_VALUE,
    IDGOV_VALUE,
)

#: A departments payload shaped like the measured one, plus a field that is not
#: in the summary — so "the body is not dumped" is testable.
UNPRINTED = "SHOULD-NOT-BE-PRINTED"
DEPARTMENTS = [
    {"id": 2, "name": "ТСЦ МВС № 3242", "allowOnlineCount": 1, "internalNote": UNPRINTED},
    {"id": 3, "name": "ТСЦ МВС № 4641", "allowOnlineCount": 0, "internalNote": UNPRINTED},
    {"id": 4, "name": "ТСЦ МВС № 8043", "allowOnlineCount": 5, "internalNote": UNPRINTED},
]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeBrowserContext:
    """Playwright's BrowserContext, reduced to the one method that is called.

    ``add_cookies`` and ``clear_cookies`` exist only to fail: writing back into
    the browser is the thing this experiment must never do.
    """

    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = cookies

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(cookie) for cookie in self._cookies]

    async def add_cookies(self, cookies: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("the diagnostic wrote cookies back into the browser")

    async def clear_cookies(self) -> None:  # pragma: no cover - must not run
        raise AssertionError("the diagnostic cleared the browser cookies")


class ProbePage(FakePage):
    """A page sitting on an authenticated cabinet, with a cookie jar behind it."""

    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.url = CABINET
        self.context = FakeBrowserContext(BROWSER_COOKIES if cookies is None else cookies)

    async def evaluate(self, script: str) -> Any:
        self.calls.append(("evaluate", (script,), {}))
        if "userAgent" in script:
            return USER_AGENT
        return await super().evaluate(script)

    @property
    def interactions(self) -> list[str]:
        """Every call that would have driven the UI. Expected to stay empty."""
        driving = {
            "get_by_role",
            "get_by_text",
            "get_by_label",
            "get_by_placeholder",
            "get_by_test_id",
            "locator",
            "goto",
        }
        return [api for api, _args, _kwargs in self.calls if api in driving]


class FakeAuth:
    """The one authentication path, recorded rather than performed."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def ensure_authenticated(self, **_kwargs: Any) -> None:
        self.calls += 1
        self.events.append("authenticate")


class FakeHttpResponse:
    def __init__(self, status: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.content = content


class Responder:
    """A fake ``fetch``: answers, and sets cookies the way a server would."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"[]",
        content_type: str = "application/json",
        location: str = "",
        sets: dict[str, str] | None = None,
        raises: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.location = location
        self.sets = sets or {}
        self.raises = raises
        self.events = events if events is not None else []
        self.requests: list[str] = []
        self.timeouts: list[tuple[float, float]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.cookies_seen: list[dict[str, str]] = []

    def __call__(
        self, session: Any, url: str, timeout: tuple[float, float] = (5, 60)
    ) -> FakeHttpResponse:
        self.events.append("fetch")
        self.requests.append(url)
        self.timeouts.append(timeout)
        self.headers_seen.append(dict(session.headers))
        self.cookies_seen.append({c.name: c.value for c in session.cookies})
        if self.raises is not None:
            raise self.raises
        for name, value in self.sets.items():
            session.cookies.set(name, value, domain="eqn.hsc.gov.ua", path="/")
        headers = {"Content-Type": self.content_type}
        if self.location:
            headers["Location"] = self.location
        return FakeHttpResponse(self.status, headers, self.body)


def json_body(payload: Any) -> bytes:
    import json

    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


SELECTORS = {
    "login": {"authenticated_marker": {"strategy": "text", "value": "Записатись у чергу"}}
}
FLOW = {"site": {"base_url": "https://eqn.hsc.gov.ua", "cabinet_url": CABINET}}


def build_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        app=AppSettings(),
        paths=Paths(data_dir=tmp_path),
        service_centers=[],
        _selectors=SelectorRegistry.from_dict(SELECTORS),
        _flow=FlowConfig.from_dict(FLOW),
    )


def build_context(
    tmp_path: Path, page: ProbePage, events: list[str]
) -> tuple[AppConfig, FlowContext, FakeAuth]:
    config = build_config(tmp_path)
    ctx = FlowContext(config=config, page=page)
    auth = FakeAuth(events)
    # Replaces the real guard with a recorder; every other page object stays as
    # production builds it, so nothing can quietly click on the way past.
    ctx.auth = auth  # type: ignore[assignment]
    return config, ctx, auth


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #


def test_relative_path_resolves_against_the_api_host():
    assert resolve_url("/api/v2/equeue/departments?serviceId=47") == DEPARTMENTS_URL
    assert resolve_url(None) == DEPARTMENTS_URL
    assert resolve_url("  ") == DEPARTMENTS_URL


def test_absolute_url_on_the_api_host_is_accepted():
    assert resolve_url(DEPARTMENTS_URL) == DEPARTMENTS_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/api/v2/equeue/departments",
        "https://eqn.hsc.gov.ua.evil.example.com/api",
        "https://id.gov.ua/api/v2/equeue/departments",
        "https://hsc.gov.ua/api/v2/equeue/departments",  # sibling host, not the API
        "http://eqn.hsc.gov.ua/api/v2/equeue/departments",  # plaintext
        "//evil.example.com/api",
        "ftp://eqn.hsc.gov.ua/api",
        "api/v2/equeue/departments",  # neither a path nor a URL
    ],
)
def test_foreign_or_ambiguous_urls_are_refused(url):
    with pytest.raises(ApiProbeError):
        resolve_url(url)


def test_cli_refuses_a_foreign_host_without_opening_a_browser(capsys):
    # No app_session, no Playwright: the refusal happens on the arguments alone.
    assert main(["api-probe", "--url", "https://evil.example.com/api"]) == EXIT_CONFIG
    assert "evil.example.com" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Cookie export
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("eqn.hsc.gov.ua", True),
        (".hsc.gov.ua", True),
        ("hsc.gov.ua", True),
        ("id.gov.ua", False),
        ("hsc.gov.ua.evil.com", False),
        ("evilhsc.gov.ua", False),
        ("", False),
    ],
)
def test_only_hsc_domains_count_as_hsc(domain, expected):
    assert is_hsc_cookie_domain(domain) is expected


def test_only_hsc_cookies_are_exported():
    exported = hsc_cookies(BROWSER_COOKIES)
    assert [c["name"] for c in exported] == [
        "__Secure-auth.access-token",
        WIZARD_COOKIE,
        "__Host-next-auth.csrf-token",
    ]
    assert IDGOV_VALUE not in str(exported)


def test_cookie_descriptions_cannot_carry_a_value():
    infos = describe_cookies(hsc_cookies(BROWSER_COOKIES))
    assert {f.name for f in CookieInfo.__dataclass_fields__.values()} == {
        "name",
        "domain",
        "path",
        "fingerprint",
    }
    rendered = "\n".join(info.describe() for info in infos)
    for secret in SECRET_VALUES:
        assert secret not in rendered
    assert all(re.fullmatch(r"[0-9a-f]{8}", info.fingerprint) for info in infos)


def test_fingerprint_is_a_short_one_way_prefix():
    assert fingerprint(EQUEUE_VALUE) != fingerprint(EQUEUE_VALUE_AFTER)
    assert fingerprint(EQUEUE_VALUE) == fingerprint(EQUEUE_VALUE)
    assert len(fingerprint(EQUEUE_VALUE)) == 8
    assert EQUEUE_VALUE[:8] not in fingerprint(EQUEUE_VALUE)


def test_session_receives_the_cookies_with_their_scope():
    session = build_session(hsc_cookies(BROWSER_COOKIES), user_agent=USER_AGENT)
    jar = {cookie.name: cookie for cookie in session.cookies}

    assert set(jar) == {
        "__Secure-auth.access-token",
        WIZARD_COOKIE,
        "__Host-next-auth.csrf-token",
    }
    assert jar[WIZARD_COOKIE].value == EQUEUE_VALUE  # copied verbatim, not re-encoded
    assert jar[WIZARD_COOKIE].domain == "eqn.hsc.gov.ua"
    assert jar[WIZARD_COOKIE].path == "/"
    assert jar["__Host-next-auth.csrf-token"].domain == ".hsc.gov.ua"
    # requests builds the Cookie header itself; we never hand it a raw string.
    assert "Cookie" not in session.headers


def test_session_sends_the_browsers_request_shape():
    session = build_session(hsc_cookies(BROWSER_COOKIES), user_agent=USER_AGENT)
    assert session.headers["User-Agent"] == USER_AGENT
    assert session.headers["Accept"] == "application/json, text/plain, */*"
    assert session.headers["Referer"] == "https://eqn.hsc.gov.ua/"
    assert session.headers["Accept-Language"] == "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"
    # Not copied: unproven browser-only headers.
    assert not [h for h in session.headers if h.lower().startswith("sec-fetch")]


def test_get_is_issued_without_redirects_and_with_both_timeouts():
    captured: dict[str, Any] = {}

    class RecordingSession:
        def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeHttpResponse(200, {}, b"{}")

    http_get(RecordingSession(), DEPARTMENTS_URL)
    assert captured["url"] == DEPARTMENTS_URL
    # 60s to read, measured: 30s was too short for a live /slots call.
    assert captured["timeout"] == (5, 60)
    assert captured["allow_redirects"] is False


def test_the_read_budget_is_the_whole_budget_for_one_request():
    """Raised because the server was slow, not spent twice by asking again."""
    attempts: list[tuple[float, float]] = []

    class RecordingSession:
        def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
            attempts.append(kwargs["timeout"])
            return FakeHttpResponse(200, {}, b"{}")

    http_get(RecordingSession(), DEPARTMENTS_URL)
    http_get(RecordingSession(), DEPARTMENTS_URL, (5, 120))

    assert attempts == [(CONNECT_TIMEOUT, READ_TIMEOUT), (5, 120)]
    assert (CONNECT_TIMEOUT, READ_TIMEOUT) == (5, 60)


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #


def probe_once(**kwargs: Any):
    session = build_session(hsc_cookies(BROWSER_COOKIES), user_agent=USER_AGENT)
    responder = Responder(**kwargs)
    return perform(session, DEPARTMENTS_URL, fetch=responder), responder


def test_json_response_is_parsed_and_summarized():
    outcome, responder = probe_once(body=json_body(DEPARTMENTS))

    assert responder.requests == [DEPARTMENTS_URL]
    assert outcome.ok and outcome.kind == "json"
    assert outcome.status == 200
    assert outcome.content_type == "application/json"
    assert outcome.redirect == ""
    assert outcome.body_bytes == len(json_body(DEPARTMENTS))
    assert outcome.payload == DEPARTMENTS

    report = render_outcome(outcome)
    assert "Departments: 3" in report
    assert "id=2" in report and "allowOnlineCount=1" in report


def test_the_body_is_not_dumped_by_default():
    outcome, _ = probe_once(body=json_body(DEPARTMENTS))
    report = render_outcome(outcome, items=2)

    assert UNPRINTED not in report
    assert "internalNote" not in report
    assert "… 1 more (not printed)" in report


def test_a_wrapped_list_is_summarized_too():
    outcome, _ = probe_once(body=json_body({"data": DEPARTMENTS, "total": 3}))
    assert "Departments: 3" in render_outcome(outcome)


def test_an_unrecognised_shape_falls_back_to_a_structure_summary():
    outcome, _ = probe_once(body=json_body({"state": "WIZARD", "step": 1}))
    report = render_outcome(outcome)
    assert "JSON: object(2 keys)" in report
    assert "state: string" in report


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "unauthorized"), (403, "forbidden"), (429, "rate-limited")],
)
def test_rejections_are_reported_once_and_never_worked_around(status, kind):
    outcome, responder = probe_once(status=status, content_type="text/html", body=b"<html>")

    assert outcome.kind == kind
    assert outcome.responded and not outcome.ok
    # One request. No retry, no backoff loop, no second identity.
    assert len(responder.requests) == 1

    # The report states the finding; it never proposes a way around it.
    report = render_outcome(outcome).lower()
    for forbidden in ("captcha", "akamai", "bypass", "spoof", "proxy", "rotate"):
        assert forbidden not in report


def test_a_redirect_stays_visible():
    outcome, _ = probe_once(status=302, content_type="text/html", body=b"", location="/login")
    assert outcome.kind == "redirect"
    assert outcome.redirect == "/login"
    assert "Redirect:   /login" in render_outcome(outcome)


def test_an_empty_success_is_its_own_finding_not_a_broken_body():
    # Measured live: the departments endpoint answered 204 with nothing in it.
    outcome, _ = probe_once(status=204, content_type="", body=b"")

    assert outcome.kind == "no-content"
    assert outcome.responded and not outcome.ok
    assert outcome.body_bytes == 0
    report = render_outcome(outcome)
    assert "Status:     204" in report
    assert WIZARD_COOKIE in report  # the reading it points at
    for forbidden in ("captcha", "akamai", "bypass"):
        assert forbidden not in report.lower()


def test_a_non_json_body_is_reported_by_type_only():
    outcome, _ = probe_once(content_type="text/html", body=b"<html>challenge</html>")
    report = render_outcome(outcome)
    assert outcome.kind == "non-json"
    assert "challenge" not in report
    assert "Body bytes: 22" in report


def test_unparseable_json_is_reported_without_the_body():
    outcome, _ = probe_once(content_type="application/json", body=b"{not json")
    assert outcome.kind == "bad-json"
    assert "not json" not in render_outcome(outcome)


def test_a_timeout_is_distinct_from_a_rejection():
    import requests

    outcome, responder = probe_once(raises=requests.Timeout("read timed out"))
    assert outcome.kind == "timeout"
    assert not outcome.responded
    # One attempt. A timed-out request may still be in flight on the server;
    # sending it again would be a duplicate, not a retry worth having.
    assert len(responder.requests) == 1
    # The classification and its wording are unchanged — only the budget moved.
    assert outcome.error == "Timeout after 5s/60s (connect/read)"

    # The live failure was a ReadTimeout; it classifies the same way.
    read_timeout, _ = probe_once(raises=requests.ReadTimeout("read timed out"))
    assert read_timeout.kind == "timeout"
    assert read_timeout.error == "ReadTimeout after 5s/60s (connect/read)"
    assert "timed out" in outcome.verdict


def test_a_network_failure_is_distinct_from_a_timeout():
    import requests

    outcome, _ = probe_once(raises=requests.ConnectionError("name resolution failed"))
    assert outcome.kind == "network-error"
    assert not outcome.responded


# --------------------------------------------------------------------------- #
# Session-cookie state transitions
# --------------------------------------------------------------------------- #


def test_a_changed_session_cookie_is_detected_by_fingerprint():
    outcome, _ = probe_once(
        body=json_body(DEPARTMENTS), sets={WIZARD_COOKIE: EQUEUE_VALUE_AFTER}
    )

    change = outcome.change_for(WIZARD_COOKIE)
    assert change is not None and change.state == "changed"
    assert change.before == fingerprint(EQUEUE_VALUE)
    assert change.after == fingerprint(EQUEUE_VALUE_AFTER)
    # Untouched cookies are not reported as movement.
    assert [c.name for c in outcome.moved_cookies] == [WIZARD_COOKIE]

    report = render_outcome(outcome)
    assert f"{WIZARD_COOKIE} -> changed" in report
    for secret in SECRET_VALUES:
        assert secret not in report


def test_an_unchanged_session_cookie_is_reported_as_such():
    outcome, _ = probe_once(body=json_body(DEPARTMENTS))
    change = outcome.change_for(WIZARD_COOKIE)
    assert change is not None and change.state == "unchanged"
    assert outcome.moved_cookies == ()
    assert "(none — no cookie in the isolated session changed)" in render_outcome(outcome)


def test_added_and_removed_cookies_are_distinguished():
    changes = {
        c.name: c
        for c in compare_fingerprints(
            {"a": "1111aaaa", "b": "2222bbbb"}, {"a": "3333cccc", "c": "4444dddd"}
        )
    }
    assert changes["a"].state == "changed"
    assert changes["b"].state == "removed"
    assert changes["c"].state == "added"


def test_session_fingerprints_never_expose_values():
    session = build_session(hsc_cookies(BROWSER_COOKIES))
    prints = session_fingerprints(session)
    assert prints[WIZARD_COOKIE] == fingerprint(EQUEUE_VALUE)
    assert EQUEUE_VALUE not in str(prints)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


async def test_authentication_runs_first_and_nothing_is_clicked(tmp_path, capsys):
    events: list[str] = []
    page = ProbePage()
    config, ctx, auth = build_context(tmp_path, page, events)
    responder = Responder(body=json_body(DEPARTMENTS), events=events)

    assert await run_api_probe(config, ctx, fetch=responder) == EXIT_OK

    assert auth.calls == 1
    assert events == ["authenticate", "fetch"]
    # Stopped at /cabinet: no queue card, no menu, no wizard step.
    assert page.interactions == []
    assert responder.requests == [DEPARTMENTS_URL]

    out = capsys.readouterr().out
    assert "API PROBE" in out
    assert "Departments: 3" in out


async def test_the_request_carries_the_browser_cookies_and_identity(tmp_path):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(body=json_body(DEPARTMENTS))

    await run_api_probe(config, ctx, fetch=responder)

    assert responder.cookies_seen[0] == {
        "__Secure-auth.access-token": ACCESS_TOKEN_VALUE,
        WIZARD_COOKIE: EQUEUE_VALUE,
        "__Host-next-auth.csrf-token": CSRF_VALUE,
    }
    headers = responder.headers_seen[0]
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Referer"] == "https://eqn.hsc.gov.ua/"
    assert "Authorization" not in headers


async def test_no_cookie_value_reaches_the_logs_or_the_terminal(tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(
        body=json_body(DEPARTMENTS), sets={WIZARD_COOKIE: EQUEUE_VALUE_AFTER}
    )

    await run_api_probe(config, ctx, fetch=responder)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    printed = capsys.readouterr()
    everything = f"{logged}\n{printed.out}\n{printed.err}"

    for secret in SECRET_VALUES:
        assert secret not in everything
    # Names are the diagnostic; they must still be there.
    assert "Exported 3 HSC cookies" in logged
    assert WIZARD_COOKIE in logged and "__Secure-auth.access-token" in logged


async def test_browser_cookies_are_never_written_back(tmp_path):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    # FakeBrowserContext.add_cookies / clear_cookies raise if they are called.
    await run_api_probe(
        config, ctx, fetch=Responder(body=json_body(DEPARTMENTS), sets={WIZARD_COOKIE: "new"})
    )


async def test_a_rejection_is_still_a_successful_experiment(tmp_path, capsys):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(status=403, content_type="text/html", body=b"<html>")

    # 403 is an observation about the API, not a failure of the command.
    assert await run_api_probe(config, ctx, fetch=responder) == EXIT_OK
    assert len(responder.requests) == 1
    assert "direct requests are not accepted" in capsys.readouterr().out.lower()


async def test_never_reaching_the_server_is_a_runtime_failure(tmp_path):
    import requests

    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(raises=requests.ConnectionError("no route"))

    assert await run_api_probe(config, ctx, fetch=responder) == EXIT_RUNTIME


async def test_without_hsc_cookies_the_experiment_stops(tmp_path, capsys):
    page = ProbePage([{"name": "SESSION", "value": IDGOV_VALUE, "domain": "id.gov.ua"}])
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(body=json_body(DEPARTMENTS))

    assert await run_api_probe(config, ctx, fetch=responder) == EXIT_RUNTIME
    assert responder.requests == []
    assert "No hsc.gov.ua cookies" in capsys.readouterr().err


async def test_a_foreign_url_is_refused_before_authentication(tmp_path):
    events: list[str] = []
    page = ProbePage()
    config, ctx, auth = build_context(tmp_path, page, events)
    responder = Responder(events=events)

    result = await run_api_probe(config, ctx, url="https://evil.example.com/api", fetch=responder)

    assert result == EXIT_CONFIG
    assert auth.calls == 0 and responder.requests == []


# --------------------------------------------------------------------------- #
# The measured sequence
# --------------------------------------------------------------------------- #


def test_the_sequence_contains_only_measured_get_requests():
    assert MEASURED_REQUESTS  # never empty: the HAR gives us one
    for request in MEASURED_REQUESTS:
        assert request.method == "GET"
        assert request.evidence, f"{request.name} has no measurement behind it"
        assert resolve_url(request.path).startswith(API_ORIGIN)


def test_a_non_get_measurement_is_recorded_but_never_executed():
    booking = MeasuredRequest(name="book", path="/api/v2/equeue/reserve", method="POST")
    with pytest.raises(ApiProbeError, match="only ever issues GET"):
        require_read_only(booking)


async def test_sequence_mode_walks_the_measured_list(tmp_path):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(body=json_body(DEPARTMENTS))

    assert await run_api_probe(config, ctx, sequence=True, fetch=responder) == EXIT_OK
    assert responder.requests == [resolve_url(r.path) for r in MEASURED_REQUESTS]


async def test_sequence_mode_stops_at_the_first_refusal(tmp_path, capsys):
    page = ProbePage()
    config, ctx, _auth = build_context(tmp_path, page, [])
    responder = Responder(status=429, content_type="text/html", body=b"slow down")

    await run_api_probe(config, ctx, sequence=True, fetch=responder)

    assert len(responder.requests) == 1
    assert "Stopping the sequence" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The browser-side observer
# --------------------------------------------------------------------------- #


def observe(*responses: FakeResponse) -> ApiObserver:
    page = FakePage()
    observer = ApiObserver(page)
    observer.start()
    for response in responses:
        page.emit("response", response)
    return observer


def test_only_hsc_api_responses_are_observed():
    observer = observe(
        FakeResponse(f"{API_ORIGIN}{DEFAULT_PATH}", 200, content_type="application/json"),
        FakeResponse(f"{API_ORIGIN}/cabinet", 200),  # not under /api/
        FakeResponse("https://id.gov.ua/api/v1/whoami", 200),  # not HSC
        FakeResponse("https://eqn.hsc.gov.ua.evil.com/api/x", 200),  # look-alike host
    )
    assert [record.target for record in observer.records] == [DEFAULT_PATH]


def test_an_observation_reads_like_the_example():
    observer = observe(
        FakeResponse(f"{API_ORIGIN}{DEFAULT_PATH}", 200, content_type="application/json")
    )
    assert observer.records[0].describe() == (
        "GET /api/v2/equeue/departments?serviceId=47 -> 200 application/json"
    )


def test_sensitive_query_values_are_redacted_but_names_are_kept():
    target = safe_target(f"{API_ORIGIN}/api/v2/equeue/slots?departmentId=3242&code=SECRET")
    assert "departmentId=3242" in target
    assert "code=[REDACTED]" in target
    assert "SECRET" not in target


def test_the_observer_only_listens():
    page = FakePage()
    observer = ApiObserver(page)
    observer.start()
    assert page.listener_count == 1  # one response listener; nothing else
    observer.stop()
    observer.stop()  # idempotent
    assert page.listener_count == 0
    assert page.calls == []  # it never drove the page


def test_repeated_calls_are_counted_once_in_the_summary():
    response = FakeResponse(f"{API_ORIGIN}{DEFAULT_PATH}", 200, content_type="application/json")
    observer = observe(response, response, response)
    rendered = observer.render()
    assert "Observed 3 HSC /api/ response(s)" in rendered
    assert "x3" in rendered


# --------------------------------------------------------------------------- #
# The boundary around production code
# --------------------------------------------------------------------------- #


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(f"{'.' * node.level}{node.module or ''}")
    return names


PRODUCTION_MODULES = [
    SRC / "flow" / "auth.py",
    SRC / "flow" / "availability.py",
    SRC / "flow" / "engine.py",
    SRC / "flow" / "steps.py",
    SRC / "monitor" / "monitor.py",
    *sorted((SRC / "pages").glob("*.py")),
]


@pytest.mark.parametrize("module", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_production_code_does_not_use_the_experiment(module):
    """The monitor, the flows and the pages stay UI-driven and offline."""
    imported = imports_of(module)
    assert not any(name.startswith("requests") for name in imported)
    assert not any("api" in name.split(".") for name in imported)


@pytest.mark.parametrize(
    "command",
    [
        cli_module.run_check_center,
        cli_module.run_check_availability,
        cli_module.cmd_monitor,
        cli_module.cmd_ensure_auth,
    ],
)
def test_the_existing_commands_are_untouched_by_the_experiment(command):
    source = inspect.getsource(command)
    for symbol in ("perform", "build_session", "requests", "ApiObserver", "api_probe"):
        assert symbol not in source


def test_the_probe_never_writes_to_the_browser():
    """Whatever the flow, there is no code path that could push a cookie back."""
    for module in (probe_module, endpoints_module):
        source = inspect.getsource(module)
        for forbidden in ("add_cookies", "clear_cookies", "add_init_script"):
            assert forbidden not in source
    probe_calls = inspect.getsource(cli_module.run_api_probe)
    assert "add_cookies" not in probe_calls


def test_har_captures_can_never_be_committed():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.har" in ignored
    # And there is no live capture sitting in the tree waiting to be added.
    assert not [p for p in PROJECT_ROOT.rglob("*.har") if ".venv" not in p.parts]
