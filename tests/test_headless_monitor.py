"""The headless half: MongoDB in, HTTP out, no browser anywhere.

Two things are tested here that matter more than the feature does.

The **boundary**: the module that GitHub Actions runs must not be able to reach
a browser at all. That is checked by walking the whole import graph, not by
reading the imports at the top of one file.

The **refusal**: a scheduled run that cannot read availability has to say so and
stop, with an exit code that tells the difference between "nothing is free",
"the session needs refreshing" and "the database is unreachable". Silently
carrying on is how a monitor becomes a thing nobody trusts.
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_api_availability import ApiServer, RefusingServer, TimingOutServer
from test_api_monitor import (
    CENTRE_A,
    CENTRE_B,
    EMPTY_DAYS,
    SLOT_0826,
    SLOT_0918,
    FakeStore,
    days_for,
    monitor_config,
    stored_session,
)
from test_api_probe import FakeHttpResponse

from hsc_queue_monitor.api.headless_monitor import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_PERSISTENCE,
    EXIT_RATE_LIMITED,
    EXIT_SERVICE_UNAVAILABLE,
    HeadlessScan,
    render_check,
    run_headless_scan,
    status_of,
)
from hsc_queue_monitor.api.monitor_state import MonitorStatus
from hsc_queue_monitor.api.probe import WIZARD_COOKIE

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "hsc-monitor.yml"


class Printed:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def __call__(self, text: str) -> None:
        self.blocks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)


def scan(
    *,
    store: FakeStore | None = None,
    server: ApiServer | None = None,
    centres: tuple[str, ...] = (CENTRE_A,),
    **kwargs: Any,
) -> tuple[HeadlessScan, Printed, FakeStore, ApiServer]:
    fake_store = store if store is not None else FakeStore(stored=stored_session())
    api = server if server is not None else ApiServer(days=EMPTY_DAYS)
    printed = Printed()
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("state_store", fake_store.states)
    kwargs.setdefault("snapshots", fake_store.snapshots)
    result = run_headless_scan(
        fake_store, centres, fetch=api, emit=printed, **kwargs
    )
    return result, printed, fake_store, api


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #


BROWSER_NAMES = (
    "playwright",
    "browsermanager",
    "browsersessionprovider",
    "authmanager",
    "queuepage",
    "loginpage",
    "native_files",
    "nativefileselector",
    "macos_ax",
    "chromium",
)


def module_path(module: str) -> Path | None:
    """``.client`` -> the file it lives in, relative to the api package."""
    relative = module.lstrip(".").replace(".", "/")
    for candidate in (SRC / f"{relative}.py", SRC / "api" / f"{relative}.py"):
        if candidate.exists():
            return candidate
    return None


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(f"{'.' * node.level}{node.module or ''}")
    return names


def import_closure(start: Path) -> dict[Path, set[str]]:
    """Every project module reachable from *start*, and what each imports."""
    seen: dict[Path, set[str]] = {}
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        found = imports_of(current)
        seen[current] = found
        for name in found:
            resolved = module_path(name)
            if resolved is not None and resolved not in seen:
                pending.append(resolved)
    return seen


def test_no_browser_is_reachable_from_the_headless_path():
    """Not "does this file import playwright" — "can this file get to it"."""
    closure = import_closure(SRC / "api" / "headless_monitor.py")

    assert len(closure) > 3, "the import graph was not walked"
    for path, imported in closure.items():
        for name in imported:
            assert not any(
                browser in name.lower() for browser in BROWSER_NAMES
            ), f"{path.name} imports {name}"

    # And the modules themselves are only the ones this path is meant to need.
    assert {path.name for path in closure} <= {
        "headless_monitor.py",
        "monitor.py",
        "availability.py",
        "client.py",
        "probe.py",
        "bootstrap.py",
        "session_store.py",
        "monitor_state.py",
        "availability_snapshot.py",
        "retry.py",
        "models.py",
        "logging_config.py",
    }


def test_no_browser_symbol_is_named_anywhere_in_the_headless_module():
    source = (SRC / "api" / "headless_monitor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    for name in used:
        assert not any(browser in name.lower() for browser in BROWSER_NAMES)


def test_the_headless_module_cannot_book_or_notify():
    """Identifiers, not prose: a docstring may say "books", code may not."""
    tree = ast.parse((SRC / "api" / "headless_monitor.py").read_text(encoding="utf-8"))
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    # STATUS_BOOKABLE is a word about availability, not an action, so the
    # check is on verbs the module could *call*.
    callable_names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("telegram", "notif", "book", "reserve", "submit", "post", "delete"):
        assert not [name for name in callable_names if forbidden in name.lower()]
        assert not [name for name in used if forbidden in name.lower() and name.isupper() is False]


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


def test_a_stored_session_is_used_and_nothing_is_launched():
    result, printed, store, server = scan()

    assert result.code == EXIT_OK
    assert server.endpoints == ["departments", "days"]
    # The stored cookies are the ones that went out.
    assert server.cookies_seen[0][WIZARD_COOKIE] == "stored-queue-session-NEVER-LOG-ME"
    # A first, unchanged run says nothing: the snapshot is the baseline.
    assert printed.text.strip() == ""
    assert store.snapshots.saved


def test_no_stored_session_asks_for_a_local_refresh():
    result, printed, _store, server = scan(store=FakeStore(stored=None))

    assert result.code == EXIT_AUTH_REQUIRED
    assert server.requests == []  # nothing was even attempted
    assert "AUTH REQUIRED" in printed.text
    assert "No HSC session is stored in MongoDB." in printed.text
    assert "python -m hsc_queue_monitor.cli refresh-session" in printed.text


def test_an_expired_session_is_not_even_tried():
    store = FakeStore(
        stored=stored_session(
            queue_session_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    result, printed, store_after, server = scan(store=store)

    assert result.code == EXIT_AUTH_REQUIRED
    assert server.requests == []
    assert "expired at" in printed.text
    # The document is left alone: there is no browser here to replace it with.
    assert store_after.deletes == 0
    assert store_after.stored is not None


def test_an_unreadable_database_is_its_own_exit_code():
    result, printed, _store, server = scan(store=FakeStore(fails_load=True))

    assert result.code == EXIT_PERSISTENCE
    assert server.requests == []
    assert "PERSISTENCE ERROR" in printed.text
    assert "AUTH REQUIRED" not in printed.text  # a different problem entirely


# --------------------------------------------------------------------------- #
# 403 and the other statuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "phrase"), [(401, "Unauthorized"), (403, "Forbidden")]
)
def test_a_refused_session_stops_with_the_auth_required_code(status, phrase):
    """Measured: an ~11-minute-old session came back 401, not 403."""
    store = FakeStore(stored=stored_session())
    server = ApiServer(statuses={"departments": status}, content_type="text/html")
    result, printed, store_after, _server = scan(store=store, server=server)

    assert result.code == EXIT_AUTH_REQUIRED
    assert "AUTH REQUIRED" in printed.text
    assert "Persisted HSC session is no longer accepted." in printed.text
    assert f"HSC answered: HTTP {status} {phrase}" in printed.text
    assert "python -m hsc_queue_monitor.cli refresh-session" in printed.text

    # One request: nothing is retried, and nothing was opened to retry it with.
    assert len(server.requests) == 1
    # And the stored document is untouched — this process cannot tell a dead
    # session from a momentarily unhappy server.
    assert store_after.deletes == 0
    assert store_after.stored is not None


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_session_is_never_reported_as_a_normal_scan(status):
    server = ApiServer(statuses={"departments": status}, content_type="text/html")
    result, printed, _store, _server = scan(server=server)

    assert result.code != EXIT_OK
    assert "Status: PARTIAL" not in printed.text
    assert "HSC AVAILABILITY CHECK" not in printed.text


def test_an_exhausted_rate_limit_is_rate_limited_not_auth_required():
    server = ApiServer(statuses={"departments": 429}, content_type="text/html")
    result, printed, store, _server = scan(server=server)

    assert result.code == EXIT_RATE_LIMITED
    assert "AUTH REQUIRED" not in printed.text
    assert "HSC SERVICE STATUS: RATE_LIMITED" in printed.text
    assert store.state is not None and store.state.status is MonitorStatus.RATE_LIMITED
    assert store.state.retry_after_at is not None  # a window to wait out
    assert len(server.requests) == 3  # the attempt budget, then given up on


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_an_exhausted_server_failure_is_service_unavailable(status):
    server = ApiServer(statuses={"departments": status}, content_type="text/html")
    result, printed, store, _server = scan(server=server)

    assert result.code == EXIT_SERVICE_UNAVAILABLE
    assert "AUTH REQUIRED" not in printed.text
    assert "HSC SERVICE STATUS: SERVICE_UNAVAILABLE" in printed.text
    assert f"HTTP {status}" in printed.text
    assert store.state is not None
    assert store.state.status is MonitorStatus.SERVICE_UNAVAILABLE
    # Temporary, so no window: the next scheduled run simply tries again.
    assert store.state.retry_after_at is None


def test_an_exhausted_timeout_is_service_unavailable():
    server = TimingOutServer(timeout_from="", days=days_for("2026-08-26"))
    result, printed, store, _server = scan(server=server)

    assert result.code == EXIT_SERVICE_UNAVAILABLE
    assert "AUTH REQUIRED" not in printed.text
    assert store.state is not None
    assert store.state.status is MonitorStatus.SERVICE_UNAVAILABLE
    assert "ReadTimeout" in store.state.reason


def test_an_empty_response_is_a_service_state_not_an_auth_one():
    """204 is a state answer, not a refusal: it does not mean "refresh me"."""

    class NoContent(ApiServer):
        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> Any:
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    result, printed, store_after, _server = scan(server=NoContent(days=EMPTY_DAYS))

    assert result.code == EXIT_SERVICE_UNAVAILABLE
    assert "AUTH REQUIRED" not in printed.text
    assert "partial — HTTP 204" in printed.text
    assert store_after.deletes == 0
    assert store_after.state is not None
    assert store_after.state.status is MonitorStatus.SERVICE_UNAVAILABLE


def test_the_headless_auth_set_is_its_own_policy():
    """Not shared with the browser monitor's: they answer different questions."""
    from hsc_queue_monitor.api.headless_monitor import AUTH_REQUIRED_KINDS
    from hsc_queue_monitor.api.monitor import AUTH_RECOVERY_KINDS

    assert {"unauthorized", "forbidden"} == AUTH_REQUIRED_KINDS
    # A 401 stops a scheduled run; it still never earns a browser locally.
    assert {"forbidden"} == AUTH_RECOVERY_KINDS
    assert AUTH_RECOVERY_KINDS < AUTH_REQUIRED_KINDS


