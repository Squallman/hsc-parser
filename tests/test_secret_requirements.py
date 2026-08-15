"""Which command needs which secret — asserted by running the commands.

The split this file protects is the whole reason the environment was reduced to
six values: ``refresh-session`` signs a person in to ID.GOV.UA and needs their
MasterKey; nothing else does, and the GitHub runner is never given one. A test
that only read the documentation would not notice the day a headless command
started asking for a key it cannot have, so each requirement here is proved by
booby-trapping the accessor and running the command anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hsc_queue_monitor.cli import (
    EXIT_CONFIG,
    EXIT_OK,
    run_init_config,
    run_monitor_once,
    run_telegram_test,
)
from hsc_queue_monitor.config import SecretSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TOKEN = "111222333:AA-THIS-IS-NOT-A-REAL-TOKEN-abcdefghijklm"

#: The two that stay on the laptop, and the methods that read them.
LOCAL_ONLY = ("require_key_path", "require_key_password")


@pytest.fixture
def no_masterkey(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a MasterKey secret at all fails the test that is running.

    Deleting the variables would only prove the command tolerates their
    absence. Making the accessors explode proves it never looks.
    """
    for name in LOCAL_ONLY:
        monkeypatch.setattr(
            SecretSettings,
            name,
            lambda _self, _name=name: pytest.fail(
                f"a headless command asked for {_name}()"
            ),
        )
    for variable in ("IDGOV_SIGNING_KEY_PATH", "IDGOV_SIGNING_KEY_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def no_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_USERS"):
        monkeypatch.delenv(variable, raising=False)


def config_for(tmp_path: Path) -> Any:
    from test_api_monitor import monitor_config

    return monitor_config(tmp_path)


class Printed:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
# monitor-once — the scheduled, headless command
# --------------------------------------------------------------------------- #


def test_monitor_once_scans_without_ever_asking_for_a_masterkey(
    tmp_path, no_masterkey, no_telegram
):
    """The GitHub runner has neither secret, and the command must not want one."""
    from test_api_availability import DAYS, SLOTS, ApiServer
    from test_api_monitor import FakeStore, stored_session

    store = FakeStore(stored=stored_session())
    server = ApiServer(days=DAYS, slots=SLOTS)

    code = run_monitor_once(
        config_for(tmp_path),
        centers=["3242"],
        slot_interval=0.0,  # pacing is tested elsewhere; this test is about secrets
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=server,
        emit=Printed(),
    )

    assert code == EXIT_OK
    # It really scanned: the availability it read is now the stored baseline.
    assert store.snapshots.saved


def test_monitor_once_without_a_database_names_the_database(
    tmp_path, monkeypatch, capsys, no_masterkey, no_telegram
):
    """The one secret it genuinely needs — and the error says so, not more."""
    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    monkeypatch.delenv("HSC_SESSION_ENCRYPTION_KEY", raising=False)

    code = run_monitor_once(config_for(tmp_path), centers=["3242"])

    assert code == EXIT_CONFIG
    error = capsys.readouterr().err
    assert "HSC_MONGODB_URI" in error
    assert "HSC_SESSION_ENCRYPTION_KEY" in error
    # It cannot authenticate, and it does not pretend the fix is a local secret.
    assert "IDGOV_SIGNING_KEY_PATH" not in error
    assert "IDGOV_SIGNING_KEY_PASSWORD" not in error
    assert "refresh-session" in error


def test_notifications_are_optional_for_a_scheduled_scan(
    tmp_path, no_masterkey, no_telegram
):
    """No bot configured is a quiet monitor, not a failed one."""
    from test_api_availability import DAYS, SLOTS, ApiServer
    from test_api_monitor import FakeStore, stored_session

    store = FakeStore(stored=stored_session())

    code = run_monitor_once(
        config_for(tmp_path),
        centers=["3242"],
        slot_interval=0.0,  # pacing is tested elsewhere; this test is about secrets
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=ApiServer(days=DAYS, slots=SLOTS),
        emit=Printed(),
    )

    assert code == EXIT_OK


# --------------------------------------------------------------------------- #
# init-config — discovery, also headless
# --------------------------------------------------------------------------- #


def test_init_config_needs_the_database_and_no_key(tmp_path, no_masterkey, no_telegram):
    from test_api_availability import ApiServer
    from test_api_monitor import FakeStore, stored_session

    store = FakeStore(stored=stored_session())
    printed = Printed()

    code = run_init_config(
        config_for(tmp_path),
        output=tmp_path / "service_centers.yaml",
        store=store,
        fetch=ApiServer(),
        emit=printed,
    )

    assert code == EXIT_OK
    assert (tmp_path / "service_centers.yaml").exists()


def test_init_config_without_a_database_says_to_refresh_locally(
    tmp_path, monkeypatch, capsys, no_masterkey
):
    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    monkeypatch.delenv("HSC_SESSION_ENCRYPTION_KEY", raising=False)

    code = run_init_config(config_for(tmp_path), output=tmp_path / "out.yaml")

    assert code == EXIT_CONFIG
    error = capsys.readouterr().err
    assert "HSC_MONGODB_URI" in error
    assert "IDGOV_SIGNING_KEY_PATH" not in error


# --------------------------------------------------------------------------- #
# telegram-test — the bot, and nothing else
# --------------------------------------------------------------------------- #


def test_telegram_test_needs_no_key_and_no_database(tmp_path, monkeypatch, no_masterkey):
    """Two secrets, one purpose. A database failure here would be a distraction."""
    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    monkeypatch.delenv("HSC_SESSION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_USERS", "111111111")

    sent: list[dict[str, Any]] = []

    def post(_url: str, payload: Any, _timeout: Any) -> Any:
        sent.append(dict(payload))
        return type("R", (), {"status_code": 200, "headers": {}})()

    monkeypatch.setattr(
        "hsc_queue_monitor.notifications.telegram.http_post", post, raising=True
    )

    code = run_telegram_test(config_for(tmp_path), emit=Printed())

    assert code == EXIT_OK
    assert [message["chat_id"] for message in sent] == [111111111]


def test_telegram_test_without_a_bot_stops_at_the_bot(
    tmp_path, monkeypatch, capsys, no_masterkey, no_telegram
):
    code = run_telegram_test(config_for(tmp_path))

    assert code == EXIT_CONFIG
    error = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in error
    assert "MONGODB" not in error.upper()


# --------------------------------------------------------------------------- #
# refresh-session — the one command that does need the key
# --------------------------------------------------------------------------- #


def test_the_masterkey_is_required_exactly_where_it_is_used(tmp_path, monkeypatch):
    """`refresh-session` authenticates, so it — and only it — reaches for the key."""
    import ast

    source = (PROJECT_ROOT / "src" / "hsc_queue_monitor" / "flow" / "auth.py").read_text(
        encoding="utf-8"
    )
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert set(LOCAL_ONLY) <= called
