"""`refresh-session --dump-session`: the opt-in diagnostic session dump.

The feature exists to compare browser cookies, the bridged ``requests.Session``
and the plaintext Mongo payload side by side — see
:mod:`hsc_queue_monitor.api.session_dump`. Everything here is diagnostic-only
and must stay strictly opt-in, so most of these tests are really about absence:
no file unless asked, no secret in the file, no cookie value on stdout/stderr.
"""

from __future__ import annotations

import dataclasses
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from test_api_availability import ApiServer
from test_api_monitor import ProviderWithFakeBrowser, monitor_config
from test_api_probe import ACCESS_TOKEN_VALUE, CSRF_VALUE, EQUEUE_VALUE

from hsc_queue_monitor.api.session_store import NullSessionStore
from hsc_queue_monitor.cli import EXIT_OK, EXIT_PERSISTENCE, run_refresh_session

MINT_VALUE = "queue-session-NEVER-LOG-ME"
SECRET_COOKIE_VALUES = (ACCESS_TOKEN_VALUE, EQUEUE_VALUE, CSRF_VALUE, MINT_VALUE)


async def _refresh(
    tmp_path: Path,
    *,
    dump_session: Path | None = None,
    overwrite_session_dump: bool = False,
    config: Any = None,
) -> tuple[int, ProviderWithFakeBrowser]:
    harness = ProviderWithFakeBrowser(
        tmp_path,
        fetch=ApiServer(),
        mints=MINT_VALUE,
        capture_browser_cookies=dump_session is not None,
    )
    code = await run_refresh_session(
        config if config is not None else monitor_config(tmp_path),
        provider=harness,
        store=NullSessionStore(),
        dump_session=dump_session,
        overwrite_session_dump=overwrite_session_dump,
    )
    return code, harness


# --------------------------------------------------------------------------- #
# Opt-in only
# --------------------------------------------------------------------------- #


async def test_no_dump_file_by_default(tmp_path):
    before = set(tmp_path.iterdir())

    code, _harness = await _refresh(tmp_path)

    assert code == EXIT_OK
    assert set(tmp_path.iterdir()) == before  # nothing new on disk


async def test_dump_written_when_requested(tmp_path):
    target = tmp_path / "session-dump.json"

    code, _harness = await _refresh(tmp_path, dump_session=target)

    assert code == EXIT_OK
    assert target.exists()
    dump = json.loads(target.read_text(encoding="utf-8"))
    assert dump["dump_version"] == 1
    assert dump["source"] == "refresh-session"
    assert "created_at" in dump
    assert "user_agent" in dump
    assert isinstance(dump["cookies"], list) and dump["cookies"]
    assert isinstance(dump["headers"], dict)
    assert isinstance(dump["browser_cookies"], list) and dump["browser_cookies"]
    assert isinstance(dump["mongo_session_payload"], dict)


# --------------------------------------------------------------------------- #
# Overwrite protection
# --------------------------------------------------------------------------- #


async def test_existing_dump_is_not_overwritten_by_default(tmp_path, capsys):
    target = tmp_path / "session-dump.json"
    target.write_text("previous diagnostic run\n", encoding="utf-8")

    code, _harness = await _refresh(tmp_path, dump_session=target)

    assert code == EXIT_PERSISTENCE
    assert target.read_text(encoding="utf-8") == "previous diagnostic run\n"
    assert "--overwrite-session-dump" in capsys.readouterr().err


async def test_overwrite_flag_replaces_an_existing_dump(tmp_path):
    target = tmp_path / "session-dump.json"
    target.write_text("previous diagnostic run\n", encoding="utf-8")

    code, _harness = await _refresh(
        tmp_path, dump_session=target, overwrite_session_dump=True
    )

    assert code == EXIT_OK
    dump = json.loads(target.read_text(encoding="utf-8"))
    assert dump["dump_version"] == 1


# --------------------------------------------------------------------------- #
# Content: raw cookies show up, and only where they should
# --------------------------------------------------------------------------- #


