"""Logging with mandatory redaction.

Passwords, Telegram tokens, cookies and authorization headers must never reach
a log file or the terminal. The redactor is applied as a filter on the root
handler so it also covers third-party libraries.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

#: Patterns that look like credentials regardless of where they came from.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Header-shaped: everything after the colon is the credential, including
    # any "Bearer " prefix, so consume to the end of the line.
    re.compile(r"(?i)\b(authorization|cookie|set-cookie)\b\s*[:=][^\n]+"),
    re.compile(r"(?i)\bbearer\s+[\w\-.=+/]+"),
    re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|csrf[_-]?token|session)"
               r"\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*\S+"),
    # Telegram bot tokens: 123456789:AA... — no leading \b, because these
    # normally appear glued to a URL path segment ("/bot123456789:AA...").
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_\-]{30,}"),
)


#: Query parameters whose *values* are credentials or session identity. An
#: OIDC URL carries the authorization code and the state that binds the
#: session, so neither may reach an artifact — the parameter names are kept,
#: because "there was a code" is exactly what a diagnostic needs to say.
SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "code",
        "state",
        "session_state",
        "nonce",
        "token",
        "id_token",
        "access_token",
        "refresh_token",
        "ticket",
        "assertion",
        "auth",
    }
)


def redact_url(url: str, secrets: tuple[str, ...] = ()) -> str:
    """A URL safe to write into an artifact.

    Sensitive query *values* are replaced; the rest of the URL is left intact
    so it stays recognisable. The raw pairs are edited in place rather than
    re-encoded, so nothing else about the URL changes.
    """
    parts = urlsplit(url)
    if not parts.query:
        return redact(url, secrets)

    pairs = []
    for pair in parts.query.split("&"):
        key, separator, _value = pair.partition("=")
        if separator and key.lower() in SENSITIVE_QUERY_KEYS:
            pairs.append(f"{key}={REDACTED}")
        else:
            pairs.append(pair)

    return redact(urlunsplit(parts._replace(query="&".join(pairs))), secrets)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Scrub known secret values and credential-shaped patterns from ``text``."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(
            lambda m: _keep_label(m.group(0)),
            text,
        )
    return text


def _keep_label(matched: str) -> str:
    """Preserve ``key=`` / ``key:`` so the log stays readable, drop the value."""
    for separator in (":", "="):
        head, found, _ = matched.partition(separator)
        if found and " " not in head.strip():
            return f"{head}{separator}{REDACTED}"
    return REDACTED


class RedactingFilter(logging.Filter):
    """Rewrites every record's rendered message through :func:`redact`."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    @property
    def secrets(self) -> tuple[str, ...]:
        return self._secrets

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets = (*self._secrets, secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken format string
            return True
        cleaned = redact(message, self._secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


_filter: RedactingFilter | None = None


def setup_logging(
    *, verbose: bool = False, secrets: tuple[str, ...] = ()
) -> RedactingFilter:
    """Configure root logging once and return the shared redaction filter."""
    global _filter

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    if _filter is None:
        _filter = RedactingFilter(secrets)
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        handler.addFilter(_filter)
        root.handlers = [handler]
    else:
        for secret in secrets:
            _filter.add_secret(secret)

    for handler in root.handlers:
        handler.setLevel(level)

    # Playwright is extremely chatty at DEBUG and its records can carry headers.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return _filter


def sanitize_url(url: str) -> Any:
    """:func:`redact_url` against the process-wide secret list."""
    return redact_url(url, _filter.secrets if _filter else ())


def sanitize(value: Any) -> Any:
    """Redact a value destined for a debug artifact (JSON dump, screenshot name)."""
    secrets = _filter.secrets if _filter else ()
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value
