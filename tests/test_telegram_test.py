"""`telegram-test`: does the bot reach its people, and nothing else?

The value of this command is what it *cannot* do. If it needed MongoDB to run,
a failure would be ambiguous; if it could open a browser, it would be a slow way
to answer a fast question. So the boundary is walked here as an import graph,
not asserted as an intention.

The rest is delivery arithmetic: one request per recipient, one person's failure
not silencing the others, and an exit code that tells a human whether to go
looking.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest
import requests

from hsc_queue_monitor.cli import EXIT_CONFIG, EXIT_OK, EXIT_RUNTIME, run_telegram_test
from hsc_queue_monitor.models import ConfigError
from hsc_queue_monitor.notifications.selftest import (
    TEST_MESSAGE,
    DeliveryReport,
    send_test_message,
)
from hsc_queue_monitor.notifications.telegram import TelegramNotifier

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"
NOTIFICATIONS = SRC / "notifications"

#: Shaped like a real one, and deliberately not one.
TOKEN = "111222333:AA-THIS-IS-NOT-A-REAL-TOKEN-abcdefghijklm"


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}


class Recorder:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [FakeResponse(200)]
        self.calls: list[tuple[str, dict[str, Any], tuple[float, float]]] = []

    def __call__(self, url: str, payload: Any, timeout: Any) -> FakeResponse:
        self.calls.append((url, dict(payload), timeout))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def chat_ids(self) -> list[int]:
        return [payload["chat_id"] for _url, payload, _t in self.calls]

    @property
    def texts(self) -> list[str]:
        return [payload["text"] for _url, payload, _t in self.calls]


class Printed:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def bot(*answers: Any, recipients: tuple[int, ...] = (111, 222)) -> tuple[Any, Recorder]:
    post = Recorder(*answers)
    return TelegramNotifier(TOKEN, recipients, post=post), post


def run(*answers: Any, recipients: tuple[int, ...] = (111, 222)) -> tuple[int, Printed, Recorder]:
    notifier, post = bot(*answers, recipients=recipients)
    printed = Printed()
    code = run_telegram_test(_config(), notifier=notifier, emit=printed)
    return code, printed, post


def _config() -> Any:
    """A config object the command never actually reads when given a notifier."""
    from test_api_monitor import monitor_config

    return monitor_config(PROJECT_ROOT / "tests")


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #


def test_the_message_is_exactly_the_agreed_text():
    assert TEST_MESSAGE == (
        "✅ Тестове повідомлення\n"
        "\n"
        "HSC Parser успішно підключений до Telegram."
    )


def test_the_message_is_plain_text():
    _code, _printed, post = run()

    for _url, payload, _timeout in post.calls:
        assert set(payload) == {"chat_id", "text"}  # no parse_mode, no markup
        assert payload["text"] == TEST_MESSAGE


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def test_one_recipient_gets_one_request():
    code, printed, post = run(recipients=(111,))

    assert code == EXIT_OK
    assert post.chat_ids == [111]
    assert "Recipients: 1" in printed.text
    assert "sent successfully to 1 recipient(s)" in printed.text


def test_every_recipient_gets_their_own_request():
    code, printed, post = run(recipients=(111, 222, 333))

    assert code == EXIT_OK
    assert post.chat_ids == [111, 222, 333]
    assert len(post.calls) == 3  # never one request carrying three people
    assert "Recipients: 3" in printed.text


def test_a_duplicated_recipient_is_only_sent_to_once(tmp_path, monkeypatch):
    """Deduplication happens where the list is parsed, and it holds here."""
    from test_api_monitor import monitor_config

    from hsc_queue_monitor.cli import build_notifier

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_USERS", "111,222,111, 222 ,333")
    notifier = build_notifier(monitor_config(tmp_path))

    assert notifier is not None
    assert notifier.recipients == (111, 222, 333)


def test_a_failure_does_not_stop_the_recipients_after_it():
    code, printed, post = run(
        FakeResponse(200), FakeResponse(403), FakeResponse(200), recipients=(111, 222, 333)
    )

    assert post.chat_ids == [111, 222, 333]  # the third person still heard
    assert code == EXIT_RUNTIME
    assert "Sent:   2" in printed.text
    assert "Failed: 1" in printed.text
    assert "pressed Start" in printed.text


def test_everything_delivered_exits_zero():
    code, printed, _post = run()

    assert code == EXIT_OK
    assert "Failed:" not in printed.text


def test_a_total_failure_exits_non_zero():
    code, printed, post = run(FakeResponse(401), recipients=(111,))

    assert code == EXIT_RUNTIME
    assert "Sent:   0" in printed.text
    assert len(post.calls) == 1  # one attempt per recipient, no retry


def test_a_transport_failure_counts_as_a_failure():
    code, _printed, _post = run(requests.ConnectionError("no route"), recipients=(111,))

    assert code == EXIT_RUNTIME


def test_the_report_adds_up():
    notifier, _post = bot(FakeResponse(200), FakeResponse(403), recipients=(111, 222))
    report = send_test_message(notifier, emit=lambda _text: None)

    assert report == DeliveryReport(recipients=2, sent=1, failed=1)
    assert not report.ok
    assert DeliveryReport(recipients=2, sent=2, failed=0).ok


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_no_configuration_at_all_fails_clearly(tmp_path, monkeypatch, capsys):
    from test_api_monitor import monitor_config

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_USERS", raising=False)

    code = run_telegram_test(monitor_config(tmp_path))

    assert code == EXIT_CONFIG
    error = capsys.readouterr().err
    assert "TELEGRAM TEST FAILED" in error
    assert "Telegram notifications are not configured." in error


@pytest.mark.parametrize(
    ("token", "users", "expected"),
    [
        (TOKEN, None, "TELEGRAM_USERS is empty"),
        (None, "123456789", "TELEGRAM_BOT_TOKEN is empty"),
    ],
)
def test_half_configuration_reuses_the_existing_validation(
    tmp_path, monkeypatch, capsys, token, users, expected
):
    from test_api_monitor import monitor_config

    for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_USERS", users)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    code = run_telegram_test(monitor_config(tmp_path))

    assert code == EXIT_CONFIG
    assert expected in capsys.readouterr().err


def test_a_malformed_recipient_list_is_refused(tmp_path, monkeypatch):
    from test_api_monitor import monitor_config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_USERS", "111,not-an-id")

    with pytest.raises(ConfigError, match="numeric Telegram ids"):
        monitor_config(tmp_path)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_the_token_never_appears_in_the_output_or_the_logs(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    code, printed, _post = run(FakeResponse(200), FakeResponse(403))
    captured = capsys.readouterr()

    everything = "\n".join([printed.text, captured.out, captured.err, caplog.text])
    assert code == EXIT_RUNTIME
    assert TOKEN not in everything
    assert "api.telegram.org" not in everything  # the URL carries the token
    assert "bot111222333" not in everything


def test_recipients_stay_masked_in_the_logs(caplog):
    caplog.set_level(logging.INFO)
    run(recipients=(123456789,))

    assert "***6789" in caplog.text
    assert "123456789" not in caplog.text


def test_a_transport_exception_is_never_logged_verbatim(caplog):
    caplog.set_level(logging.WARNING)
    run(requests.ConnectionError(f"POST https://api.telegram.org/bot{TOKEN}/x"), recipients=(111,))

    assert TOKEN not in caplog.text
    assert "ConnectionError" in caplog.text  # the class, which is safe


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #


FORBIDDEN = (
    "hscapiclient",
    "mongosessionstore",
    "monitorstatestore",
    "availabilitysnapshotstore",
    "browsermanager",
    "browsersessionprovider",
    "playwright",
    "queuepage",
    "loginpage",
    "authmanager",
    "pymongo",
    "cryptography",
    "chromium",
)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(f"{'.' * node.level}{node.module or ''}")
    return names


def resolve(module: str, source: Path) -> Path | None:
    """Where a (possibly relative) import lands inside this project."""
    if module.startswith("."):
        depth = len(module) - len(module.lstrip("."))
        base = source.parent
        for _ in range(depth - 1):
            base = base.parent
        tail = module.lstrip(".").replace(".", "/")
        candidate = base / f"{tail}.py" if tail else base / "__init__.py"
        return candidate if candidate.exists() else None

    if module.startswith("hsc_queue_monitor"):
        tail = module.split(".", 1)[1].replace(".", "/") if "." in module else ""
        candidate = SRC / f"{tail}.py"
        return candidate if candidate.exists() else None
    return None


def closure(start: Path) -> dict[Path, set[str]]:
    seen: dict[Path, set[str]] = {}
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        found = imports_of(current)
        seen[current] = found
        pending += [
            resolved
            for name in found
            if (resolved := resolve(name, current)) is not None and resolved not in seen
        ]
    return seen


def test_the_self_test_cannot_reach_a_database_a_browser_or_hsc():
    """The point of the command: when it fails, Telegram is what failed."""
    walked = closure(NOTIFICATIONS / "selftest.py")

    assert {path.name for path in walked} == {
        "selftest.py",
        "telegram.py",
        "base.py",
        "models.py",
        "logging_config.py",
    }
    for path, imported in walked.items():
        for name in imported:
            assert not any(bad in name.lower() for bad in FORBIDDEN), f"{path.name}: {name}"


def test_the_self_test_names_nothing_it_must_not_touch():
    tree = ast.parse((NOTIFICATIONS / "selftest.py").read_text(encoding="utf-8"))
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }

    for name in used:
        assert not any(bad in name.lower() for bad in FORBIDDEN)


def test_the_self_test_does_not_reach_the_availability_templates():
    """They are built from API types, and this path may not import those."""
    assert "templates_uk" not in imports_of(NOTIFICATIONS / "selftest.py")
    assert "TEST_MESSAGE" in (NOTIFICATIONS / "selftest.py").read_text(encoding="utf-8")


def test_the_bot_still_cannot_receive_anything():
    tree = ast.parse((NOTIFICATIONS / "selftest.py").read_text(encoding="utf-8"))
    literals = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    used = {
        (node.attr if isinstance(node, ast.Attribute) else node.id).lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }

    for forbidden in ("getupdates", "setwebhook", "deletewebhook", "webhook", "polling"):
        assert not [text for text in literals if forbidden in text]
        assert not [name for name in used if forbidden in name]


def test_only_one_http_client_exists_for_telegram():
    """The transport is reused, not reimplemented."""
    source = (NOTIFICATIONS / "selftest.py").read_text(encoding="utf-8")

    assert "requests" not in source
    assert "sendMessage" not in source
    assert "send_test_message" in source


def test_the_command_is_synchronous_and_declares_no_browser():
    import inspect

    from hsc_queue_monitor.cli import build_parser, cmd_telegram_test

    assert not inspect.iscoroutinefunction(run_telegram_test)
    args = build_parser().parse_args(["telegram-test"])
    assert args.needs_browser is False
    assert args.func is cmd_telegram_test


def test_the_command_touches_no_store_and_no_scan():
    import inspect

    source = inspect.getsource(run_telegram_test)

    for forbidden in (
        "build_session_store",
        "monitor_state_store",
        "availability_snapshot_store",
        "run_headless_scan",
        "BrowserSessionProvider",
        "app_session",
    ):
        assert forbidden not in source
