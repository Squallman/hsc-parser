"""Service-centre discovery: one departments call turned into configuration.

The interesting parts are not the happy path. They are: what happens to a
configuration a human has been editing, what happens when the response is not
trustworthy, and what happens to the number in ``ТСЦ МВС № 3242`` — because
writing the *internal* department id into the config, or inventing a number from
a digit run in an address, would produce a file that looks right and monitors
the wrong centre.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_api_availability import ApiServer
from test_api_monitor import CENTRE_A, FakeStore, monitor_config, stored_session
from test_api_probe import FakeHttpResponse

from hsc_queue_monitor.api.availability import Department, parse_departments
from hsc_queue_monitor.api.config_init import (
    CONFIG_KEY,
    EXIT_API,
    Discovery,
    centre_number,
    discover,
    merge,
    read_existing,
    render_config,
    run_config_init,
    write_atomically,
)
from hsc_queue_monitor.api.headless_monitor import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_PERSISTENCE,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "hsc_queue_monitor"

#: A departments response shaped like the live one: the visible number lives in
#: the name, and the `id` is the API's own and differs from it.
CATALOGUE: list[dict[str, Any]] = [
    {"id": 2, "name": "ТСЦ МВС № 3242 м. Біла Церква", "allowOnlineCount": 1},
    {"id": 17, "name": "ТСЦ МВС № 1241 м. Київ, вул. Набережна 1", "allowOnlineCount": 0},
    {"id": 3242, "name": "ТСЦ МВС № 4641 м. Київ, вул. Лугова 19", "allowOnlineCount": 2},
]


class Printed:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def __call__(self, text: str) -> None:
        self.blocks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)


def init(
    tmp_path: Path,
    *,
    store: FakeStore | None = None,
    server: ApiServer | None = None,
    existing: str | None = None,
    **kwargs: Any,
) -> tuple[int, Printed, Path, FakeStore, ApiServer]:
    output = tmp_path / "service_centers.yaml"
    if existing is not None:
        output.write_text(existing, encoding="utf-8")

    fake_store = store if store is not None else FakeStore(stored=stored_session())
    api = server if server is not None else ApiServer(departments=CATALOGUE)
    printed = Printed()
    kwargs.setdefault("sleep", lambda _seconds: None)
    code = run_config_init(
        fake_store, output=output, fetch=api, emit=printed, **kwargs
    )
    return code, printed, output, fake_store, api


def written(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))[CONFIG_KEY]


# --------------------------------------------------------------------------- #
# Centre numbers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ТСЦ МВС № 3242", "3242"),
        ("ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20", "3242"),
        ("ТСЦ МВС №3242", "3242"),
        ("ТСЦ МВС №   3242", "3242"),
        ("ТСЦ МВС № 3242\n", "3242"),
    ],
)
def test_the_marked_number_is_the_centre_number(name, expected):
    assert centre_number(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "ТСЦ МВС м. Київ, вул. Лугова 19",  # an address, not a centre number
        "Регіональний сервісний центр 8043",  # a number, but not marked
        "",
        "ТСЦ МВС № 12",  # too short to be one
        "ТСЦ МВС № 3242 та № 4641",  # two marked numbers: which one?
    ],
)
def test_an_unmarked_or_ambiguous_number_is_not_guessed(name):
    assert centre_number(name) is None


def test_discovery_reports_what_it_could_not_map():
    departments = [
        Department(department_id=2, display_name="ТСЦ МВС № 3242 м. Біла Церква"),
        Department(department_id=9, display_name="Мобільний сервісний центр"),
    ]

    found = discover(departments)

    assert [c.centre_id for c in found.centres] == ["3242"]
    assert found.unresolved == ("id=9 Мобільний сервісний центр",)


def test_two_departments_claiming_one_number_are_reported():
    departments = [
        Department(department_id=2, display_name="ТСЦ МВС № 3242 (основний)"),
        Department(department_id=8, display_name="ТСЦ МВС № 3242 (тимчасовий)"),
    ]

    found = discover(departments)

    assert found.duplicates == ("3242",)
    assert len(found.centres) == 1  # one entry, and the conflict said out loud


def test_the_internal_department_id_is_never_the_centre_id():
    found = discover(parse_departments(CATALOGUE))

    ids = [centre.centre_id for centre in found.centres]
    assert ids == ["1241", "3242", "4641"]  # numeric order, visible numbers
    # The API's own ids — 2, 17, 3242 — are not what the config is keyed by.
    assert [c.department_id for c in found.centres] == [17, 2, 3242]
    assert "2" not in ids and "17" not in ids


# --------------------------------------------------------------------------- #
# Writing the file
# --------------------------------------------------------------------------- #


def test_every_discovered_centre_is_written_in_numeric_order(tmp_path):
    code, _printed, output, _store, _server = init(tmp_path)

    assert code == EXIT_OK
    assert [entry["id"] for entry in written(output)] == ["1241", "3242", "4641"]


def test_new_centres_arrive_disabled(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path)

    assert all(entry["enabled"] is False for entry in written(output))


def test_the_file_keeps_the_existing_schema(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path)

    loaded = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert set(loaded) == {CONFIG_KEY}
    assert isinstance(loaded[CONFIG_KEY], list)
    assert set(loaded[CONFIG_KEY][0]) == {"id", "name", "enabled"}
    # The header explains itself, and carries no timestamp to churn the diff.
    text = output.read_text(encoding="utf-8")
    assert "init-config" in text
    assert "resolved" in text and "department id" in text
    assert str(datetime.now(UTC).year) not in text


def test_no_internal_department_id_reaches_the_file(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path)

    text = output.read_text(encoding="utf-8")
    for internal in ("department_id", "departmentId", "id: 2\n", "id: 17"):
        assert internal not in text


def test_the_written_file_still_loads_as_configuration(tmp_path):
    from hsc_queue_monitor.config import load_service_centers

    _code, _printed, output, _store, _server = init(tmp_path)

    centres = load_service_centers(output)
    assert [c.id for c in centres] == ["1241", "3242", "4641"]
    assert all(not c.enabled for c in centres)


def test_the_output_is_stable_between_runs(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path)
    first = output.read_text(encoding="utf-8")

    init(tmp_path, existing=first)

    assert output.read_text(encoding="utf-8") == first


# --------------------------------------------------------------------------- #
# Not destroying what is already there
# --------------------------------------------------------------------------- #


EXISTING = """\
service_centers:
  - id: "3242"
    name: "ТСЦ МВС № 3242"
    full_name: "ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20"
    enabled: true
    note: "the one I actually want"
  - id: "9999"
    name: "ТСЦ МВС № 9999"
    enabled: true
