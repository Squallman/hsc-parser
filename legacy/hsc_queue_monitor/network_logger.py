"""Network inspection for discovering the still-unknown queue endpoints.

Attaches to a live Playwright context, records every ``/api/v2/equeue/`` call to
``data/network-events.jsonl`` and **redacts** anything that could authenticate a
request: cookies, authorization headers, CSRF/session/access tokens in bodies
and query strings. The capture is meant to be readable by a human (and pasted
into an issue) without leaking the session it was captured from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .config import API_PREFIX

logger = logging.getLogger(__name__)

REDACTED = "<redacted>"

#: Headers dropped wholesale; never written to disk, never logged.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "authentication",
        "x-csrf-token",
        "x-xsrf-token",
        "x-auth-token",
        "x-access-token",
        "x-api-key",
        "api-key",
        "next-auth.csrf-token",
    }
)

#: Any key/param whose name matches is replaced by ``<redacted>``.
SENSITIVE_KEY_RE = re.compile(
    r"(token|cookie|secret|password|passwd|pwd|authoriz|auth[-_]?key|csrf|xsrf|session|jwt|"
    r"bearer|signature|sign|otp|captcha|recaptcha|apikey|api[-_]?key|credential|access|refresh|"
    r"rnokpp|passport|inn|phone|email)",
    re.IGNORECASE,
)

MAX_BODY_CHARS = 8_000
MAX_LIST_ITEMS = 50
MAX_DEPTH = 8


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy headers with sensitive ones removed/redacted (case-insensitive)."""
    safe: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in SENSITIVE_HEADERS or SENSITIVE_KEY_RE.search(lowered):
            safe[lowered] = REDACTED
        else:
            safe[lowered] = value
    return safe


def redact_json(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact sensitive keys, truncate long strings and big lists."""
    if depth > MAX_DEPTH:
        return "<truncated:depth>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str):
                result[key_str] = REDACTED
            else:
                result[key_str] = redact_json(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        items = [redact_json(item, depth=depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f"<truncated:{len(value) - MAX_LIST_ITEMS} more items>")
        return items
    if isinstance(value, str) and len(value) > 512:
        return value[:512] + "<truncated>"
    return value


def redact_query(url: str) -> dict[str, str]:
    """Query parameters of ``url`` with sensitive values redacted."""
    parsed = urlsplit(url)
    params: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        params[key] = REDACTED if SENSITIVE_KEY_RE.search(key) else value
    return params


def redact_url(url: str) -> str:
    """URL with sensitive query values replaced (path is kept verbatim)."""
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    params = redact_query(url)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?{query}" if query else base


def redact_body(body: str | None) -> Any:
    """Redact a request/response body, JSON-aware with a text fallback."""
    if body is None:
        return None
    text = body.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if "=" in text and "\n" not in text and len(text) < 4_000:
            # Looks like a form-encoded body.
            pairs = parse_qsl(text, keep_blank_values=True)
            if pairs:
                return {k: (REDACTED if SENSITIVE_KEY_RE.search(k) else v) for k, v in pairs}
        snippet = text[:MAX_BODY_CHARS]
        return snippet + "<truncated>" if len(text) > MAX_BODY_CHARS else snippet
    return redact_json(parsed)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NetworkLogger:
    """Records interesting API traffic of a Playwright context to JSONL."""

    def __init__(
        self,
        output_path: Path,
        *,
        url_filter: str = API_PREFIX,
        max_body_chars: int = MAX_BODY_CHARS,
    ) -> None:
        self.output_path = output_path
        self.url_filter = url_filter
        self.max_body_chars = max_body_chars
        self.endpoints: dict[str, int] = {}
        self.event_count = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._context: Any | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------
    def attach(self, context: Any) -> None:
        """Subscribe to the context's response/requestfailed events."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._context = context
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_request_failed)
        logger.info("Network inspection enabled -> %s", self.output_path)

    async def detach(self) -> None:
        """Stop listening and wait for in-flight writes."""
        if self._context is not None:
            try:
                self._context.remove_listener("response", self._on_response)
                self._context.remove_listener("requestfailed", self._on_request_failed)
            except Exception:  # pragma: no cover - context may already be closed
                pass
            self._context = None
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def summary(self) -> str:
        if not self.endpoints:
            return "No matching API calls captured."
        lines = [f"Captured {self.event_count} API event(s):"]
        for endpoint, count in sorted(self.endpoints.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:4d}  {endpoint}")
        return "\n".join(lines)

    # -- event handlers -----------------------------------------------------
    def _matches(self, url: str) -> bool:
        return self.url_filter in url

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_response(self, response: Any) -> None:
        if not self._matches(response.url):
            return
        self._spawn(self._handle_response(response))

    def _on_request_failed(self, request: Any) -> None:
        if not self._matches(request.url):
            return
        self._spawn(self._handle_request_failed(request))

    async def _handle_response(self, response: Any) -> None:
        request = response.request
        record: dict[str, Any] = {
            "ts": _now(),
            "type": "response",
            "method": request.method,
            "url": redact_url(response.url),
            "path": urlsplit(response.url).path,
            "query": redact_query(response.url),
            "status": response.status,
            "resource_type": getattr(request, "resource_type", None),
        }
        try:
            post_data = request.post_data
        except Exception:  # pragma: no cover - binary payloads
            post_data = None
        if post_data:
            record["request_body"] = redact_body(post_data)

        try:
            headers = await response.all_headers()
        except Exception:  # pragma: no cover
            headers = {}
        content_type = headers.get("content-type", "")
        record["response_content_type"] = content_type.split(";")[0] or None

        body_text: str | None = None
        try:
            body_text = await response.text()
        except Exception:
            body_text = None

        if body_text is None:
            record["response_body"] = "<unavailable>"
        elif len(body_text) > self.max_body_chars:
            record["response_body"] = "<too-large>"
            record["response_size"] = len(body_text)
        else:
            record["response_body"] = redact_body(body_text)

        await self._write(record)
        logger.info(
            "captured %s %s -> %s",
            record["method"],
            record["path"],
            record["status"],
        )

    async def _handle_request_failed(self, request: Any) -> None:
        record = {
            "ts": _now(),
            "type": "requestfailed",
            "method": request.method,
            "url": redact_url(request.url),
            "path": urlsplit(request.url).path,
            "query": redact_query(request.url),
            "failure": getattr(request, "failure", None),
        }
        await self._write(record)

    async def _write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            with self.output_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self.event_count += 1
        key = f"{record.get('method')} {record.get('path')}"
        self.endpoints[key] = self.endpoints.get(key, 0) + 1
