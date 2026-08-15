"""Outbound Telegram: what gets said, to whom, and — mostly — when not to.

The failure this file guards against is not a missing message. It is a bot that
writes to somebody's phone every five minutes: the same AUTH_REQUIRED, the same
availability, forever. So most of these tests assert *silence*, and the ones
that do send assert that the same event never sends twice.

Nothing here touches the network. The transport takes an injected ``post``, so a
test can read exactly what would have gone over the wire — including the fact
that the bot token never appears in a log line, which is the one secret this
package handles.
"""

from __future__ import annotations

import ast
import logging
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import requests

from hsc_queue_monitor.api.availability_snapshot import AvailabilityDiff, AvailableSlot
from hsc_queue_monitor.api.headless_monitor import HeadlessScan
from hsc_queue_monitor.api.monitor_state import MonitorStateTransition, MonitorStatus
from hsc_queue_monitor.config import parse_telegram_users
from hsc_queue_monitor.models import ConfigError
from hsc_queue_monitor.notifications.base import NotificationError
from hsc_queue_monitor.notifications.dispatcher import NotificationDispatcher
from hsc_queue_monitor.notifications.telegram import (
    MAX_TELEGRAM_TEXT,
    TelegramNotifier,
    mask,
)
from hsc_queue_monitor.notifications.templates_uk import (
    render_auth_required,
    render_availability,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"
NOTIFICATIONS = SRC / "notifications"

TOKEN = "123456789:AAH-NEVER-LOG-THIS-BOT-TOKEN-abcdefghijk"
USERS = (111111111, 222222222)

AUG_26 = date(2026, 8, 26)
AUG_27 = date(2026, 8, 27)


def slot(centre: str, day: date, start: str, end: str | None = None) -> AvailableSlot:
    return AvailableSlot(
        centre=centre,
        date=day,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end) if end else None,
    )


