"""Discover the service centres HSC offers, and write them into config.

One request — ``GET /api/v2/equeue/departments?serviceId=47`` — turned into
``config/service_centers.yaml``. No days, no slots, no browser, no booking.

Three things this module is careful about.

**The visible number is not the internal id.** ``ТСЦ МВС № 3242`` is what a
person sees and what the config is keyed by; the API's own ``id`` for that
centre has been observed as both 2 and 100 on different days. The internal id is
therefore never written down — every scan resolves it again from this same
endpoint.

**Discovery does not own the whole file.** A centre the API no longer returns is
kept, not deleted; a centre the user enabled stays enabled; fields this module
knows nothing about are copied through untouched. Discovery is authoritative
about *what exists*, and about nothing else.

**Nothing is written unless everything was read.** A refusal, a timeout or an
unrecognised schema leaves the file exactly as it was: a half-discovered
catalogue is worse than yesterday's complete one.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from ..models import HscMonitorError
from .availability import Department, parse_departments
from .client import HscApiClient
from .headless_monitor import (
    AUTH_REQUIRED_KINDS,
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_PERSISTENCE,
    REFRESH_COMMAND,
)
from .probe import DEFAULT_TIMEOUT, Fetch
from .retry import RetryConfig
from .session_store import (
    SessionPersister,
    SessionStore,
    jar_fingerprint,
    session_from_cookies,
)

logger = logging.getLogger(__name__)

#: The API answered, but not with something that can be turned into a catalogue.
EXIT_API: Final = 5

#: The top-level key of ``service_centers.yaml``. The existing schema, kept.
CONFIG_KEY: Final = "service_centers"

#: Fields discovery owns. Everything else in an entry belongs to whoever put it
#: there and is copied through untouched.
DISCOVERED_FIELDS: Final = ("name",)

#: ``ТСЦ МВС № 3242`` — the number is read only where the site marks one. An
#: unrelated digit run in an address is not a centre number, and guessing one
#: would put a wrong centre in the config under a right-looking key.
_CENTRE_NUMBER = re.compile(r"№\s*(\d{3,6})\b")

HEADER = """\
# Service centres, addressed by their visible service centre ID.
#
# Generated/updated by:
#   python -m hsc_queue_monitor.cli init-config
#
# `id` is the identity: it is what the site shows, what the search box is given
# and what every scan matches on. The internal HSC department id is deliberately
# NOT stored — it has been observed to change between runs, and it is resolved
# from /api/v2/equeue/departments on every scan.
#
# `enabled: true` selects a centre for monitoring. Discovery adds new centres
# disabled: HSC returns service centres across the whole country, and scanning
# all of them is not what this project is for.
"""


class DiscoveryFailed(HscMonitorError):
    """The catalogue could not be read, so the configuration is not touched."""


# --------------------------------------------------------------------------- #
# Reading the API's names
# --------------------------------------------------------------------------- #


def centre_number(name: str) -> str | None:
    """``ТСЦ МВС № 3242 м. Біла Церква`` -> ``"3242"``. Otherwise ``None``.

    Conservative on purpose: the number has to be marked as one by ``№``, and
    the name has to carry exactly one such number. Anything else is reported as
    unresolved rather than guessed at.
    """
    found = {match.group(1) for match in _CENTRE_NUMBER.finditer(name or "")}
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True, slots=True)
class DiscoveredCentre:
    """One centre as the API describes it today."""

    centre_id: str
    name: str
    #: Kept for logs and for the summary. Never written to the config.
    department_id: int = 0


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one departments response says exists."""

    centres: tuple[DiscoveredCentre, ...] = ()
    #: Departments whose name carries no single, marked centre number.
    unresolved: tuple[str, ...] = ()
    #: Visible numbers that two departments both claimed.
    duplicates: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.centres)


def discover(departments: Iterable[Department]) -> Discovery:
    """Map departments onto visible centre numbers, keeping the doubts visible."""
    centres: dict[str, DiscoveredCentre] = {}
    unresolved: list[str] = []
    duplicates: list[str] = []

    for department in departments:
        number = centre_number(department.display_name)
        if number is None:
            unresolved.append(f"id={department.department_id} {department.display_name}")
            continue
        if number in centres:
            # Two departments answering to one visible number: a scan would
            # refuse to pick between them, so say so rather than pick here.
            duplicates.append(number)
            continue
        centres[number] = DiscoveredCentre(
            centre_id=number,
            name=department.display_name,
            department_id=department.department_id,
        )

    return Discovery(
        centres=tuple(sorted(centres.values(), key=lambda c: _order(c.centre_id))),
        unresolved=tuple(unresolved),
        duplicates=tuple(sorted(set(duplicates), key=_order)),
    )