async def test_raw_cookie_values_are_present_in_the_file(tmp_path):
    target = tmp_path / "session-dump.json"

    await _refresh(tmp_path, dump_session=target)

    raw = target.read_text(encoding="utf-8")
    # The bridged requests.Session jar, the raw browser jar and the plaintext
    # Mongo payload should each carry the queue-session value the fake
    # bootstrap minted.
    assert MINT_VALUE in raw
    assert ACCESS_TOKEN_VALUE in raw


async def test_raw_cookie_values_never_reach_stdout_or_stderr(tmp_path, capsys):
    target = tmp_path / "session-dump.json"

    await _refresh(tmp_path, dump_session=target)

    printed = capsys.readouterr()
    for secret in SECRET_COOKIE_VALUES:
        assert secret not in printed.out
        assert secret not in printed.err


async def test_the_warning_is_printed_before_writing(tmp_path, capsys):
    target = tmp_path / "session-dump.json"

    await _refresh(tmp_path, dump_session=target)

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "authentication credentials" in out


# --------------------------------------------------------------------------- #
# What must never appear, even though nothing here should carry it structurally
# --------------------------------------------------------------------------- #


async def test_signing_key_password_and_mongo_uri_are_absent(tmp_path):
    target = tmp_path / "session-dump.json"
    base = monitor_config(tmp_path)
    secretive = dataclasses.replace(
        base,
        secrets=dataclasses.replace(
            base.secrets,
            key_password="NEVER-LOG-ME-either-signing-key-password",
            mongodb_uri="mongodb+srv://hsc_user:sup3r-secret-pw@cluster0.example.mongodb.net/",
        ),
    )

    await _refresh(tmp_path, dump_session=target, config=secretive)

    raw = target.read_text(encoding="utf-8")
    assert "NEVER-LOG-ME-either-signing-key-password" not in raw
    assert "sup3r-secret-pw" not in raw
    assert "cluster0.example.mongodb.net" not in raw


# --------------------------------------------------------------------------- #
# File permissions
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions only")
async def test_the_dump_file_is_owner_only(tmp_path):
    target = tmp_path / "session-dump.json"

    await _refresh(tmp_path, dump_session=target)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


# --------------------------------------------------------------------------- #
# Same authenticated session, three representations
# --------------------------------------------------------------------------- #


async def test_browser_request_and_persistence_agree_on_the_session(tmp_path):
    target = tmp_path / "session-dump.json"

    await _refresh(tmp_path, dump_session=target)

    dump = json.loads(target.read_text(encoding="utf-8"))

    browser_names = {c["name"] for c in dump["browser_cookies"]}
    request_names = {c["name"] for c in dump["cookies"]}
    mongo_names = {c["name"] for c in dump["mongo_session_payload"]["cookies"]}

    # Every HSC cookie the requests.Session carries came from the same
    # browser context the browser_cookies section describes.
    assert request_names <= browser_names
    # And the plaintext Mongo payload is exactly the bridged jar, not a
    # separately reconstructed one.
    assert mongo_names == request_names
    assert dump["mongo_session_payload"]["user_agent"] == dump["user_agent"]


async def test_dump_is_not_created_when_bootstrap_fails(tmp_path):
    from test_api_availability import cookies_without_queue_session

    target = tmp_path / "session-dump.json"
    # No queue-session cookie to start with, and `mints=None`: the fake queue
    # navigation never creates one either — the same failure
    # `Queue bootstrap: FAILED` covers without this flag.
    harness = ProviderWithFakeBrowser(
        tmp_path,
        fetch=ApiServer(),
        mints=None,
        cookies=cookies_without_queue_session(),
        capture_browser_cookies=True,
    )

    code = await run_refresh_session(
        monitor_config(tmp_path),
        provider=harness,
        store=NullSessionStore(),
        dump_session=target,
    )

    assert code != EXIT_OK
    assert not target.exists()