def test_a_403_after_a_retry_still_asks_for_a_refresh():
    """A retryable failure followed by an answer: the answer is what counts."""
    from test_api_availability import Scripted, responds

    fetch = Scripted(responds(502, b'"gateway"'), responds(403, b'"no"'))
    store = FakeStore(stored=stored_session())
    printed = Printed()
    result = run_headless_scan(
        store,
        [CENTRE_A],
        state_store=store.states,
        fetch=fetch,
        emit=printed,
        sleep=lambda _s: None,
    )

    assert fetch.calls == 2  # retried once, then answered, and no third attempt
    assert result.code == EXIT_AUTH_REQUIRED
    assert store.deletes == 0
    assert store.state is not None and store.state.status is MonitorStatus.AUTH_REQUIRED


def test_a_recovered_transient_failure_is_just_a_normal_scan():
    """One 502, then an answer: the run succeeds and the state is READY."""

    class FlakyOnce(ApiServer):
        """502s the very first attempt, then behaves."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.attempts = 0

        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                return FakeHttpResponse(
                    502, {"Content-Type": "application/json"}, b'"gateway"'
                )
            return super().__call__(session, url, timeout)

    server = FlakyOnce(days=EMPTY_DAYS)
    result, printed, store, _server = scan(server=server)

    assert server.attempts == 3  # departments twice (502, 200), then days
    assert result.code == EXIT_OK
    assert store.state is not None and store.state.status is MonitorStatus.READY
    assert store.state.last_success_at is not None


# --------------------------------------------------------------------------- #
# The scan itself
# --------------------------------------------------------------------------- #


def test_two_empty_centres_cost_three_reads():
    result, _printed, _store, server = scan(centres=(CENTRE_A, CENTRE_B))

    assert server.endpoints == ["departments", "days", "days"]
    assert result.code == EXIT_OK


def test_slots_are_read_only_where_there_are_dates():
    server = ApiServer(
        days=days_for("2026-08-26"), slots={"2026-08-26T00:00:00": [SLOT_0826]}
    )
    _result, printed, store, _server = scan(server=server, centres=(CENTRE_A,))

    assert server.endpoints == ["departments", "days", "slots"]
    # Read, and remembered — but a baseline is not an announcement.
    assert store.snapshots.saved[-1].slot_count == 1
    assert printed.text.strip() == ""


def test_the_refreshed_cookie_is_written_back():
    store = FakeStore(stored=stored_session())
    _result, _printed, store_after, _server = scan(
        store=store,
        server=ApiServer(
            days=days_for("2026-08-26"),
            slots={"2026-08-26T00:00:00": [SLOT_0826]},
            sets={
                "departments": "after-departments",
                "days": "after-days",
                "slots": "after-slots",
            },
        ),
    )

    written = [
        {c["name"]: c["value"] for c in save.cookies}[WIZARD_COOKIE]
        for save in store_after.saves
    ]
    assert written == ["after-departments", "after-days", "after-slots"]


def test_an_unchanged_jar_is_not_written():
    store = FakeStore(stored=stored_session())
    scan(store=store, server=ApiServer(days=EMPTY_DAYS, sets={}))

    assert store.saves == []


def test_a_write_failure_never_changes_the_result(caplog):
    caplog.set_level(logging.WARNING)
    store = FakeStore(stored=stored_session(), fails_save=True)
    result, printed, _store, _server = scan(store=store)

    assert result.code == EXIT_OK
    assert "could not be written back" in printed.text
    assert "Could not persist the HSC session" in caplog.text


def test_the_shared_pacer_is_used_between_dates():
    waits: list[float] = []
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    server = ApiServer(
        days=days_for("2026-08-26", "2026-08-27"),
        slots={
            "2026-08-26T00:00:00": [SLOT_0826],
            "2026-08-27T00:00:00": [SLOT_0918],
        },
    )
    run_headless_scan(
        FakeStore(stored=stored_session()),
        [CENTRE_A],
        fetch=server,
        emit=lambda _text: None,
        slot_interval=3.0,
        sleep=sleep,
        clock=lambda: clock["now"],
    )

    assert waits == [3.0]  # one gap between two dates, none before the first


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def test_a_successful_run_says_nothing_when_nothing_changed():
    """The whole point: silence, not a five-minute heartbeat."""
    _result, printed, _store, _server = scan(centres=(CENTRE_A, CENTRE_B))

    assert printed.text.strip() == ""
    for noise in ("HSC AVAILABILITY CHECK", "no availability", "No changes", "Status:"):
        assert noise not in printed.text


def test_the_full_availability_is_no_longer_printed_every_run():
    server = ApiServer(
        days=days_for("2026-08-26"),
        slots={"2026-08-26T00:00:00": [SLOT_0826, SLOT_0918]},
    )
    _result, printed, store, _server = scan(server=server, centres=(CENTRE_A, CENTRE_B))

    # Two centres, four slots between them, and not one of them printed.
    assert store.snapshots.saved[-1].slot_count == 4
    assert "08:26-08:52" not in printed.text


def test_a_partial_centre_is_named_as_such():
    server = RefusingServer(
        refuse_from="2026-08-26T00:00:00", days=days_for("2026-08-26")
    )
    _result, printed, _store, _server = scan(server=server)

    assert f"{CENTRE_A}: partial — HTTP 429 Too Many Requests" in printed.text
    assert "Status: PARTIAL" in printed.text


def test_status_precedence_never_hides_an_incomplete_read():
    from hsc_queue_monitor.api.monitor import CentreReading

    empty = CentreReading(centre_id=CENTRE_A, complete=True)
    partial = CentreReading(centre_id=CENTRE_B, complete=False, detail="HTTP 429")

    assert status_of([empty, empty]) == "OK"
    assert status_of([empty, partial]) == "PARTIAL"
    assert render_check([empty, partial]).count("partial —") == 1


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_command_needs_persistence_configured(tmp_path, monkeypatch, capsys):
    from hsc_queue_monitor.cli import EXIT_CONFIG, run_monitor_once

    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    result = run_monitor_once(monitor_config(tmp_path), centers=[CENTRE_A])

    assert result == EXIT_CONFIG
    assert "cannot authenticate" in capsys.readouterr().err


def test_monitor_once_returns_zero_for_all_operational_outcomes(tmp_path):
    """monitor-once returns 0 for all operational outcomes in GitHub Actions."""
    from hsc_queue_monitor.cli import run_monitor_once

    store = FakeStore(stored=None)
    result = run_monitor_once(
        monitor_config(tmp_path),
        centers=[CENTRE_A],
        store=store,
        fetch=ApiServer(days=EMPTY_DAYS),
        emit=lambda _text: None,
    )

    # Always returns 0, even when AUTH_REQUIRED internally
    assert result == 0
    assert store.closed


def test_the_command_is_synchronous_and_declares_no_browser():
    import inspect

    from hsc_queue_monitor.cli import build_parser, cmd_monitor_once, run_monitor_once

    assert not inspect.iscoroutinefunction(run_monitor_once)
    assert not inspect.iscoroutinefunction(cmd_monitor_once)

    parser = build_parser()
    args = parser.parse_args(["monitor-once"])
    assert args.needs_browser is False
    assert args.func is cmd_monitor_once


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_runs_every_five_minutes(workflow):
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflow.get("on") or workflow[True]
    assert triggers["schedule"] == [{"cron": "*/5 * * * *"}]
    assert "workflow_dispatch" in triggers


def test_the_workflow_never_runs_two_scans_at_once(workflow):
    assert workflow["concurrency"]["group"] == "hsc-availability-monitor"
    # A queued run is better than a replaced one: cancelling mid-scan could
    # leave the persisted jar behind the server's.
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_workflow_cannot_hang_forever(workflow):
    job = workflow["jobs"]["check-availability"]
    assert 10 <= job["timeout-minutes"] <= 20


def commands_of(workflow: dict[str, Any]) -> str:
    """What the runner actually executes. Comments are not instructions."""
    job = workflow["jobs"]["check-availability"]
    return " ".join(str(step.get("run", "")) for step in job["steps"])


def env_of(workflow: dict[str, Any]) -> dict[str, str]:
    """Every environment variable the runner is handed, across all steps."""
    job = workflow["jobs"]["check-availability"]
    handed: dict[str, str] = dict(workflow.get("env") or {})
    handed.update(job.get("env") or {})
    for step in job["steps"]:
        handed.update(step.get("env") or {})
    return {str(k): str(v) for k, v in handed.items()}


def test_the_workflow_runs_one_scan_and_exits(workflow):
    commands = commands_of(workflow)

    assert "monitor-once" in commands
    assert "api-monitor" not in commands  # not the long-running local mode
    for looping in ("while", "for ", "sleep", "--once"):
        assert looping not in commands
    # And no browser is ever installed on the runner.
    assert "playwright install" not in commands


def test_the_workflow_takes_its_credentials_from_secrets(workflow_text):
    assert "${{ secrets.HSC_MONGODB_URI }}" in workflow_text
    assert "${{ secrets.HSC_SESSION_ENCRYPTION_KEY }}" in workflow_text
    # Nothing that looks like an actual value.
    assert "mongodb+srv://" not in workflow_text
    assert "mongodb://" not in workflow_text


@pytest.mark.parametrize(
    "forbidden",
    [
        "IDGOV_SIGNING_KEY_PATH",
        "IDGOV_SIGNING_KEY_PASSWORD",
        ".dat",
        "browser-profile",
        "playwright install",
    ],
)
def test_the_runner_is_never_given_the_signing_key(workflow, forbidden):
    """The security boundary: what is *handed to* the runner, not what is said.

    A comment explaining that the MasterKey stays local is the opposite of a
    violation, so this reads the environment and the commands rather than the
    file's prose.
    """
    handed = env_of(workflow)
    assert not [name for name in handed if forbidden.lower() in name.lower()]
    assert not [value for value in handed.values() if forbidden.lower() in value.lower()]
    assert forbidden.lower() not in commands_of(workflow).lower()


def test_the_runner_gets_what_it_needs_and_no_more(workflow):
    """Four values, all of them secrets. Nothing operational is passed at all."""
    handed = env_of(workflow)

    assert set(handed) == {
        "HSC_MONGODB_URI",
        "HSC_SESSION_ENCRYPTION_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_USERS",
    }
    for name, value in handed.items():
        assert value == "${{ secrets." + name + " }}", name

    # Still nothing that could authenticate: the runner gets a bot, not a key.
    assert not [name for name in handed if "KEY_" in name or "PASSWORD" in name]


def test_the_recipient_list_is_a_secret_not_a_repository_variable(workflow_text):
    """Telegram ids identify people, so they are never a `vars.` lookup."""
    assert "${{ secrets.TELEGRAM_USERS }}" in workflow_text
    assert "vars.TELEGRAM_USERS" not in workflow_text
    assert "vars." not in workflow_text


def test_the_job_runs_in_the_production_environment(workflow):
    """Which is where all four secrets are defined, and the only place."""
    assert workflow["jobs"]["check-availability"]["environment"] == "production"


@pytest.mark.parametrize(
    "operational",
    [
        "HSC_MONGODB_DATABASE",
        "HSC_MONGODB_COLLECTION",
        "HSC_MONITOR_INTERVAL_SECONDS",
        "HSC_READ_TIMEOUT_SECONDS",
        "HSC_SLOT_REQUEST_INTERVAL_SECONDS",
        "HSC_POLL_INTERVAL_SECONDS",
        "HSC_HEADLESS",
        "TELEGRAM_CHAT_ID",
    ],
)
def test_operational_settings_are_not_configured_twice(workflow, operational):
    """They live in config/app.yaml, which the checkout already provides.

    Two sources of truth for a timeout is how a runner ends up behaving
    differently from the machine it was tested on.
    """
    assert operational not in env_of(workflow)


def test_the_workflow_reads_its_settings_from_the_committed_file(workflow_text):
    """A sanity check on the pairing: app.yaml exists and the job checks out."""
    assert (PROJECT_ROOT / "config" / "app.yaml").exists()
    assert "actions/checkout" in workflow_text


def test_the_workflow_python_matches_the_project(workflow):
    import tomllib

    job = workflow["jobs"]["check-availability"]
    setup = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python")
    )
    version = str(setup["with"]["python-version"])

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = project["project"]["requires-python"]
    assert requires.startswith(">=")
    assert tuple(map(int, version.split("."))) >= tuple(
        map(int, requires.removeprefix(">=").split("."))
    )