class FakeResponse:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class Recorder:
    """Stands in for ``requests.post``, and remembers everything it was given."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [FakeResponse(200)]
        self.calls: list[tuple[str, dict[str, Any], tuple[float, float]]] = []

    def __call__(
        self, url: str, payload: Any, timeout: tuple[float, float]
    ) -> FakeResponse:
        self.calls.append((url, dict(payload), timeout))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def texts(self) -> list[str]:
        return [payload["text"] for _url, payload, _t in self.calls]

    @property
    def chat_ids(self) -> list[int]:
        return [payload["chat_id"] for _url, payload, _t in self.calls]


def notifier(
    *answers: Any, recipients: tuple[int, ...] = USERS
) -> tuple[TelegramNotifier, Recorder]:
    post = Recorder(*answers)
    return TelegramNotifier(TOKEN, recipients, post=post), post


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789", (123456789,)),
        ("123456789,987654321", (123456789, 987654321)),
        ("123, 456 ,789", (123, 456, 789)),
        ("  123  ", (123,)),
        ("123,,456", (123, 456)),
        ("123,456,123", (123, 456)),  # deduplicated, first-seen order kept
        ("", ()),
        ("   ", ()),
        ("-1001234567890", (-1001234567890,)),  # a group id is negative
        ("9" * 30, (int("9" * 30),)),  # no 32-bit assumption anywhere
    ],
)
def test_recipient_lists_parse_forgivingly(raw, expected):
    assert parse_telegram_users(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "123,abc", "12.5", "123 456", "١٢٣٤"])
def test_a_malformed_recipient_is_refused(raw):
    with pytest.raises(ConfigError, match="numeric Telegram ids"):
        parse_telegram_users(raw)


def test_the_bot_token_is_registered_as_a_secret(tmp_path, monkeypatch):
    from hsc_queue_monitor.config import load_secrets

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_USERS", "123,456")
    secrets = load_secrets(env_file=tmp_path / "absent.env")

    assert TOKEN in secrets.redactable()
    # Each recipient id is redacted too: those name people.
    assert "123" in secrets.redactable() and "456" in secrets.redactable()
    assert secrets.telegram_users == (123, 456)
    assert secrets.telegram_configured


def test_neither_setting_means_notifications_are_simply_off(tmp_path, monkeypatch):
    from test_api_monitor import monitor_config

    from hsc_queue_monitor.cli import build_notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_USERS", raising=False)

    assert build_notifier(monitor_config(tmp_path)) is None


def test_a_token_with_nobody_to_send_to_is_refused(tmp_path, monkeypatch):
    from test_api_monitor import monitor_config

    from hsc_queue_monitor.cli import build_notifier

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_USERS", raising=False)

    with pytest.raises(ConfigError, match="TELEGRAM_USERS is empty"):
        build_notifier(monitor_config(tmp_path))


def test_recipients_with_no_bot_are_refused(tmp_path, monkeypatch):
    from test_api_monitor import monitor_config

    from hsc_queue_monitor.cli import build_notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_USERS", "123456789")

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN is empty"):
        build_notifier(monitor_config(tmp_path))


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def test_every_recipient_gets_their_own_request():
    bot, post = notifier()

    bot.send("привіт")

    assert len(post.calls) == 2  # never one request carrying two people
    assert post.chat_ids == [111111111, 222222222]
    assert post.texts == ["привіт", "привіт"]
    assert all(url.endswith("/sendMessage") for url, _p, _t in post.calls)
    assert bot.delivered == 2


def test_the_token_never_reaches_a_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    bot, _post = notifier(FakeResponse(200), FakeResponse(403))

    bot.send("привіт")

    assert TOKEN not in caplog.text
    assert "bot123456789" not in caplog.text
    assert "api.telegram.org" not in caplog.text  # the URL carries the token


def test_recipients_are_masked_in_logs(caplog):
    caplog.set_level(logging.INFO)
    bot, _post = notifier()

    bot.send("привіт")

    assert "***1111" in caplog.text
    assert "111111111" not in caplog.text
    assert mask(123456789) == "***6789"
    assert mask(12) == "***"


def test_a_bot_with_no_token_refuses_to_exist():
    with pytest.raises(NotificationError, match="bot token is required"):
        TelegramNotifier("", USERS)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "never pressed Start"),
        (401, "bot token was not accepted"),
        (403, "may have been blocked"),
        (429, "rate limiting"),
        (500, "delivery failed"),
    ],
)
def test_each_failure_says_what_it_means(status, expected, caplog):
    caplog.set_level(logging.WARNING)
    bot, _post = notifier(FakeResponse(status), recipients=(111111111,))

    bot.send("привіт")

    assert expected in caplog.text
    assert bot.failed == 1 and bot.delivered == 0


def test_a_rate_limit_is_logged_but_never_slept_on(caplog):
    caplog.set_level(logging.WARNING)
    bot, post = notifier(FakeResponse(429, {"Retry-After": "30"}), recipients=(111111111,))

    bot.send("привіт")

    assert "Retry-After: 30" in caplog.text
    assert len(post.calls) == 1  # one attempt per recipient, and no waiting


def test_one_bad_recipient_does_not_silence_the_others(caplog):
    caplog.set_level(logging.WARNING)
    bot, post = notifier(FakeResponse(403), FakeResponse(200))

    bot.send("привіт")

    assert len(post.calls) == 2  # the second person still heard about it
    assert bot.delivered == 1 and bot.failed == 1


def test_a_transport_failure_never_leaks_the_url(caplog):
    caplog.set_level(logging.WARNING)
    bot, _post = notifier(
        requests.ConnectionError(f"failed to POST {TOKEN}"), recipients=(111111111,)
    )

    bot.send("привіт")

    assert TOKEN not in caplog.text
    assert "ConnectionError" in caplog.text
    assert bot.failed == 1


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def test_added_only_reads_like_the_example():
    messages = render_availability(
        AvailabilityDiff(
            added=(
                slot("3242", AUG_26, "09:18", "09:44"),
                slot("3242", AUG_26, "10:10", "10:36"),
            )
        )
    )

    assert messages == [
        "🟢 З'явилися нові слоти\n"
        "\n"
        "ТСЦ МВС №3242\n"
        "📅 26.08.2026\n"
        "+ 09:18–09:44\n"
        "+ 10:10–10:36"
    ]


def test_removed_only_reads_like_the_example():
    messages = render_availability(
        AvailabilityDiff(
            removed=(
                slot("3242", AUG_26, "14:56", "15:22"),
                slot("3242", AUG_26, "15:22", "15:48"),
            )
        )
    )

    assert messages == [
        "🔴 Слоти більше недоступні\n"
        "\n"
        "ТСЦ МВС №3242\n"
        "📅 26.08.2026\n"
        "− 14:56–15:22\n"
        "− 15:22–15:48"
    ]


def test_additions_and_removals_are_one_combined_message():
    messages = render_availability(
        AvailabilityDiff(
            added=(slot("3242", AUG_26, "09:18", "09:44"),),
            removed=(slot("3242", AUG_27, "14:30", "14:56"),),
        )
    )

    assert len(messages) == 1  # one notification, not two
    assert messages[0] == (
        "🔄 Зміни доступності\n"
        "\n"
        "ТСЦ МВС №3242\n"
        "\n"
        "🟢 Нові слоти\n"
        "📅 26.08.2026\n"
        "+ 09:18–09:44\n"
        "\n"
        "🔴 Більше недоступні\n"
        "📅 27.08.2026\n"
        "− 14:30–14:56"
    )


def test_several_centres_each_get_their_own_block():
    messages = render_availability(
        AvailabilityDiff(
            added=(
                slot("4641", AUG_27, "08:26", "08:52"),
                slot("3242", AUG_26, "09:18", "09:44"),
            )
        )
    )

    assert messages == [
        "🟢 З'явилися нові слоти\n"
        "\n"
        "ТСЦ МВС №3242\n"
        "📅 26.08.2026\n"
        "+ 09:18–09:44\n"
        "\n"
        "ТСЦ МВС №4641\n"
        "📅 27.08.2026\n"
        "+ 08:26–08:52"
    ]


def test_several_dates_each_get_their_own_heading():
    messages = render_availability(
        AvailabilityDiff(
            added=(
                slot("3242", AUG_27, "08:00"),
                slot("3242", AUG_26, "09:00"),
                slot("3242", AUG_26, "08:00"),
            )
        )
    )

    assert messages[0].count("📅") == 2
    assert messages[0].index("26.08.2026") < messages[0].index("27.08.2026")
    # Centre, then date, then start — the same order everywhere.
    assert messages[0].index("+ 08:00") < messages[0].index("+ 09:00")


def test_the_order_does_not_depend_on_the_input_order():
    slots = [
        slot("4641", AUG_26, "10:00"),
        slot("3242", AUG_27, "08:00"),
        slot("3242", AUG_26, "09:00"),
    ]
    first = render_availability(AvailabilityDiff(added=tuple(slots)))
    second = render_availability(AvailabilityDiff(added=tuple(reversed(slots))))

    assert first == second


def test_a_slot_without_an_end_is_still_reported():
    messages = render_availability(AvailabilityDiff(added=(slot("3242", AUG_26, "09:18"),)))

    assert "+ 09:18\n" in messages[0] + "\n"
    assert "–" not in messages[0].split("📅")[1]


def test_no_change_says_nothing_at_all():
    assert render_availability(AvailabilityDiff()) == []


def test_the_auth_message_tells_the_reader_what_to_do():
    messages = render_auth_required()

    assert len(messages) == 1
    assert messages[0] == (
        "🔐 Потрібна повторна авторизація\n"
        "\n"
        "Сесія HSC більше не дійсна.\n"
        "Моніторинг призупинено.\n"
        "\n"
        "Запусти локально:\n"
        "\n"
        "python -m hsc_queue_monitor.cli refresh-session\n"
        "\n"
        "Після успішної авторизації моніторинг відновиться автоматично."
    )


def test_a_status_reason_may_be_appended():
    messages = render_auth_required("HTTP 401 Unauthorized")

    assert messages[0].endswith("Причина: HTTP 401 Unauthorized")


@pytest.mark.parametrize(
    "reason",
    [
        "Could not persist the monitor state: InvalidOperation",
        "mongodb+srv://user:pw@host",
        "__Host-next.equeue-session expired",
        "",
    ],
)
def test_anything_that_is_not_a_status_line_is_left_out(reason):
    """A phone is no place for a session detail, a URI or a cookie name."""
    messages = render_auth_required(reason)

    assert messages == [render_auth_required()[0]]
    assert reason not in messages[0] or reason == ""


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def many_slots(count: int, centre: str = "3242") -> tuple[AvailableSlot, ...]:
    return tuple(
        slot(centre, date(2026, 8, 1 + index // 20), f"{7 + index % 12:02d}:{index % 60:02d}")
        for index in range(count)
    )


def test_a_large_change_is_split_below_the_limit():
    messages = render_availability(AvailabilityDiff(added=many_slots(600)))

    assert len(messages) > 1
    assert all(len(message) <= MAX_TELEGRAM_TEXT for message in messages)


def test_nothing_is_lost_in_the_split():
    slots = many_slots(600)
    messages = render_availability(AvailabilityDiff(added=slots))

    joined = "\n".join(messages)
    for entry in slots:
        assert f"+ {entry.start_time.strftime('%H:%M')}" in joined
    # Every date still appears, and every chunk still says what it is.
    assert all(message.startswith("🟢 З'явилися нові слоти") for message in messages)
    assert all("ТСЦ МВС №3242" in message for message in messages)


def test_a_split_never_cuts_a_time_range_in_half():
    messages = render_availability(
        AvailabilityDiff(added=many_slots(600, centre="3242"))
    )

    for message in messages:
        for line in message.splitlines():
            if line.startswith("+"):
                assert len(line.split()[1]) in (5, 11)  # HH:MM or HH:MM–HH:MM


def test_the_split_is_deterministic():
    slots = many_slots(600)
    first = render_availability(AvailabilityDiff(added=slots))
    second = render_availability(AvailabilityDiff(added=tuple(reversed(slots))))

    assert first == second


def test_many_centres_split_on_centre_boundaries():
    slots = tuple(
        entry
        for index in range(12)
        for entry in many_slots(60, centre=f"{3242 + index}")
    )
    messages = render_availability(AvailabilityDiff(added=slots))

    assert len(messages) > 1
    assert all(len(message) <= MAX_TELEGRAM_TEXT for message in messages)
    joined = "\n".join(messages)
    for index in range(12):
        assert f"ТСЦ МВС №{3242 + index}" in joined


# --------------------------------------------------------------------------- #
# The dispatcher
# --------------------------------------------------------------------------- #


def scan(
    *, availability: AvailabilityDiff | None = None, transition: Any = None, code: int = 0
) -> HeadlessScan:
    return HeadlessScan(code=code, transition=transition, availability=availability)


def transition(previous: Any, current: MonitorStatus, reason: str = "") -> MonitorStateTransition:
    return MonitorStateTransition(previous=previous, current=current, reason=reason)


def test_a_changed_availability_is_announced():
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(
        scan(availability=AvailabilityDiff(added=(slot("3242", AUG_26, "09:18"),)))
    )

    assert sent == 1
    assert len(post.calls) == 2  # one message, two recipients
    assert "З'явилися нові слоти" in post.texts[0]


def test_a_baseline_run_announces_nothing():
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(scan(availability=AvailabilityDiff()))

    assert sent == 0
    assert post.calls == []


def test_a_scan_that_compared_nothing_announces_nothing():
    """A partial or refused scan hands over ``None``, and None is not news."""
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(scan(availability=None))

    assert sent == 0
    assert post.calls == []


@pytest.mark.parametrize("previous", [MonitorStatus.READY, None])
def test_entering_auth_required_is_announced_once_per_recipient(previous):
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(
        scan(
            transition=transition(previous, MonitorStatus.AUTH_REQUIRED, "HTTP 401 Unauthorized"),
            code=3,
        )
    )

    assert sent == 1
    assert len(post.calls) == 2
    assert post.chat_ids == [111111111, 222222222]
    assert "Потрібна повторна авторизація" in post.texts[0]
    assert "Причина: HTTP 401 Unauthorized" in post.texts[0]


def test_a_gated_run_announces_nothing():
    """The second, third and hundredth AUTH_REQUIRED run: no transition, silence."""
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(scan(transition=None, code=3))

    assert sent == 0
    assert post.calls == []


def test_staying_in_auth_required_announces_nothing():
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(
        scan(
            transition=transition(MonitorStatus.AUTH_REQUIRED, MonitorStatus.AUTH_REQUIRED),
            code=3,
        )
    )

    assert sent == 0
    assert post.calls == []


def test_recovering_announces_nothing_in_this_task():
    bot, post = notifier()
    sent = NotificationDispatcher(bot).notify_scan(
        scan(transition=transition(MonitorStatus.AUTH_REQUIRED, MonitorStatus.READY))
    )

    assert sent == 0
    assert post.calls == []


@pytest.mark.parametrize(
    "status", [MonitorStatus.RATE_LIMITED, MonitorStatus.SERVICE_UNAVAILABLE]
)
def test_service_states_are_announced_on_transition(status):
    """State transitions to RATE_LIMITED and SERVICE_UNAVAILABLE send a notification."""
    bot, post = notifier(recipients=(111111111,))
    sent = NotificationDispatcher(bot).notify_scan(
        scan(transition=transition(MonitorStatus.READY, status, "HTTP 502"))
    )

    assert sent == 1
    assert len(post.calls) == 1


def test_availability_and_auth_in_one_scan_send_both():
    bot, post = notifier(recipients=(111111111,))
    sent = NotificationDispatcher(bot).notify_scan(
        scan(
            availability=AvailabilityDiff(added=(slot("3242", AUG_26, "09:18"),)),
            transition=transition(MonitorStatus.READY, MonitorStatus.AUTH_REQUIRED),
        )
    )

    assert sent == 2
    assert "нові слоти" in post.texts[0]
    assert "авторизація" in post.texts[1]


def test_without_a_notifier_nothing_happens_at_all():
    dispatcher = NotificationDispatcher(None)

    assert not dispatcher.enabled
    assert dispatcher.notify_scan(
        scan(availability=AvailabilityDiff(added=(slot("3242", AUG_26, "09:18"),)))
    ) == 0


def test_a_delivery_failure_never_reaches_the_caller(caplog):
    caplog.set_level(logging.WARNING)
    bot, post = notifier(FakeResponse(500), FakeResponse(500))

    sent = NotificationDispatcher(bot).notify_scan(
        scan(availability=AvailabilityDiff(added=(slot("3242", AUG_26, "09:18"),)))
    )

    assert sent == 1  # it tried, and said so
    assert len(post.calls) == 2
    assert bot.delivered == 0
    assert "delivery failed" in caplog.text


# --------------------------------------------------------------------------- #
# The whole path
# --------------------------------------------------------------------------- #


def monitor_once(tmp_path, *, server, store, monkeypatch, post) -> tuple[int, Recorder]:

    from hsc_queue_monitor.cli import run_monitor_once

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_USERS", "111111111,222222222")
    monkeypatch.setattr(
        "hsc_queue_monitor.notifications.telegram.http_post", post, raising=True
    )

    from test_api_monitor import monitor_config as _config

    code = run_monitor_once(
        _config(tmp_path),
        centers=["3242"],
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=server,
        emit=lambda _text: None,
    )
    return code, post


def test_a_401_notifies_once_and_the_gated_run_after_it_notifies_not_at_all(
    tmp_path, monkeypatch
):
    from test_api_availability import ApiServer
    from test_api_monitor import FakeStore, stored_session

    post = Recorder()
    store = FakeStore(stored=stored_session())
    refused = ApiServer(statuses={"departments": 401}, content_type="text/html")

    code, _post = monitor_once(
        tmp_path, server=refused, store=store, monkeypatch=monkeypatch, post=post
    )

    # monitor-once always returns 0, even for AUTH_REQUIRED
    assert code == 0
    assert len(post.calls) == 2  # one message, both recipients
    assert "Потрібна повторна авторизація" in post.texts[0]

    # The next scheduled run: the gate stops it before any request, and the
    # phone stays quiet.
    again = ApiServer(days=[])
    second, _post = monitor_once(
        tmp_path, server=again, store=store, monkeypatch=monkeypatch, post=post
    )

    assert second == 0  # still 0
    assert again.requests == []
    assert len(post.calls) == 2  # unchanged: nothing new was sent


def test_a_403_notifies_the_same_way(tmp_path, monkeypatch):
    from test_api_monitor import FakeStore, ForbiddenServer, stored_session

    post = Recorder()
    store = FakeStore(stored=stored_session())

    code, _post = monitor_once(
        tmp_path,
        server=ForbiddenServer(days=[]),
        store=store,
        monkeypatch=monkeypatch,
        post=post,
    )

    # monitor-once always returns 0, even for AUTH_REQUIRED
    assert code == 0
    assert "Потрібна повторна авторизація" in post.texts[0]


def test_a_new_slot_notifies_but_the_baseline_before_it_did_not(tmp_path, monkeypatch):
    from test_api_availability import ApiServer
    from test_api_monitor import FakeStore, days_for, stored_session

    post = Recorder()
    store = FakeStore(stored=stored_session())
    one = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [{"startTime": "08:26:00", "stopTime": "08:52:00"}]},
    )

    monitor_once(tmp_path, server=one, store=store, monkeypatch=monkeypatch, post=post)
    assert post.calls == []  # the baseline said nothing

    two = ApiServer(
        days=days_for("2026-08-26"),
        slots={
            "2026-08-26T00:00:00": [
                {"startTime": "08:26:00", "stopTime": "08:52:00"},
                {"startTime": "09:18:00", "stopTime": "09:44:00"},
            ]
        },
    )
    monitor_once(tmp_path, server=two, store=store, monkeypatch=monkeypatch, post=post)

    assert len(post.calls) == 2
    assert "+ 09:18–09:44" in post.texts[0]
    assert "08:26" not in post.texts[0]  # only what changed


def test_a_telegram_failure_leaves_hsc_state_alone(tmp_path, monkeypatch):
    from test_api_availability import ApiServer
    from test_api_monitor import FakeStore, days_for, stored_session

    store = FakeStore(stored=stored_session())
    first = ApiServer(days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": []})
    monitor_once(
        tmp_path, server=first, store=store, monkeypatch=monkeypatch, post=Recorder()
    )

    server = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [{"startTime": "09:18:00", "stopTime": "09:44:00"}]},
    )
    post = Recorder(FakeResponse(429), FakeResponse(500))

    code, _post = monitor_once(
        tmp_path, server=server, store=store, monkeypatch=monkeypatch, post=post
    )

    # The scan is untouched by the delivery failing.
    assert code == 0
    assert store.state is not None and store.state.status is MonitorStatus.READY
    assert store.snapshots.snapshot is not None
    assert store.snapshots.snapshot.slot_count == 1
    # And no HSC request was repeated because Telegram was unhappy.
    assert server.endpoints == ["departments", "days", "slots"]


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def sources() -> list[Path]:
    return sorted(NOTIFICATIONS.glob("*.py"))


@pytest.mark.parametrize("path", sources(), ids=lambda p: p.name)
def test_the_bot_cannot_receive_anything(path):
    """What the code *does*, not what the docs say it does not do."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        # Docstrings explain the absence of these; they are not calls.
        and not _is_docstring(tree, node)
    }
    used = {name.lower() for name in _identifiers(path)}

    for forbidden in (
        "getupdates",
        "setwebhook",
        "deletewebhook",
        "answercallbackquery",
        "webhook",
        "polling",
        "offset",
    ):
        assert not [text for text in literals if forbidden in text], path.name
        assert not [name for name in used if forbidden in name], path.name


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = getattr(parent, "body", [])
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and first.value is node:
                return True
    return False


