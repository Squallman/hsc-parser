"""`monitor`: the long-running headless loop, and what it must not change.

The loop adds exactly one thing to `monitor-once` — the wait between
iterations — and nothing here should be able to say otherwise. So most of
these tests are really about `monitor-once` staying untouched: the same
function, the same operational rules (AUTH_REQUIRED, persistence, no second
retry layer), called repeatedly with a wait in between that is itself never
concurrent with a scan.

Sleep is always faked. Nothing here waits five minutes, or any minutes.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import signal
import sys
from typing import Any

import pytest
from test_api_availability import ApiServer
from test_api_monitor import CENTRE_A, EMPTY_DAYS, FakeStore, monitor_config, stored_session

from hsc_queue_monitor.cli import (
    EXIT_CONFIG,
    EXIT_OK,
    build_parser,
    cmd_browser_monitor,
    cmd_monitor,
    cmd_monitor_once,
    run_monitor_loop,
    run_monitor_once,
)
from hsc_queue_monitor.config import DEFAULT_MONITOR_INTERVAL_SECONDS, ApiConfig
from hsc_queue_monitor.models import ConfigError

ONE_SCAN = ["departments", "days"]  # EMPTY_DAYS: one centre, no dates to expand


def store_for(tmp_path: Any) -> FakeStore:
    return FakeStore(stored=stored_session())


def loop_kwargs(store: FakeStore, server: ApiServer) -> dict[str, Any]:
    return dict(
        centers=[CENTRE_A],
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=server,
    )


class Emitted:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def counting_sleep(events: list[str], *, stop_after: int) -> tuple[Any, list[float]]:
    """Records the interval it was called with, and stops the loop like Ctrl+C
    would — by letting a `KeyboardInterrupt` come out of the wait — after
    `stop_after` calls."""
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        events.append("sleep")
        if len(calls) >= stop_after:
            raise KeyboardInterrupt

    return sleep, calls


def marking_server(events: list[str], **kwargs: Any) -> tuple[Any, ApiServer]:
    """An `ApiServer` wrapped in a plain function that appends "scan" once per
    scan (on `departments`, which every scan calls exactly once), so scans
    and sleeps can be interleaved in one shared timeline.

    Returns ``(fetch, server)``: pass ``fetch`` as the client's fetch
    callable, and read ``server.endpoints``/``.requests`` for assertions —
    wrapping a plain function around the server, rather than monkeypatching
    ``__call__`` on the instance, because ``obj(...)`` dispatches through the
    *type*, not the instance, so an instance-level override would silently
    never run.
    """
    server = ApiServer(**kwargs)

    def fetch(session: Any, url: str, timeout: Any = (5, 60)) -> Any:
        response = server(session, url, timeout)
        if server.endpoints[-1] == "departments":
            events.append("scan")
        return response

    return fetch, server


# --------------------------------------------------------------------------- #
# monitor-once is unchanged
# --------------------------------------------------------------------------- #


def test_monitor_once_still_runs_exactly_one_scan(tmp_path):
    store = store_for(tmp_path)
    server = ApiServer(days=EMPTY_DAYS)

    result = run_monitor_once(
        monitor_config(tmp_path),
        centers=[CENTRE_A],
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=server,
        emit=lambda _text: None,
    )

    assert result == EXIT_OK
    assert server.endpoints == ONE_SCAN


def test_monitor_once_command_still_wired_as_before():
    parser = build_parser()
    args = parser.parse_args(["monitor-once"])

    assert args.func is cmd_monitor_once
    assert args.needs_browser is False
    assert not inspect.iscoroutinefunction(run_monitor_once)
    assert not inspect.iscoroutinefunction(cmd_monitor_once)


# --------------------------------------------------------------------------- #
# monitor: repeated scans, sequential, on the configured interval
# --------------------------------------------------------------------------- #


def test_monitor_repeats_scans_sequentially_on_the_configured_interval(tmp_path):
    config = monitor_config(tmp_path)
    config = dataclasses.replace(
        config,
        app=dataclasses.replace(
            config.app,
            api=dataclasses.replace(config.app.api, monitor_interval_seconds=45.0),
        ),
    )
    store = store_for(tmp_path)
    events: list[str] = []
    fetch, server = marking_server(events, days=EMPTY_DAYS)
    sleep, calls = counting_sleep(events, stop_after=3)
    emitted = Emitted()

    result = run_monitor_loop(
        config,
        sleep=sleep,
        emit=emitted,
        install_sigterm_handler=False,
        **loop_kwargs(store, fetch),
    )

    assert result == EXIT_OK
    # Three full scans, three waits, strictly alternating — never a scan
    # starting before the previous wait (and scan) finished.
    assert events == ["scan", "sleep", "scan", "sleep", "scan", "sleep"]
    assert server.endpoints == ONE_SCAN * 3
    assert calls == [45.0, 45.0, 45.0]
    assert "Starting HSC monitor loop with interval 45s" in emitted.lines
    assert "HSC monitor stopped" in emitted.lines


def test_default_interval_is_five_minutes(tmp_path):
    assert ApiConfig().monitor_interval_seconds == 300.0 == DEFAULT_MONITOR_INTERVAL_SECONDS

    config = monitor_config(tmp_path)  # api.monitor_interval_seconds not overridden
    store = store_for(tmp_path)
    server = ApiServer(days=EMPTY_DAYS)
    sleep, calls = counting_sleep([], stop_after=1)
    emitted = Emitted()

    run_monitor_loop(
        config, sleep=sleep, emit=emitted, install_sigterm_handler=False,
        **loop_kwargs(store, server),
    )

    assert calls == [300.0]
    assert "Starting HSC monitor loop with interval 300s" in emitted.lines


def test_no_nested_retry_layer_is_introduced(tmp_path):
    """The wait is always exactly the configured interval — nothing about a
    scan's own outcome ever changes what the loop sleeps for."""
    config = monitor_config(tmp_path)
    store = store_for(tmp_path)
    server = ApiServer(days=EMPTY_DAYS)
    sleep, calls = counting_sleep([], stop_after=4)

    run_monitor_loop(
        config, sleep=sleep, emit=lambda _t: None, install_sigterm_handler=False,
        **loop_kwargs(store, server),
    )

    assert calls == [300.0, 300.0, 300.0, 300.0]


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #


def test_ctrl_c_during_the_wait_exits_cleanly(tmp_path, caplog):
    config = monitor_config(tmp_path)
    store = store_for(tmp_path)
    server = ApiServer(days=EMPTY_DAYS)
    sleep, calls = counting_sleep([], stop_after=1)
    emitted = Emitted()

    result = run_monitor_loop(
        config, sleep=sleep, emit=emitted, install_sigterm_handler=False,
        **loop_kwargs(store, server),
    )

    assert result == EXIT_OK
    assert server.endpoints == ONE_SCAN  # exactly one scan before the interrupt
    assert "HSC monitor stopped" in emitted.lines
    assert "Unexpected error" not in caplog.text


def test_ctrl_c_during_a_scan_is_not_reported_as_an_hsc_error(tmp_path, caplog):
    """A KeyboardInterrupt is a BaseException: it must fall straight through
    `monitor-once`'s own `except Exception` and be caught only by the loop —
    never logged as "Unexpected error during monitor-once scan"."""
    config = monitor_config(tmp_path)
    store = store_for(tmp_path)

    def interrupting_fetch(_session: Any, _url: str, _timeout: Any = (5, 60)) -> Any:
        raise KeyboardInterrupt

    emitted = Emitted()
    sleep_calls: list[float] = []

    result = run_monitor_loop(
        config,
        centers=[CENTRE_A],
        store=store,
        state_store=store.states,
        snapshots=store.snapshots,
        fetch=interrupting_fetch,
        sleep=sleep_calls.append,
        emit=emitted,
        install_sigterm_handler=False,
    )

    assert result == EXIT_OK
    assert sleep_calls == []  # the interrupt happened before any wait
    assert "HSC monitor stopped" in emitted.lines
    assert "Unexpected error during monitor-once scan" not in caplog.text


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signal delivery")
def test_sigterm_exits_cleanly(tmp_path):
    config = monitor_config(tmp_path)
    store = store_for(tmp_path)
    server = ApiServer(days=EMPTY_DAYS)
    emitted = Emitted()
    previous = signal.getsignal(signal.SIGTERM)

    def send_sigterm(_seconds: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    result = run_monitor_loop(
        config, sleep=send_sigterm, emit=emitted, install_sigterm_handler=True,
        **loop_kwargs(store, server),
    )

    assert result == EXIT_OK
    assert server.endpoints == ONE_SCAN
    assert "HSC monitor stopped" in emitted.lines
    # The handler installed for the duration of the loop is gone afterward.
    assert signal.getsignal(signal.SIGTERM) == previous


def test_monitor_loop_is_synchronous_and_declares_no_browser():
    assert not inspect.iscoroutinefunction(run_monitor_loop)
    assert not inspect.iscoroutinefunction(cmd_monitor)

    parser = build_parser()
    args = parser.parse_args(["monitor"])
    assert args.func is cmd_monitor
    assert args.needs_browser is False


# --------------------------------------------------------------------------- #
# A broken config stops the loop instead of retrying it forever
# --------------------------------------------------------------------------- #


def test_a_config_failure_stops_the_loop_without_ever_sleeping(tmp_path, monkeypatch):
    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    config = monitor_config(tmp_path)

    def sleep_should_not_be_called(_seconds: float) -> None:
        raise AssertionError("the loop slept after a configuration failure")

    result = run_monitor_loop(
        config,
        centers=[CENTRE_A],
        sleep=sleep_should_not_be_called,
        emit=lambda _t: None,
        install_sigterm_handler=False,
    )

    assert result == EXIT_CONFIG


# --------------------------------------------------------------------------- #
# Config validation (already owned by ApiConfig.from_dict; pinned here too)
# --------------------------------------------------------------------------- #


def test_zero_monitor_interval_fails_clearly():
    with pytest.raises(ConfigError, match="greater than zero"):
        ApiConfig.from_dict({"monitor_interval_seconds": 0})


def test_negative_monitor_interval_fails_clearly():
    with pytest.raises(ConfigError, match="greater than zero"):
        ApiConfig.from_dict({"monitor_interval_seconds": -5})


def test_omitted_monitor_interval_defaults_to_300_seconds():
    assert ApiConfig.from_dict({}).monitor_interval_seconds == 300.0


# --------------------------------------------------------------------------- #
# The renamed browser-driven command still works under its new name
# --------------------------------------------------------------------------- #


def test_browser_monitor_keeps_the_old_behavior_under_a_new_name():
    parser = build_parser()
    args = parser.parse_args(["browser-monitor"])

    assert args.func is cmd_browser_monitor
    assert args.needs_browser is True
    assert inspect.iscoroutinefunction(cmd_browser_monitor)