def _order(centre_id: str) -> tuple[int, str]:
    """Numeric where possible, so 1241 sorts before 3242 and diffs stay stable."""
    return (int(centre_id), "") if centre_id.isdigit() else (10**9, centre_id)


# --------------------------------------------------------------------------- #
# Merging with what is already there
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Merge:
    """The file to write, and what changed on the way to it."""

    entries: tuple[dict[str, Any], ...] = ()
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)


def read_existing(path: Path) -> list[dict[str, Any]]:
    """The centres already configured, as raw mappings.

    Raw on purpose: entries may carry fields this module has never heard of, and
    they have to survive a rewrite untouched.
    """
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise DiscoveryFailed(f"{path} does not contain a mapping at the top level.")
    entries = loaded.get(CONFIG_KEY) or []
    if not isinstance(entries, list):
        raise DiscoveryFailed(f"{path}: `{CONFIG_KEY}:` must be a list.")
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def merge(
    existing: Sequence[Mapping[str, Any]], discovery: Discovery, *, force: bool = False
) -> Merge:
    """Discovered identities, user decisions, and everything else preserved.

    Without ``force``: names come from the API, ``enabled`` and every unknown
    field come from the file, and a configured centre the API did not return is
    kept exactly as it is.

    With ``force``: the discovered entries are rebuilt from the API alone, which
    also resets ``enabled`` to false. Centres the API did not return are still
    kept — discovery never deletes.
    """
    by_id = {str(entry.get("id", "")).strip(): dict(entry) for entry in existing}
    discovered_ids = {centre.centre_id for centre in discovery.centres}

    entries: list[dict[str, Any]] = []
    added: list[str] = []
    updated: list[str] = []

    for centre in discovery.centres:
        previous = by_id.get(centre.centre_id)
        if previous is None:
            entries.append(
                {"id": centre.centre_id, "name": centre.name, "enabled": False}
            )
            added.append(centre.centre_id)
            continue

        entry = {"id": centre.centre_id, "name": centre.name, "enabled": False}
        if not force:
            # The user's fields win; only what discovery owns is refreshed.
            entry = {**previous, "id": centre.centre_id, "name": centre.name}
        entries.append(entry)
        if entry != previous:
            updated.append(centre.centre_id)

    retained = [
        centre_id for centre_id in by_id if centre_id and centre_id not in discovered_ids
    ]
    entries += [by_id[centre_id] for centre_id in retained]

    entries.sort(key=lambda entry: _order(str(entry.get("id", ""))))
    return Merge(
        entries=tuple(entries),
        added=tuple(added),
        updated=tuple(updated),
        retained=tuple(sorted(retained, key=_order)),
    )


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    # Anything structured is handed back to PyYAML rather than hand-rolled.
    return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()


def render_config(entries: Sequence[Mapping[str, Any]]) -> str:
    """The whole file, stable enough that an unchanged catalogue is an empty diff."""
    lines = [HEADER, f"{CONFIG_KEY}:"]
    if not entries:
        lines[-1] = f"{CONFIG_KEY}: []"

    for entry in entries:
        # A fixed order for the fields we know, then whatever else was there.
        known = [key for key in ("id", "name", "full_name", "enabled") if key in entry]
        rest = [key for key in entry if key not in known]
        first, *others = known + rest
        lines.append(f"  - {first}: {_scalar(entry[first])}")
        lines += [f"    {key}: {_scalar(entry[key])}" for key in others]
    return "\n".join(lines) + "\n"