"""


def test_an_enabled_centre_stays_enabled(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path, existing=EXISTING)

    entries = {entry["id"]: entry for entry in written(output)}
    assert entries["3242"]["enabled"] is True
    assert entries["1241"]["enabled"] is False  # newly discovered


def test_fields_discovery_knows_nothing_about_survive(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path, existing=EXISTING)

    entry = {e["id"]: e for e in written(output)}["3242"]
    assert entry["note"] == "the one I actually want"
    assert entry["full_name"].endswith("Сухоярська 20")
    # And the API's name is what refreshes.
    assert entry["name"] == "ТСЦ МВС № 3242 м. Біла Церква"


def test_a_centre_hsc_no_longer_returns_is_kept_and_reported(tmp_path):
    _code, printed, output, _store, _server = init(tmp_path, existing=EXISTING)

    entries = {entry["id"]: entry for entry in written(output)}
    assert "9999" in entries  # never silently deleted
    assert entries["9999"]["enabled"] is True
    assert (
        "Existing centre 9999 was not returned by HSC and was retained."
        in printed.text
    )


def test_force_rebuilds_the_discovered_entries_only(tmp_path):
    _code, printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, force=True
    )

    entries = {entry["id"]: entry for entry in written(output)}
    # The discovered portion is rebuilt from the API: enabled resets, extra
    # fields go.
    assert entries["3242"]["enabled"] is False
    assert "note" not in entries["3242"]
    # What HSC did not return is still kept, even with --force.
    assert entries["9999"]["enabled"] is True
    assert "reset `enabled` to false" in printed.text


def test_the_summary_counts_what_happened(tmp_path):
    _code, printed, _output, _store, _server = init(tmp_path, existing=EXISTING)

    assert "Discovered: 3 service centres" in printed.text
    assert "Added:      2" in printed.text
    assert "Updated:    1" in printed.text
    assert "Retained:   1" in printed.text
    assert "Unresolved: 0" in printed.text


# --------------------------------------------------------------------------- #
# Not writing when it should not
# --------------------------------------------------------------------------- #


def test_a_dry_run_changes_nothing(tmp_path):
    code, printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, dry_run=True
    )

    assert code == EXIT_OK
    assert output.read_text(encoding="utf-8") == EXISTING
    assert "Dry run — configuration was not modified." in printed.text
    assert "Discovered: 3 service centres" in printed.text


@pytest.mark.parametrize("status", [429, 500])
def test_a_refused_response_leaves_the_file_alone(tmp_path, status):
    server = ApiServer(statuses={"departments": status}, content_type="text/html")
    code, printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, server=server
    )

    assert code == EXIT_API
    assert output.read_text(encoding="utf-8") == EXISTING
    assert "DISCOVERY FAILED" in printed.text
    assert "The configuration was not modified." in printed.text


def test_an_unreadable_schema_leaves_the_file_alone(tmp_path):
    server = ApiServer(departments={"unexpected": True})
    code, _printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, server=server
    )

    assert code == EXIT_API
    assert output.read_text(encoding="utf-8") == EXISTING


def test_a_catalogue_with_no_readable_numbers_leaves_the_file_alone(tmp_path):
    server = ApiServer(departments=[{"id": 1, "name": "Мобільний сервісний центр"}])
    code, printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, server=server
    )

    assert code == EXIT_API
    assert output.read_text(encoding="utf-8") == EXISTING
    assert "nothing to configure" in printed.text


def test_the_file_is_replaced_atomically(tmp_path, monkeypatch):
    """A crash mid-write must leave the previous file, not half a new one."""
    output = tmp_path / "service_centers.yaml"
    output.write_text(EXISTING, encoding="utf-8")

    def explode(source: Any, target: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hsc_queue_monitor.api.config_init.os.replace", explode)

    with pytest.raises(OSError, match="disk full"):
        write_atomically(output, "half a file")

    assert output.read_text(encoding="utf-8") == EXISTING
    # And no temporary file was left lying about.
    assert [p.name for p in tmp_path.iterdir()] == ["service_centers.yaml"]


def test_writing_creates_the_directory_when_missing(tmp_path):
    target = tmp_path / "nested" / "service_centers.yaml"
    write_atomically(target, "service_centers: []\n")
    assert target.read_text(encoding="utf-8") == "service_centers: []\n"


# --------------------------------------------------------------------------- #
# Session and network
# --------------------------------------------------------------------------- #


def test_exactly_one_endpoint_is_called(tmp_path):
    _code, _printed, _output, _store, server = init(tmp_path)

    assert server.endpoints == ["departments"]
    assert "days" not in server.endpoints
    assert "slots" not in server.endpoints


def test_a_transient_failure_uses_the_clients_own_retry(tmp_path):
    """Inherited from HscApiClient, not reimplemented here."""
    from test_api_availability import Scripted, responds

    output = tmp_path / "service_centers.yaml"
    waits: list[float] = []
    fetch = Scripted(responds(502, b'"gateway"'), responds(200, json.dumps(CATALOGUE).encode()))
    code = run_config_init(
        FakeStore(stored=stored_session()),
        output=output,
        fetch=fetch,
        sleep=waits.append,
        emit=lambda _text: None,
    )

    assert code == EXIT_OK
    assert fetch.calls == 2  # retried once, then answered
    assert waits == [2.0]  # the shared policy's first backoff
    assert fetch.timeouts == [(5, 60), (5, 60)]  # one timeout, not two
    assert [entry["id"] for entry in written(output)] == ["1241", "3242", "4641"]


def test_a_refreshed_cookie_jar_is_persisted(tmp_path):
    from hsc_queue_monitor.api.probe import WIZARD_COOKIE

    store = FakeStore(stored=stored_session())
    init(tmp_path, store=store, server=ApiServer(departments=CATALOGUE))

    assert store.saves, "the refreshed session was not written back"
    saved = {c["name"]: c["value"] for c in store.saves[-1].cookies}
    assert saved[WIZARD_COOKIE] == "wizard-state-after-departments-NEVER-LOG-ME"


def test_an_unchanged_jar_is_not_written(tmp_path):
    store = FakeStore(stored=stored_session())
    init(tmp_path, store=store, server=ApiServer(departments=CATALOGUE, sets={}))

    assert store.saves == []


def test_no_session_material_reaches_the_configuration(tmp_path):
    _code, _printed, output, _store, _server = init(tmp_path)

    text = output.read_text(encoding="utf-8")
    for secret in ("cookie", "session", "token", "NEVER-LOG-ME", "mongodb"):
        assert secret.lower() not in text.lower()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_no_stored_session_asks_for_a_local_refresh(tmp_path):
    code, printed, output, _store, server = init(
        tmp_path, existing=EXISTING, store=FakeStore(stored=None)
    )

    assert code == EXIT_PERSISTENCE
    assert server.requests == []
    assert "CONFIG INIT FAILED" in printed.text
    assert "No persisted HSC session is available." in printed.text
    assert "python -m hsc_queue_monitor.cli refresh-session" in printed.text
    assert output.read_text(encoding="utf-8") == EXISTING


def test_an_expired_session_asks_for_a_local_refresh(tmp_path):
    store = FakeStore(
        stored=stored_session(
            queue_session_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    code, printed, _output, _store, server = init(tmp_path, store=store)

    assert code == EXIT_PERSISTENCE
    assert server.requests == []
    assert "refresh-session" in printed.text


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_session_stops_with_the_auth_required_code(tmp_path, status):
    server = ApiServer(statuses={"departments": status}, content_type="text/html")
    code, printed, output, store, _server = init(
        tmp_path, existing=EXISTING, server=server
    )

    assert code == EXIT_AUTH_REQUIRED
    assert "AUTH REQUIRED" in printed.text
    assert "Persisted HSC session is no longer accepted." in printed.text
    assert "refresh-session" in printed.text
    assert len(server.requests) == 1  # nothing retried
    assert store.deletes == 0  # nothing deleted
    assert output.read_text(encoding="utf-8") == EXISTING


def test_a_database_failure_is_its_own_code(tmp_path):
    code, printed, output, _store, server = init(
        tmp_path, existing=EXISTING, store=FakeStore(fails_load=True)
    )

    assert code == EXIT_PERSISTENCE
    assert server.requests == []
    assert output.read_text(encoding="utf-8") == EXISTING
    assert "CONFIG INIT FAILED" in printed.text


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #


def test_no_browser_is_reachable_from_discovery():
    from test_headless_monitor import BROWSER_NAMES, import_closure

    closure = import_closure(SRC / "api" / "config_init.py")

    assert len(closure) > 3
    for path, imported in closure.items():
        for name in imported:
            assert not any(
                browser in name.lower() for browser in BROWSER_NAMES
            ), f"{path.name} imports {name}"


def test_discovery_calls_no_other_endpoint():
    """Structural: the module names departments and nothing else."""
    tree = ast.parse((SRC / "api" / "config_init.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "departments" in called
    assert "days" not in called
    assert "slots" not in called
    for forbidden in ("post", "put", "patch", "delete"):
        assert forbidden not in called


def test_discovery_reuses_the_departments_parser():
    """One reading of this endpoint, not two."""
    source = (SRC / "api" / "config_init.py").read_text(encoding="utf-8")
    assert "parse_departments" in source
    # And the shared parser is what resolve_department uses too.
    availability = (SRC / "api" / "availability.py").read_text(encoding="utf-8")
    assert availability.count("def parse_departments") == 1
    assert "parse_departments(payload)" in availability


def test_the_command_is_synchronous_and_declares_no_browser():
    import inspect

    from hsc_queue_monitor.cli import build_parser, cmd_init_config, run_init_config

    assert not inspect.iscoroutinefunction(run_init_config)
    args = build_parser().parse_args(["init-config"])
    assert args.needs_browser is False
    assert args.func is cmd_init_config


def test_the_command_needs_persistence_configured(tmp_path, monkeypatch, capsys):
    from hsc_queue_monitor.cli import EXIT_CONFIG, run_init_config

    monkeypatch.delenv("HSC_MONGODB_URI", raising=False)
    assert run_init_config(monitor_config(tmp_path)) == EXIT_CONFIG
    assert "refresh-session" in capsys.readouterr().err


def test_the_command_passes_the_configured_output_through(tmp_path):
    from hsc_queue_monitor.cli import run_init_config

    target = tmp_path / "somewhere" / "centres.yaml"
    code = run_init_config(
        monitor_config(tmp_path),
        output=target,
        store=FakeStore(stored=stored_session()),
        fetch=ApiServer(departments=CATALOGUE),
        emit=lambda _text: None,
    )

    assert code == EXIT_OK
    assert [entry["id"] for entry in written(target)] == ["1241", "3242", "4641"]


def test_discovery_is_not_in_the_scheduled_workflow():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "hsc-monitor.yml").read_text(
        encoding="utf-8"
    )
    assert "init-config" not in workflow
    assert "monitor-once" in workflow


# --------------------------------------------------------------------------- #
# Small pieces
# --------------------------------------------------------------------------- #


def test_reading_a_missing_file_is_not_an_error(tmp_path):
    assert read_existing(tmp_path / "absent.yaml") == []


def test_reading_a_malformed_file_is_refused(tmp_path):
    path = tmp_path / "service_centers.yaml"
    path.write_text("service_centers: 3\n", encoding="utf-8")

    with pytest.raises(Exception, match="must be a list"):
        read_existing(path)


def test_rendering_an_empty_catalogue_is_still_valid_yaml():
    loaded = yaml.safe_load(render_config([]))
    assert loaded[CONFIG_KEY] == []


def test_merge_reports_nothing_changed_when_nothing_did():
    discovery = Discovery()
    result = merge([{"id": "3242", "name": "x", "enabled": True}], discovery)

    assert not result.changed
    assert result.retained == ("3242",)
    assert result.entries[0]["enabled"] is True


def test_an_empty_response_is_not_treated_as_a_catalogue(tmp_path):
    class NoContent(ApiServer):
        def __call__(self, session: Any, url: str, timeout: Any = (5, 60)) -> Any:
            super().__call__(session, url, timeout)
            return FakeHttpResponse(204, {}, b"")

    code, _printed, output, _store, _server = init(
        tmp_path, existing=EXISTING, server=NoContent()
    )

    assert code == EXIT_API
    assert output.read_text(encoding="utf-8") == EXISTING


def test_the_centre_id_the_scan_uses_is_the_one_written(tmp_path):
    """End to end: what discovery writes is what resolution matches on."""
    from hsc_queue_monitor.api.availability import resolve_department

    _code, _printed, output, _store, _server = init(tmp_path)

    for entry in written(output):
        department = resolve_department(CATALOGUE, entry["id"])
        assert entry["name"] == department.display_name
        assert str(department.department_id) != entry["id"] or entry["id"] == "3242"
    assert CENTRE_A in [entry["id"] for entry in written(output)]
