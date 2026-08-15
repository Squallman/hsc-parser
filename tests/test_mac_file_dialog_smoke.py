"""The file-row diagnostic in the smoke script.

Go to Folder turned out to be directory navigation only — given a full file
path it navigates to the parent and selects nothing. So the question moved to
"how is a file picked out of the list", and this diagnostic answers it by
reading. These tests hold it to that: it must find the right file, describe
what surrounds it, and touch nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from test_native_files import (  # noqa: I001 - see the path insert below
    FakeAxApi,
    node,
    opening_panel_app,
    panel_app,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import mac_file_dialog_smoke as smoke  # noqa: E402, I001

from hsc_queue_monitor.browser.native_files import MacOSFileSelector  # noqa: E402

BASENAME = "hsc-smoke-test.txt"


# --------------------------------------------------------------------------- #
# A panel showing a directory listing
# --------------------------------------------------------------------------- #


#: The shared panel fake already models row selection and settability.
ListingApi = FakeAxApi


def listing_app(names: list[str], *, selectable: bool = True) -> dict[str, Any]:
    """An Open panel showing a file list, with no Go to Folder sheet open.

    Built from the same fixtures production is tested against, so the shape
    here is the shape measured live: AXTextField in AXCell in AXRow, inside
    AXOutline "ListView".
    """
    return panel_app(files=names, selectable=selectable)


def selector_for(api: FakeAxApi) -> MacOSFileSelector:
    return MacOSFileSelector(api=api, appear_timeout_ms=600, close_timeout_ms=600)


# --------------------------------------------------------------------------- #
# Navigation goes to the directory
# --------------------------------------------------------------------------- #


async def test_the_parent_directory_is_what_reaches_go_to_folder(tmp_path):
    """A full path navigates to the parent and selects nothing — so send that."""
    target = tmp_path / BASENAME
    target.write_text("x", encoding="utf-8")

    api = FakeAxApi({1: opening_panel_app(files=[BASENAME])})

    await smoke.run_file_row_inspection(selector_for(api), target)

    written = [value for _, name, value in api.writes if name == "AXValue"]
    assert written == [str(tmp_path.resolve())]
    assert BASENAME not in written[0], "the filename must not be in the path"


async def test_the_return_machinery_is_untouched_by_the_diagnostic(tmp_path):
    target = tmp_path / BASENAME
    target.write_text("x", encoding="utf-8")

    api = FakeAxApi({62868: opening_panel_app(files=[BASENAME])})

    await smoke.run_file_row_inspection(selector_for(api), target)

    assert api.chords == [(5, True, True)], "⌘⇧G, once"
    assert api.returns == [62868], "one Return, to the panel process"


# --------------------------------------------------------------------------- #
# Finding the file
# --------------------------------------------------------------------------- #


async def test_the_exact_basename_is_found():
    api = ListingApi({1: listing_app(["other.txt", BASENAME, "notes.md"])})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)

    assert report.found is True
    assert report.elements[0].label == "filename element"
    assert report.elements[0].attributes["AXValue"] == BASENAME


async def test_a_similarly_named_file_does_not_match():
    """A prefix match would happily pick the wrong file."""
    api = ListingApi({1: listing_app([f"{BASENAME}.bak", f"copy-{BASENAME}"])})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)

    assert report.found is False
    assert "No element in the panel has AXValue" in report.render()


async def test_a_missing_file_is_reported_rather_than_approximated():
    api = ListingApi({1: listing_app(["something-else.txt"])})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)

    assert report.found is False
    assert report.elements == []


async def test_no_panel_at_all_is_reported():
    api = ListingApi({1: node("AXApplication", AXWindows=[], _id="idle")})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)

    assert report.found is False


# --------------------------------------------------------------------------- #
# What it reports
# --------------------------------------------------------------------------- #


async def test_the_cell_and_row_ancestry_is_captured():
    api = ListingApi({1: listing_app([BASENAME])})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)
    labels = [element.label for element in report.elements]

    assert labels == [
        "filename element", "AXCell ancestor", "AXRow ancestor", "AXOutline container"
    ]
    assert [element.role for element in report.elements] == [
        "AXTextField", "AXCell", "AXRow", "AXOutline"
    ]


async def test_selection_metadata_and_settability_are_reported():
    """Whether AXSelected can be *set* is the thing we do not yet know."""
    api = ListingApi({1: listing_app([BASENAME], selectable=True)})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)
    row = next(e for e in report.elements if e.label == "AXRow ancestor")

    assert row.attributes["AXSelected"] == "False"
    assert row.settable["AXSelected"] is True
    assert row.attributes["AXIndex"] == "1"
    assert row.action_names == ["AXPress"]


async def test_an_unsettable_selection_is_reported_as_such():
    api = ListingApi({1: listing_app([BASENAME], selectable=False)})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)
    row = next(e for e in report.elements if e.label == "AXRow ancestor")

    assert row.settable["AXSelected"] is False
    assert "settable=False" in row.render()


async def test_the_container_selection_attributes_are_reported():
    api = ListingApi({1: listing_app([BASENAME, "other.txt"])})

    report = await smoke.inspect_file_row(selector_for(api), BASENAME)
    container = next(e for e in report.elements if e.label.endswith("container"))

    assert container.attributes["AXRows"] == "2 element(s)"
    assert container.attributes["AXVisibleRows"] == "2 element(s)"
    assert container.attributes["AXSelectedRows"] == "0 element(s)"
    assert container.attributes["AXIdentifier"] == "ListView"


# --------------------------------------------------------------------------- #
# It touches nothing
# --------------------------------------------------------------------------- #


async def test_the_diagnostic_selects_nothing_and_presses_nothing():
    api = ListingApi({1: listing_app([BASENAME])})

    await smoke.inspect_file_row(selector_for(api), BASENAME)

    assert api.writes == [], "nothing was written"
    assert api.actions == [], "nothing was pressed"
    assert api.returns == [], "no Return for the file itself"


async def test_the_full_run_stops_after_navigating(tmp_path):
    """No Open press: pressing it before the file is selected would be wrong."""
    target = tmp_path / BASENAME
    target.write_text("x", encoding="utf-8")

    api = ListingApi({1: opening_panel_app(files=[BASENAME])})

    code = await smoke.run_file_row_inspection(selector_for(api), target)

    assert code == 0
    assert api.actions == [], "the OK button was never pressed"
    assert not any(name == "AXSelected" for _, name, _ in api.writes)
    # The panel is deliberately left open.
    assert selector_for(api).find_open_panel() is not None


def test_the_parser_accepts_the_inspection_flag():
    args = smoke.build_parser().parse_args(["--inspect-file-row", "--delay", "5"])

    assert args.inspect_file_row is True
    assert args.delay == 5.0


def test_the_smoke_script_is_macos_only(monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "linux")

    assert smoke.main([]) == 2
    assert "only works on macOS" in capsys.readouterr().err


def test_nothing_in_production_imports_the_smoke_script():
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        assert "mac_file_dialog_smoke" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize(
    "banned", ["AXPress", "AXSelected", "click", "double", "coordinate"]
)
def test_the_diagnostic_never_names_an_action_to_take(banned):
    """It reports what a row supports; it must not start using it."""
    import ast

    source = (PROJECT_ROOT / "scripts" / "mac_file_dialog_smoke.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "perform_action" not in calls
    assert "set_attribute_value" not in calls
    # AXSelected appears only in the read list, never as something acted on.
    assert f"perform_action({banned}" not in source
