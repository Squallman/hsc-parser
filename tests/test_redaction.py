"""Secrets must never reach a log line or a debug artifact."""

from __future__ import annotations

import logging

from hsc_queue_monitor.logging_config import (
    REDACTED,
    RedactingFilter,
    redact,
    redact_url,
)

SECRET = "sup3r-s3cret-pass"
TOKEN = "123456789:AAHqxLm0k3vQwErTyUiOpAsDfGhJkLzXcVb"


def test_known_secret_value_is_removed():
    assert SECRET not in redact(f"login with {SECRET} ok", (SECRET,))
    assert REDACTED in redact(f"login with {SECRET} ok", (SECRET,))


def test_password_assignment_is_redacted_without_knowing_the_value():
    assert "letmein" not in redact("password=letmein")


def test_authorization_header_is_redacted():
    cleaned = redact("Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc")
    assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in cleaned


def test_cookie_header_is_redacted():
    cleaned = redact("cookie: __Secure-auth.access-token=eyJhbGciOiJSU0EtT0FFUCJ9")
    assert "eyJhbGciOiJSU0EtT0FFUCJ9" not in cleaned


def test_access_token_assignment_is_redacted():
    assert "abc123def456" not in redact("access_token=abc123def456")
    assert "abc123def456" not in redact("refresh-token: abc123def456")


def test_csrf_token_is_redacted():
    assert "9207e3eeb12ca282" not in redact("csrf-token=9207e3eeb12ca282")


def test_telegram_bot_token_shape_is_redacted_even_if_unregistered():
    assert TOKEN not in redact(f"calling https://api.telegram.org/bot{TOKEN}/sendMessage")


def test_ordinary_text_survives_untouched():
    message = "Clicking category.category_a -> get_by_text('Категорія A')"
    assert redact(message) == message


def test_label_is_kept_so_logs_stay_readable():
    assert redact("password=letmein").startswith("password")


def test_filter_rewrites_log_records(caplog):
    logger = logging.getLogger("test.redaction")
    logger.addFilter(RedactingFilter((SECRET,)))
    logger.propagate = True

    with caplog.at_level(logging.INFO, logger="test.redaction"):
        logger.info("Filling password with %s", SECRET)

    assert SECRET not in caplog.text
    assert REDACTED in caplog.text


def test_filter_handles_lazy_format_arguments(caplog):
    logger = logging.getLogger("test.redaction.args")
    logger.addFilter(RedactingFilter((TOKEN,)))

    with caplog.at_level(logging.INFO, logger="test.redaction.args"):
        logger.info("token=%s chat=%s", TOKEN, "42")

    assert TOKEN not in caplog.text


def test_added_secrets_apply_to_later_records(caplog):
    redactor = RedactingFilter()
    logger = logging.getLogger("test.redaction.added")
    logger.addFilter(redactor)
    redactor.add_secret("later-secret")

    with caplog.at_level(logging.INFO, logger="test.redaction.added"):
        logger.info("value is later-secret")

    assert "later-secret" not in caplog.text


# --------------------------------------------------------------------------- #
# URLs written into artifacts
# --------------------------------------------------------------------------- #

OIDC_URL = (
    "https://id.gov.ua/?response_type=code&client_id=hsc"
    "&redirect_uri=https%3A%2F%2Feqn.hsc.gov.ua%2Fcallback"
    "&state=9f2a1c&code=AUTH-CODE-abc123"
)


def test_an_oidc_url_keeps_its_shape_but_loses_the_credentials():
    cleaned = redact_url(OIDC_URL)

    assert "AUTH-CODE-abc123" not in cleaned
    assert "9f2a1c" not in cleaned
    # What is left is what makes the URL readable in a diagnostic.
    assert cleaned.startswith("https://id.gov.ua/?")
    assert "response_type=code" in cleaned
    assert "client_id=hsc" in cleaned
    assert cleaned.count(REDACTED) == 2


def test_a_plain_url_is_unchanged():
    url = "https://eqn.hsc.gov.ua/cabinet/queue"
    assert redact_url(url) == url


def test_token_shaped_parameters_are_covered():
    for key in ("access_token", "id_token", "session_state", "nonce"):
        assert "VALUE" not in redact_url(f"https://id.gov.ua/x?{key}=VALUE")


def test_url_redaction_still_applies_known_secrets():
    assert SECRET not in redact_url(f"https://id.gov.ua/x?q={SECRET}", (SECRET,))


def test_sanitize_walks_nested_structures():
    from hsc_queue_monitor.logging_config import sanitize

    payload = {
        "url": "https://eqn.hsc.gov.ua/cabinet/queue",
        "elements": [{"text": "password=letmein"}],
    }
    cleaned = sanitize(payload)
    assert "letmein" not in str(cleaned)
    assert cleaned["url"] == payload["url"]
