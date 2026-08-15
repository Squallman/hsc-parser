"""Network inspection tests — especially that nothing sensitive is written."""

from __future__ import annotations

import json

from hsc_queue_monitor.network_logger import (
    REDACTED,
    NetworkLogger,
    redact_body,
    redact_headers,
    redact_json,
    redact_query,
    redact_url,
)

COOKIE_VALUE = "__Host-next.equeue-session=super-secret-value; bm_sv=abc"
TOKEN = "eyJhbGciOiJSU0EtT0FFUCJ9.super.secret"


def test_redact_headers_removes_credentials():
    safe = redact_headers(
        {
            "Cookie": COOKIE_VALUE,
            "Set-Cookie": COOKIE_VALUE,
            "Authorization": f"Bearer {TOKEN}",
            "X-CSRF-Token": "9207e3ee",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    assert safe["cookie"] == REDACTED
    assert safe["set-cookie"] == REDACTED
    assert safe["authorization"] == REDACTED
    assert safe["x-csrf-token"] == REDACTED
    assert safe["accept"] == "application/json"
    assert COOKIE_VALUE not in json.dumps(safe)
    assert TOKEN not in json.dumps(safe)


def test_redact_headers_is_case_insensitive():
    assert redact_headers({"COOKIE": COOKIE_VALUE})["cookie"] == REDACTED


def test_redact_json_hides_sensitive_keys_at_any_depth():
    payload = {
        "departmentId": 8041,
        "accessToken": TOKEN,
        "user": {"sessionId": "abc", "csrfToken": "def", "name": "Ivan"},
        "items": [{"refresh_token": TOKEN, "date": "2026-08-20"}],
    }
    safe = redact_json(payload)
    assert safe["departmentId"] == 8041
    assert safe["accessToken"] == REDACTED
    assert safe["user"]["sessionId"] == REDACTED
    assert safe["user"]["csrfToken"] == REDACTED
    assert safe["user"]["name"] == "Ivan"
    assert safe["items"][0]["refresh_token"] == REDACTED
    assert safe["items"][0]["date"] == "2026-08-20"
    assert TOKEN not in json.dumps(safe)


def test_redact_json_truncates_long_lists_and_strings():
    safe = redact_json({"items": list(range(120)), "blob": "x" * 2000})
    assert len(safe["items"]) == 51
    assert "more items" in safe["items"][-1]
    assert safe["blob"].endswith("<truncated>")


def test_redact_query_and_url():
    url = "https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId=47&access_token=" + TOKEN
    params = redact_query(url)
    assert params["serviceId"] == "47"
    assert params["access_token"] == REDACTED
    assert TOKEN not in redact_url(url)
    assert "serviceId=47" in redact_url(url)


def test_redact_url_without_query_is_unchanged():
    url = "https://eqn.hsc.gov.ua/api/v2/equeue/departments"
    assert redact_url(url) == url


def test_redact_body_handles_json_form_and_text():
    assert redact_body(json.dumps({"token": TOKEN, "id": 1})) == {"token": REDACTED, "id": 1}
    assert redact_body(f"csrfToken={TOKEN}&departmentId=8041") == {
        "csrfToken": REDACTED,
        "departmentId": "8041",
    }
    assert redact_body("plain text") == "plain text"
    assert redact_body(None) is None
    assert redact_body("   ") is None


# ------------------------------------------------------------ live capture
class FakeRequest:
    def __init__(self, url, method="GET", post_data=None):
        self.url = url
        self.method = method
        self.post_data = post_data
        self.resource_type = "fetch"


class FakeResponse:
    def __init__(self, request, status=200, body="{}", headers=None):
        self.request = request
        self.url = request.url
        self.status = status
        self._body = body
        self._headers = headers or {"content-type": "application/json"}

    async def all_headers(self):
        return self._headers

    async def text(self):
        return self._body


class FakeContext:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)


async def test_logger_writes_redacted_records(tmp_path):
    path = tmp_path / "network-events.jsonl"
    net_logger = NetworkLogger(path)
    context = FakeContext()
    net_logger.attach(context)

    request = FakeRequest(
        "https://eqn.hsc.gov.ua/api/v2/equeue/slots?departmentId=8041&access_token=" + TOKEN,
        method="POST",
        post_data=json.dumps({"csrfToken": TOKEN, "departmentId": 8041}),
    )
    response = FakeResponse(
        request,
        body=json.dumps({"data": [{"time": "10:40", "sessionToken": TOKEN}]}),
        headers={"content-type": "application/json", "set-cookie": COOKIE_VALUE},
    )

    context.handlers["response"][0](response)
    await net_logger.detach()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["method"] == "POST"
    assert record["path"] == "/api/v2/equeue/slots"
    assert record["query"] == {"departmentId": "8041", "access_token": REDACTED}
    assert record["status"] == 200
    assert record["request_body"]["csrfToken"] == REDACTED
    assert record["response_body"]["data"][0]["time"] == "10:40"

    raw = path.read_text(encoding="utf-8")
    assert TOKEN not in raw
    assert "super-secret-value" not in raw
    assert "set-cookie" not in raw.lower()


async def test_logger_ignores_unrelated_traffic(tmp_path):
    path = tmp_path / "network-events.jsonl"
    net_logger = NetworkLogger(path)
    context = FakeContext()
    net_logger.attach(context)

    request = FakeRequest("https://eqn.hsc.gov.ua/_next/static/chunk.js")
    context.handlers["response"][0](FakeResponse(request))
    await net_logger.detach()

    assert not path.exists() or path.read_text(encoding="utf-8") == ""
    assert net_logger.event_count == 0


async def test_logger_skips_huge_bodies_and_summarises(tmp_path):
    path = tmp_path / "network-events.jsonl"
    net_logger = NetworkLogger(path, max_body_chars=100)
    context = FakeContext()
    net_logger.attach(context)

    request = FakeRequest("https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId=47")
    context.handlers["response"][0](FakeResponse(request, body="x" * 500))
    await net_logger.detach()

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["response_body"] == "<too-large>"
    assert record["response_size"] == 500
    assert "GET /api/v2/equeue/departments" in net_logger.summary()