def write_atomically(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory, then rename.

    A crash leaves either the old file or the new one. Truncated YAML would take
    the whole configuration down, and the centre list is not worth that risk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def run_config_init(
    store: SessionStore,
    *,
    output: Path,
    dry_run: bool = False,
    force: bool = False,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retry: RetryConfig | None = None,
    fetch: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> int:
    """Read the catalogue once, merge it into the config, write it atomically."""
    emit("\nHSC CONFIG INITIALIZATION\n")

    try:
        stored = store.load()
    except HscMonitorError as exc:
        emit(f"CONFIG INIT FAILED\n\n{exc}\n")
        return EXIT_PERSISTENCE

    if stored is None or stored.expired():
        why = (
            "No persisted HSC session is available."
            if stored is None
            else "The persisted HSC session has expired."
        )
        emit(
            f"CONFIG INIT FAILED\n\n{why}\nRun locally:\n\n  {REFRESH_COMMAND}\n"
        )
        return EXIT_PERSISTENCE

    emit("Loaded persisted HSC session")
    client = HscApiClient(
        session_from_cookies(stored.cookies, user_agent=stored.user_agent),
        timeout=timeout,
        retry=retry,
        fetch=fetch,
        sleep=sleep,
    )
    # The same persister the monitors use: a departments response can refresh
    # the queue cookie, and this run should leave it fresher than it found it.
    client.on_response = SessionPersister(
        store,
        created_at=stored.created_at,
        fingerprint=jar_fingerprint(stored.cookies),
    )

    try:
        call = client.departments()
    finally:
        client.close()

    if call.outcome.kind in AUTH_REQUIRED_KINDS:
        emit(
            "\nAUTH REQUIRED\n\n"
            "Persisted HSC session is no longer accepted.\n"
            f"Run locally:\n\n  {REFRESH_COMMAND}\n"
        )
        return EXIT_AUTH_REQUIRED

    if not call.ok:
        # 429, a final 502, a timeout, a non-JSON body: nothing is written,
        # because a catalogue is only worth replacing when it is complete.
        emit(
            f"\nDISCOVERY FAILED\n\n{call.label}: {call.outcome.kind}\n"
            f"{call.outcome.verdict}\n\nThe configuration was not modified.\n"
        )
        return EXIT_API

    try:
        departments = parse_departments(call.outcome.payload)
        existing = read_existing(output)
    except HscMonitorError as exc:
        emit(f"\nDISCOVERY FAILED\n\n{exc}\n\nThe configuration was not modified.\n")
        return EXIT_API

    discovery = discover(departments)
    if not discovery.centres:
        emit(
            "\nDISCOVERY FAILED\n\nNo department name carried a service centre "
            "number, so there is nothing to configure.\nThe configuration was "
            "not modified.\n"
        )
        return EXIT_API

    merged = merge(existing, discovery, force=force)
    emit(render_summary(discovery, merged, output=output, dry_run=dry_run, force=force))

    if dry_run:
        return EXIT_OK

    write_atomically(output, render_config(merged.entries))
    return EXIT_OK


def render_summary(
    discovery: Discovery,
    merged: Merge,
    *,
    output: Path,
    dry_run: bool,
    force: bool = False,
) -> str:
    lines = [
        "",
        f"Discovered: {discovery.total} service centres",
        f"Added:      {len(merged.added)}",
        f"Updated:    {len(merged.updated)}",
        f"Retained:   {len(merged.retained)}",
        f"Unresolved: {len(discovery.unresolved)}",
    ]
    if discovery.duplicates:
        lines.append(f"Duplicates: {len(discovery.duplicates)}")

    for centre_id in merged.retained:
        lines.append(
            f"\nExisting centre {centre_id} was not returned by HSC and was retained."
        )
    for name in discovery.unresolved:
        lines.append(f"\nNo centre number in: {name}")
    for centre_id in discovery.duplicates:
        lines.append(
            f"\nTwo departments both claim centre {centre_id}; a scan will refuse "
            "to choose between them."
        )
    if force:
        lines.append(
            "\n--force: discovered entries were rebuilt from the API, which also "
            "reset `enabled` to false. Centres HSC did not return were still kept."
        )

    lines += ["", "Dry run — configuration was not modified." if dry_run else "Written:"]
    if not dry_run:
        lines.append(f"  {output}")
    lines.append("")
    return "\n".join(lines)


#: Named for the boundary test: this path may reach these, and nothing else.
ALLOWED_DEPENDENCIES: Final[frozenset[str]] = frozenset(
    {
        "..models",
        ".availability",
        ".client",
        ".headless_monitor",
        ".probe",
        ".session_store",
    }
)