def test_no_http_server_is_started_anywhere():
    for path in sources():
        used = _identifiers(path)
        for forbidden in ("serve", "listen", "bind", "flask", "aiohttp", "uvicorn"):
            assert not [name for name in used if forbidden in name.lower()]


def test_only_one_telegram_endpoint_is_ever_called():
    source = (NOTIFICATIONS / "telegram.py").read_text(encoding="utf-8")

    assert source.count('SEND_MESSAGE: Final = "sendMessage"') == 1
    assert source.count("SEND_MESSAGE") == 2  # the constant, and the URL it builds


def test_telegram_may_post_but_hsc_still_may_not():
    """The GET-only rule is about HSC, and it has not moved."""
    telegram = (NOTIFICATIONS / "telegram.py").read_text(encoding="utf-8")
    assert "requests.post" in telegram

    import re

    for path in (SRC / "api").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"\b(session|requests|client|http|fetch)\.(post|put|patch|delete)\s*\(", source
        ), path.name


def test_the_notifications_package_reaches_no_browser():
    from test_headless_monitor import BROWSER_NAMES, import_closure

    for path in sources():
        closure = import_closure(path)
        for module, imported in closure.items():
            for name in imported:
                assert not any(b in name.lower() for b in BROWSER_NAMES), module.name


def test_nothing_here_books_anything():
    for path in sources():
        used = _identifiers(path)
        for forbidden in ("book", "reserve", "submit", "slot_select"):
            assert not [name for name in used if forbidden in name.lower()]


def test_the_older_browser_notifier_is_untouched():
    """`notification/` (singular) belongs to the browser monitor and stays as is."""
    from hsc_queue_monitor.notification.telegram import TelegramNotifier as Older

    assert Older is not TelegramNotifier
    source = (SRC / "notification" / "telegram.py").read_text(encoding="utf-8")
    assert "TELEGRAM_USERS" not in source


def _identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
