"""API client and departments-parsing tests. Nothing here touches the network."""

from __future__ import annotations

import json

import pytest

from hsc_queue_monitor.api import (
    ApiError,
    ApiResponse,
    AuthenticationRequiredError,
    EndpointNotDiscoveredError,
    HscApiClient,
    RateLimitedError,
)
from hsc_queue_monitor.config import Settings
from hsc_queue_monitor.models import parse_departments

DEPARTMENTS_PAYLOAD = [
    {
        "id": 8041,
        "name": "ТСЦ 8041",
        "region": "м. Київ",
        "city": "Київ",
        "street": "вул. Набережно-Хрещатицька",
        "building": "27",
        "office": "2",
        "allowOnlineCount": 4,
    },
    {"id": 8042, "title": "ТСЦ 8042"},
]


class FakePage:
    """Stands in for playwright's Page: replays queued fetch results."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.url = "https://eqn.hsc.gov.ua/cabinet/queue"

    async def evaluate(self, expression: str, arg=None):
        self.calls.append(arg)
        if not self._responses:
            raise AssertionError("unexpected extra fetch call")
        result = self._responses.pop(0)
        return result


def js_result(status: int, body, *, content_type="application/json", ok=None):
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {
        "status": status,
        "ok": 200 <= status < 300 if ok is None else ok,
        "networkError": False,
        "contentType": content_type,
        "text": text,
        "truncated": False,
    }


def make_client(responses, **overrides):
    settings = Settings().with_overrides(**overrides)
    page = FakePage(responses)
    recorded_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)

    client = HscApiClient(page, settings, base_backoff_seconds=1.0, sleep=fake_sleep)
    return client, page, recorded_sleeps


# --------------------------------------------------------- payload parsing
def test_parse_departments_from_plain_list():
    departments = parse_departments(DEPARTMENTS_PAYLOAD)
    assert [d.id for d in departments] == [8041, 8042]
    first = departments[0]
    assert first.name == "ТСЦ 8041"
    assert first.city == "Київ"
    assert first.building == "27"
    assert first.allow_online_count == 4
    assert first.raw["id"] == 8041


def test_parse_departments_from_wrapped_payload():
    assert len(parse_departments({"data": DEPARTMENTS_PAYLOAD})) == 2


def test_parse_departments_tolerates_missing_fields():
    departments = parse_departments([{"id": 1}])
    assert departments[0].name is None
    assert departments[0].allow_online_count is None
    assert departments[0].address == ""


def test_parse_departments_ignores_junk():
    assert parse_departments({"data": ["nope", 5]}) == []
    assert parse_departments("garbage") == []
    assert parse_departments([{"unrelated": True}]) == []


# ------------------------------------------------------- response parsing
def test_api_response_parses_json():
    response = ApiResponse.from_js("https://x/y", js_result(200, {"a": 1}))
    assert response.status == 200
    assert response.ok is True
    assert response.json == {"a": 1}


def test_api_response_keeps_text_when_json_fails():
    response = ApiResponse.from_js("https://x/y", js_result(500, "<html>boom</html>", ok=False))
    assert response.json is None
    assert "boom" in response.text
    assert response.snippet.startswith("<html>")


def test_api_response_snippet_is_truncated():
    response = ApiResponse.from_js("https://x/y", js_result(500, "x" * 5000, ok=False))
    assert len(response.snippet) <= 301


def test_api_response_handles_network_error():
    payload = {
        "status": 0,
        "ok": False,
        "networkError": True,
        "contentType": "",
        "text": "TypeError: Failed to fetch",
        "truncated": False,
    }
    response = ApiResponse.from_js("https://x/y", payload)
    assert response.network_error is True
    assert response.status == 0


# ------------------------------------------------------------- API client
def test_build_url_appends_params():
    client, _, _ = make_client([])
    url = client.build_url("/api/v2/equeue/departments", {"serviceId": 47, "skip": None})
    assert url == "https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId=47"


async def test_get_departments_uses_service_id():
    client, page, _ = make_client([js_result(200, DEPARTMENTS_PAYLOAD)])

    departments = await client.get_departments(47)

    assert [d.id for d in departments] == [8041, 8042]
    call = page.calls[0]
    assert call["url"].endswith("/api/v2/equeue/departments?serviceId=47")
    assert call["method"] == "GET"
    # Cookies must be attached by the browser, never by us.
    assert "Cookie" not in call["headers"]
    assert call["headers"]["Accept"].startswith("application/json")


async def test_service_id_is_configurable():
    client, page, _ = make_client([js_result(200, [])], service_id=99)
    await client.get_departments()
    assert page.calls[0]["url"].endswith("serviceId=99")


async def test_401_flags_session_and_does_not_retry():
    client, page, sleeps = make_client([js_result(401, {"error": "unauthorized"})])

    with pytest.raises(AuthenticationRequiredError):
        await client.get_departments(47)

    assert client.session_suspect is True
    assert len(page.calls) == 1
    assert sleeps == []


async def test_403_does_not_retry():
    client, page, _ = make_client([js_result(403, "forbidden", content_type="text/plain")])
    with pytest.raises(AuthenticationRequiredError):
        await client.get_departments(47)
    assert len(page.calls) == 1


async def test_429_backs_off_exponentially_then_raises():
    responses = [js_result(429, {"error": "slow down"}) for _ in range(4)]
    client, page, sleeps = make_client(responses)

    with pytest.raises(RateLimitedError):
        await client.get_departments(47)

    assert len(page.calls) == 4
    # base 1s, doubling each attempt, plus up to 1s of jitter.
    assert 1.0 <= sleeps[0] <= 2.0
    assert 2.0 <= sleeps[1] <= 3.0
    assert 4.0 <= sleeps[2] <= 5.0


async def test_429_then_success_returns_data():
    client, _, sleeps = make_client([js_result(429, {}), js_result(200, DEPARTMENTS_PAYLOAD)])
    departments = await client.get_departments(47)
    assert len(departments) == 2
    assert len(sleeps) == 1


async def test_5xx_is_retried_then_reported():
    client, page, _ = make_client([js_result(503, "gateway", ok=False) for _ in range(4)])
    with pytest.raises(ApiError):
        await client.get_departments(47)
    assert len(page.calls) == 4


async def test_404_is_not_retried():
    client, page, _ = make_client([js_result(404, {"error": "nope"})])
    with pytest.raises(ApiError):
        await client.get_departments(47)
    assert len(page.calls) == 1


async def test_non_json_200_raises_with_snippet():
    client, _, _ = make_client([js_result(200, "<html>login</html>", content_type="text/html")])
    with pytest.raises(ApiError, match="not JSON"):
        await client.get_departments(47)


async def test_availability_endpoints_are_not_guessed():
    client, page, _ = make_client([])

    with pytest.raises(EndpointNotDiscoveredError):
        await client.get_available_dates(8041, service_id=47)
    with pytest.raises(EndpointNotDiscoveredError):
        await client.get_available_slots(8041, "2026-08-20", service_id=47)

    assert page.calls == []
