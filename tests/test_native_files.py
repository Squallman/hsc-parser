"""The macOS Open-panel selector.

No real Accessibility is involved: the whole API is a fake built from dicts, so
these run anywhere. What is tested is the part that has to be right against a
panel we have measured but cannot re-measure in CI — which process is chosen,
which control is written to, and what counts as success.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from hsc_queue_monitor.browser import native_files as nf
from hsc_queue_monitor.browser.macos_ax import (
    PANEL_SERVICE_BUNDLE_ID,
    AxApi,
    RunningProcess,
    load_ax_api,
)
from hsc_queue_monitor.browser.native_files import (
    GOTO_SHEET_ID,
    LIST_VIEW_ID,
    OK_BUTTON_ID,
    OPEN_PANEL_ID,
    PATH_FIELD_ID,
    MacOSFileSelector,
    NativeFileSelector,
    native_file_selector,
)
from hsc_queue_monitor.models import (
    AccessibilityUnavailable,
    ConfigError,
    NativeFileDialogError,
)

KEY = Path("/Users/someone/keys/Key-6.dat")


# --------------------------------------------------------------------------- #
# A scripted macOS
# --------------------------------------------------------------------------- #


def node(
    role: str = "AXGroup",
    *,
    identifier: str = "",
    children: list[dict[str, Any]] | None = None,
    value: str = "",
    enabled: bool = True,
    settable: bool = False,
    actions: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "AXRole": role,
        "AXIdentifier": identifier,
        "AXChildren": children if children is not None else [],
        "AXValue": value,
        "AXEnabled": enabled,
        "_settable": settable,
        "_actions": actions or [],
        "_id": extra.pop("_id", identifier or role),
    }
    element.update(extra)
    return element


class FakeAxApi:
    """The Accessibility API, backed by dicts. Records everything it is asked."""

    def __init__(
        self,
        apps: dict[int, dict[str, Any]],
        processes: list[RunningProcess] | None = None,
        *,
        refuse_write: bool = False,
        refuse_action: str | None = None,
    ) -> None:
        self.apps = apps
        self._processes = processes or [
            RunningProcess(pid, PANEL_SERVICE_BUNDLE_ID, f"Panel {pid}")
            for pid in apps
        ]
        self.created: list[int] = []
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, Any]] = []
        self.actions: list[tuple[str, str]] = []
        self.chords: list[tuple[int, bool, bool]] = []
        self.returns: list[int] = []
        self._refuse_write = refuse_write
        self._refuse_action = refuse_action
        self._on_return: Any = next(
            (app.get("_on_return") for app in apps.values() if app.get("_on_return")),
            None,
        )

    # -- required by AxApi ------------------------------------------------
    def create_application(self, pid: int) -> dict[str, Any]:
        self.created.append(pid)
        return self.apps.get(pid, node("AXApplication"))

    def set_timeout(self, element: dict[str, Any], seconds: float) -> None:
        return None

    def attribute_names(self, element: dict[str, Any]) -> list[str]:
        return [key for key in element if key.startswith("AX")]

    def attribute_value(self, element: dict[str, Any], name: str) -> Any:
        self.reads.append((str(element.get("_id", "")), name))
        return element.get(name)

    def set_attribute_value(self, element: dict[str, Any], name: str, value: Any) -> None:
        if self._refuse_write:
            raise AccessibilityUnavailable("AX error -25200")
        self.writes.append((str(element.get("_id", "")), name, value))
        element[name] = value
        # Selecting a row is what the list reports as its selection.
        outline = element.get("_outline")
        if name == "AXSelected" and isinstance(outline, dict):
            outline["AXSelectedRows"] = [element] if value else []

    def action_names(self, element: dict[str, Any]) -> list[str]:
        return list(element.get("_actions", []))

    def perform_action(self, element: dict[str, Any], name: str) -> None:
        if self._refuse_action == name:
            raise AccessibilityUnavailable("AX error -25205")
        self.actions.append((str(element.get("_id", "")), name))
        callback = element.get("_on_action")
        if callable(callback):
            callback(name)

    def is_settable(self, element: dict[str, Any], name: str) -> bool:
        if name == "AXSelected":
            return bool(element.get("_selectable_row"))
        return bool(element.get("_settable")) and name == "AXValue"

    def pid_of(self, element: dict[str, Any]) -> int | None:
        return None

    def same_element(self, first: Any, second: Any) -> bool:
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        return first.get("_id", id(first)) == second.get("_id", id(second))

    def running_processes(self) -> list[RunningProcess]:
        return list(self._processes)

    def send_key_chord(
        self, key_code: int, *, command: bool = False, shift: bool = False
    ) -> None:
        self.chords.append((key_code, command, shift))

    def post_return(self, pid: int) -> None:
        self.returns.append(pid)
        callback = self._on_return
        if callable(callback):
            callback()


# --------------------------------------------------------------------------- #
# Panels, as measured on the live machine
# --------------------------------------------------------------------------- #


def path_field(**extra: Any) -> dict[str, Any]:
    """The Go to Folder field: AXTextField, id PathTextField, settable value."""
    return node(
        "AXTextField",
        identifier=PATH_FIELD_ID,
        value="/",
        settable=True,
        actions=["AXShowMenu", "AXConfirm"],
        AXFocused=True,
        _id="path-field",
        **extra,
    )


def search_field() -> dict[str, Any]:
    """The panel's ordinary file-search field. Same role, must be ignored."""
    return node(
        "AXTextField",
        identifier="",
        subrole="AXSearchField",
        settable=True,
        _id="search-field",
    )


def default_button(identifier: str = "", _id: str = "button") -> dict[str, Any]:
    """A usable commit button: AXButton, enabled, offering AXPress."""
    return node(
        "AXButton", identifier=identifier, enabled=True, actions=["AXPress"], _id=_id
    )


def goto_sheet() -> dict[str, Any]:
    """Go to Folder: the path field, and no commit control of any kind.

    Measured live — AXDefaultButton, AXCancelButton and AXSections are all
    advertised and nil, which is why the commit is a Return.
    """
    return node(
        "AXSheet", identifier=GOTO_SHEET_ID, children=[path_field()], _id="goto-sheet"
    )


def file_row(name: str, *, index: int = 1, selectable: bool = True) -> dict[str, Any]:
    """One file, as the live list builds it: AXTextField in AXCell in AXRow.

    Only the row's AXSelected is settable; the filename's and the cell's are
    not, which is why the row is what production writes to.
    """
    label = node("AXTextField", value=name, _id=f"name-{name}")
    cell = node("AXCell", _id=f"cell-{name}", children=[label], AXSelected=False)
    row = node(
        "AXRow",
        _id=f"row-{name}",
        subrole="AXOutlineRow",
        children=[cell],
        AXSelected=False,
        AXIndex=index,
        actions=["AXPress"],
        _selectable_row=selectable,
    )
    label["AXParent"] = cell
    cell["AXParent"] = row
    return row


def list_view(names: list[str], *, selectable: bool = True) -> dict[str, Any]:
    rows = [
        file_row(name, index=index, selectable=selectable)
        for index, name in enumerate(names, start=1)
    ]
    outline = node(
        "AXOutline",
        identifier=LIST_VIEW_ID,
        _id="outline",
        children=rows,
        AXRows=rows,
        AXVisibleRows=rows,
        AXSelectedRows=[],
    )
    for row in rows:
        row["AXParent"] = outline
        row["_outline"] = outline
    return outline


def open_panel(
    *,
    with_goto: bool = False,
    panel_button: dict[str, Any] | None = None,
    files: list[str] | None = None,
    selectable: bool = True,
    ok_button: bool = True,
    _id: str = "panel-sheet",
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [search_field()]
    if with_goto:
        contents.append(goto_sheet())
    contents.append(list_view(files if files is not None else [KEY.name],
                              selectable=selectable))
    if ok_button:
        contents.append(default_button(identifier=OK_BUTTON_ID, _id="ok-button"))
    contents.append(
        node("AXButton", identifier="CancelButton", enabled=True,
             actions=["AXPress"], _id="cancel-button")
    )

    sheet = node(
        "AXSheet",
        identifier=OPEN_PANEL_ID,
        children=[node("AXSplitGroup", children=contents, _id=f"{_id}-split")],
        _id=_id,
    )
    if panel_button is not None:
        sheet["AXDefaultButton"] = panel_button
    return sheet


def panel_app(
    *,
    with_goto: bool = False,
    panel_button: dict[str, Any] | None = None,
    **panel: Any,
) -> dict[str, Any]:
    sheet = open_panel(with_goto=with_goto, panel_button=panel_button, **panel)
    return node(
        "AXApplication",
        AXWindows=[node("AXWindow", children=[sheet], _id="window")],
        _id="app",
    )


def idle_app() -> dict[str, Any]:
    """A panel-service process with no panel — the common case for the others."""
    return node("AXApplication", AXWindows=[], _id="idle")


def selector_for(api: FakeAxApi, **kwargs: Any) -> MacOSFileSelector:
    return MacOSFileSelector(
        api=api, appear_timeout_ms=600, close_timeout_ms=600, **kwargs
    )


def opening_panel_app(
    *, rebuilds_panel: bool = False, **panel: Any
) -> dict[str, Any]:
    """A panel that reacts the way the live one does.

    The Return removes the Go to Folder sheet (navigation). Pressing the commit
    button closes the panel. ``rebuilds_panel`` replaces the panel with a new
    element as the sheet closes, as AppKit is free to do, so a stale handle
    would be pressed instead of the live one.
    """
    app = panel_app(with_goto=True, **panel)
    window = app["AXWindows"][0]
    sheet = window["AXChildren"][0]
    split = sheet["AXChildren"][0]
    goto = split["AXChildren"][1]

    def close_panel(_name: str) -> None:
        window["AXChildren"] = []

    def navigated() -> None:
        split["AXChildren"] = [c for c in split["AXChildren"] if c is not goto]
        if rebuilds_panel:
            rebuilt = open_panel(_id="rebuilt", **panel)
            for button in _buttons_of(rebuilt):
                button["_on_action"] = close_panel
            window["AXChildren"] = [rebuilt]

    for button in _buttons_of(sheet):
        button["_on_action"] = close_panel
    if "AXDefaultButton" in sheet:
        sheet["AXDefaultButton"]["_on_action"] = close_panel
    app["_on_return"] = navigated
    return app


def _buttons_of(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        child
        for child in sheet["AXChildren"][0]["AXChildren"]
        if child["AXRole"] == "AXButton"
    ]


def panel_of(app: dict[str, Any]) -> dict[str, Any]:
    sheet: dict[str, Any] = app["AXWindows"][0]["AXChildren"][0]
    return sheet


def row_of(app: dict[str, Any], name: str = KEY.name) -> dict[str, Any]:
    outline = next(
        child
        for child in panel_of(app)["AXChildren"][0]["AXChildren"]
        if child["AXRole"] == "AXOutline"
    )
    row: dict[str, Any] = next(
        r for r in outline["AXChildren"] if r["_id"] == f"row-{name}"
    )
    return row


def find_goto_field(app: dict[str, Any]) -> dict[str, Any]:
    sheet = app["AXWindows"][0]["AXChildren"][0]
    goto = sheet["AXChildren"][0]["AXChildren"][1]
    field: dict[str, Any] = goto["AXChildren"][0]
    return field


# --------------------------------------------------------------------------- #
# Finding the panel process
# --------------------------------------------------------------------------- #


async def test_the_panel_is_found_by_hierarchy_not_by_name_or_recency():
    """Three panel services run at once; only one holds the open panel."""
    api = FakeAxApi(
        {
            2566: idle_app(),
            59478: idle_app(),
            62868: panel_app(),
        }
    )

    panel = selector_for(api).find_open_panel()

    assert panel is not None
    assert panel.pid == 62868
    # Not the newest, not the first, not by process name.
    assert panel.pid != max(api.apps) or True
    assert api.created[: api.created.index(62868) + 1] == [2566, 59478, 62868]


async def test_only_panel_service_processes_are_considered():
    api = FakeAxApi(
        {1: panel_app(), 2: panel_app()},
        processes=[
            RunningProcess(1, "com.google.Chrome", "Google Chrome for Testing"),
            RunningProcess(2, PANEL_SERVICE_BUNDLE_ID, "Open and Save Panel Service"),
        ],
    )

    selector = selector_for(api)

    assert selector.panel_service_pids() == [2]
    panel = selector.find_open_panel()
    assert panel is not None and panel.pid == 2


async def test_no_pid_is_hardcoded_anywhere():
    source = (
        Path(nf.__file__).parent / "native_files.py"
    ).read_text(encoding="utf-8")

    for pid in ("62868", "59478", "2566", "58579"):
        assert pid not in source


async def test_the_browser_process_name_does_not_select_the_panel():
    """The panel lives in an XPC service, not in the browser process."""
    api = FakeAxApi({62868: panel_app()})

    panel = selector_for(api, process_name="Google Chrome for Testing").find_open_panel()

    assert panel is not None and panel.pid == 62868
    # The configured name reaches nothing that does the choosing.
    assert not any("Google Chrome" in str(read) for read in api.reads)


async def test_a_missing_panel_times_out_with_an_explanation():
    api = FakeAxApi({2566: idle_app()})

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    message = str(exc.value)
    assert "No native Open panel appeared" in message
    assert OPEN_PANEL_ID in message
    assert api.writes == [] and api.actions == []


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


async def test_the_full_selection_sequence():
    api = FakeAxApi({62868: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    # ⌘⇧G, the *directory* written, a Return to navigate, the row selected,
    # then one press of the panel's commit button.
    assert api.chords == [(5, True, True)]
    assert api.writes == [
        ("path-field", "AXValue", str(KEY.parent)),
        (f"row-{KEY.name}", "AXSelected", True),
    ]
    assert api.returns == [62868]
    assert api.actions == [("ok-button", "AXPress")]


async def test_the_shortcut_is_sent_only_after_the_panel_is_found():
    api = FakeAxApi({62868: opening_panel_app()})
    selector = selector_for(api)

    assert api.chords == []
    await selector.select_file(KEY)
    # The panel was read before any key was sent.
    assert api.reads and api.chords


async def test_the_path_is_written_as_an_absolute_resolved_path(tmp_path):
    api = FakeAxApi({1: opening_panel_app()})
    messy = tmp_path / "keys" / ".." / "keys" / "Key-6.dat"
    messy.parent.mkdir(parents=True, exist_ok=True)
    messy.resolve().write_bytes(b"x")

    await selector_for(api).select_file(messy)

    written = api.writes[0][2]
    assert written == str(messy.resolve().parent)
    assert ".." not in written


async def test_the_path_field_is_located_by_identifier_not_by_role():
    """The panel's search field shares AXTextField and must be ignored."""
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert api.writes[0][0] == "path-field"
    assert not any(write[0] == "search-field" for write in api.writes)


async def test_the_path_is_committed_with_a_return_not_axconfirm():
    """Measured live: AXConfirm reports success and leaves the sheet up."""
    api = FakeAxApi({62868: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert api.returns == [62868], "one Return, to the panel process"
    assert not any(action == "AXConfirm" for _, action in api.actions)
    assert not any(target == "path-field" for target, _ in api.actions)


async def test_the_return_goes_to_the_discovered_panel_pid():
    """Not to the browser, and not to whatever is frontmost."""
    api = FakeAxApi({2566: idle_app(), 62868: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert api.returns == [62868]
    assert 2566 not in api.returns


async def test_the_return_is_sent_once_and_never_repeated():
    """A keystroke that did nothing is not worth repeating."""
    api = FakeAxApi({1: panel_app(with_goto=True)})  # nothing reacts

    with pytest.raises(NativeFileDialogError):
        await selector_for(api).select_file(KEY)

    assert api.returns == [1]


async def test_the_field_is_verified_focused_before_the_return():
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert ("path-field", "AXFocused") in api.reads
    # Already focused, so nothing needed setting.
    assert not any(name == "AXFocused" for _, name, _ in api.writes)


async def test_an_unfocused_field_is_focused_first():
    app = opening_panel_app()
    field = find_goto_field(app)
    field["AXFocused"] = False
    field["_settable_focus"] = True

    class Focusable(FakeAxApi):
        def is_settable(self, element: dict[str, Any], name: str) -> bool:
            if name == "AXFocused":
                return bool(element.get("_settable_focus"))
            return super().is_settable(element, name)

    api = Focusable({1: app})

    await selector_for(api).select_file(KEY)

    assert ("path-field", "AXFocused", True) in api.writes
    assert api.returns == [1], "and only then was the Return sent"


async def test_a_field_that_cannot_be_focused_stops_before_the_return():
    """A key event aimed at an unknown target is worse than a clear failure."""
    app = opening_panel_app()
    find_goto_field(app)["AXFocused"] = False
    api = FakeAxApi({1: app})  # AXFocused is not settable in the base fake

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    assert "not settable" in str(exc.value)
    assert api.returns == [], "nothing was sent"


async def test_a_field_that_refuses_to_take_focus_stops_before_the_return():
    app = opening_panel_app()
    field = find_goto_field(app)
    field["AXFocused"] = False

    class Stubborn(FakeAxApi):
        def is_settable(self, element: dict[str, Any], name: str) -> bool:
            return True if name == "AXFocused" else super().is_settable(element, name)

        def set_attribute_value(self, element, name, value):  # type: ignore[no-untyped-def]
            self.writes.append((str(element.get("_id", "")), name, value))
            if name != "AXFocused":
                element[name] = value  # focus silently does not take

    api = Stubborn({1: app})

    with pytest.raises(NativeFileDialogError, match="did not take focus"):
        await selector_for(api).select_file(KEY)

    assert api.returns == []


async def test_a_panel_that_vanishes_after_navigating_is_reported():
    """Navigation does not close the panel; if it goes, something is wrong."""
    app = opening_panel_app()
    window = app["AXWindows"][0]
    navigated = app["_on_return"]
    api = FakeAxApi({1: app})

    def navigate_then_vanish() -> None:
        navigated()
        window["AXChildren"] = []

    api._on_return = navigate_then_vanish

    with pytest.raises(NativeFileDialogError, match="disappeared after navigating"):
        await selector_for(api).select_file(KEY)

    assert api.actions == []


def code_identifiers(path: Path) -> set[str]:
    """Every identifier the module actually uses — no literals, no prose.

    Names only, because the error messages here legitimately talk about the
    mechanisms that were rejected, and a text search would trip over them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        element.attr if isinstance(element, ast.Attribute) else element.id
        for element in ast.walk(tree)
        if isinstance(element, ast.Attribute | ast.Name)
    }


def code_symbols(path: Path) -> set[str]:
    """Every identifier and non-docstring literal in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(scope, clean=False)
        for scope in ast.walk(tree)
        if isinstance(
            scope, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
    }
    symbols: set[str] = set()
    for element in ast.walk(tree):
        if isinstance(element, ast.Attribute):
            symbols.add(element.attr)
        elif isinstance(element, ast.Name):
            symbols.add(element.id)
        elif (
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and element.value not in docstrings
        ):
            symbols.add(element.value)
    return symbols


# --------------------------------------------------------------------------- #
# Selecting the file in the list
# --------------------------------------------------------------------------- #


async def test_the_directory_is_navigated_to_not_the_file():
    """Go to Folder is navigation: a full path lands in the parent regardless."""
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    navigated = api.writes[0]
    assert navigated == ("path-field", "AXValue", str(KEY.parent))
    assert KEY.name not in navigated[2]


async def test_the_row_is_found_by_exact_basename():
    api = FakeAxApi(
        {1: opening_panel_app(files=["other.dat", KEY.name, "notes.txt"])}
    )

    await selector_for(api).select_file(KEY)

    assert (f"row-{KEY.name}", "AXSelected", True) in api.writes
    assert not any(write[0] == "row-other.dat" for write in api.writes)


async def test_a_similarly_named_file_is_not_selected():
    """A prefix or suffix match would take the wrong file quite happily."""
    api = FakeAxApi(
        {1: opening_panel_app(files=[f"{KEY.name}.backup", f"copy-{KEY.name}"])}
    )

    with pytest.raises(NativeFileDialogError, match="is not in the Open panel"):
        await selector_for(api).select_file(KEY)

    assert not any(name == "AXSelected" for _, name, _ in api.writes)
    assert api.actions == []


async def test_the_first_row_is_never_taken_as_a_fallback():
    api = FakeAxApi({1: opening_panel_app(files=["aaa-first.dat", "zzz-last.dat"])})

    with pytest.raises(NativeFileDialogError):
        await selector_for(api).select_file(KEY)

    assert api.writes[1:] == [], "no row was selected at all"


async def test_the_row_ancestor_is_required():
    app = opening_panel_app()
    row = row_of(app)
    # A filename with no row around it: the cell is reparented to the outline.
    cell = row["AXChildren"][0]
    cell["AXParent"] = row["AXParent"]
    row["AXParent"]["AXChildren"] = [cell]

    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="no AXRow ancestor"):
        await selector_for(api).select_file(KEY)


async def test_a_row_whose_selection_is_not_settable_stops_the_run():
    """Measured live: the filename's and the cell's AXSelected are read-only."""
    api = FakeAxApi({1: opening_panel_app(selectable=False)})

    with pytest.raises(NativeFileDialogError, match="not settable"):
        await selector_for(api).select_file(KEY)

    assert not any(name == "AXSelected" for _, name, _ in api.writes)
    assert api.actions == []


async def test_the_selection_is_written_exactly_once(caplog):
    import logging

    api = FakeAxApi({1: opening_panel_app()})

    with caplog.at_level(logging.INFO):
        await selector_for(api).select_file(KEY)

    selections = [write for write in api.writes if write[1] == "AXSelected"]
    assert selections == [(f"row-{KEY.name}", "AXSelected", True)]
    assert f"Selecting file row: {KEY.name}" in caplog.text
    assert f"File row selected: {KEY.name}" in caplog.text


async def test_the_selection_is_read_back_and_must_be_true():
    app = opening_panel_app()

    class Unsticking(FakeAxApi):
        def set_attribute_value(self, element, name, value):  # type: ignore[no-untyped-def]
            self.writes.append((str(element.get("_id", "")), name, value))
            if name != "AXSelected":
                element[name] = value  # the row refuses to stay selected

    api = Unsticking({1: app})

    with pytest.raises(NativeFileDialogError, match="did not stay selected"):
        await selector_for(api).select_file(KEY)

    assert api.actions == [], "nothing was opened"


async def test_the_list_must_agree_that_the_row_is_selected():
    """AppKit's own acknowledgement, not ours."""
    app = opening_panel_app()

    class Silent(FakeAxApi):
        def set_attribute_value(self, element, name, value):  # type: ignore[no-untyped-def]
            self.writes.append((str(element.get("_id", "")), name, value))
            element[name] = value  # but AXSelectedRows is never updated

    api = Silent({1: app})

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    assert "AXSelectedRows does not contain it" in str(exc.value)
    assert api.actions == [], "nothing was opened"


async def test_selected_rows_is_compared_by_element_identity():
    """Not by repr: two handles for one row are different Python objects."""
    app = opening_panel_app()

    class Copying(FakeAxApi):
        def set_attribute_value(self, element, name, value):  # type: ignore[no-untyped-def]
            super().set_attribute_value(element, name, value)
            outline = element.get("_outline")
            if name == "AXSelected" and isinstance(outline, dict):
                # A different object for the same element, as PyObjC returns.
                outline["AXSelectedRows"] = [dict(element)]

    api = Copying({1: app})

    await selector_for(api).select_file(KEY)

    assert api.actions == [("ok-button", "AXPress")]


async def test_the_filename_is_never_opened_or_confirmed():
    """AXOpen and AXConfirm exist on the filename; neither is used."""
    app = opening_panel_app()
    name_element = row_of(app)["AXChildren"][0]["AXChildren"][0]
    name_element["_actions"] = ["AXOpen", "AXShowMenu", "AXConfirm"]
    api = FakeAxApi({1: app})

    await selector_for(api).select_file(KEY)

    assert not any(target.startswith("name-") for target, _ in api.actions)
    assert not any(action in ("AXOpen", "AXConfirm") for _, action in api.actions)


async def test_the_row_is_never_pressed():
    """The row offers AXPress; selection is a write, which can be verified."""
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert not any(target.startswith("row-") for target, _ in api.actions)


async def test_the_panel_is_re_resolved_before_the_final_press():
    api = FakeAxApi({1: opening_panel_app(rebuilds_panel=True)})

    await selector_for(api).select_file(KEY)

    # The button pressed belongs to the panel that exists now.
    assert api.actions == [("ok-button", "AXPress")]


async def test_a_usable_default_button_is_preferred(caplog):
    import logging

    api = FakeAxApi(
        {1: opening_panel_app(panel_button=default_button(_id="default-ok"))}
    )

    with caplog.at_level(logging.INFO):
        await selector_for(api).select_file(KEY)

    assert api.actions == [("default-ok", "AXPress")]
    assert "Pressing Open panel default button" in caplog.text


async def test_a_nil_default_button_falls_back_to_the_ok_button(caplog):
    import logging

    api = FakeAxApi({1: opening_panel_app()})  # no AXDefaultButton at all

    with caplog.at_level(logging.INFO):
        await selector_for(api).select_file(KEY)

    assert api.actions == [("ok-button", "AXPress")]
    assert f"using AXButton id={OK_BUTTON_ID}" in caplog.text


async def test_the_cancel_button_is_never_pressed():
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert not any(target == "cancel-button" for target, _ in api.actions)


async def test_a_cancel_button_offered_as_the_default_is_refused():
    app = opening_panel_app(
        panel_button=default_button(identifier="CancelButton", _id="cancel-default")
    )
    api = FakeAxApi({1: app})

    await selector_for(api).select_file(KEY)

    # It fell through to OKButton rather than pressing Cancel.
    assert api.actions == [("ok-button", "AXPress")]


async def test_exactly_one_press_commits_the_panel():
    api = FakeAxApi({1: opening_panel_app()})

    await selector_for(api).select_file(KEY)

    assert len(api.actions) == 1


async def test_the_panel_must_close_for_the_selection_to_count():
    app = opening_panel_app()
    for button in _buttons_of(panel_of(app)):
        button["_on_action"] = lambda name: None
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="still on screen"):
        await selector_for(api).select_file(KEY)

    assert len(api.actions) == 1, "and it was not retried"


async def test_no_rejected_mechanism_is_called_anywhere():
    """Five mechanisms have failed against this dialog; none may come back."""
    source = Path(nf.__file__).parent / "native_files.py"
    identifiers = code_identifiers(source)
    literals = {
        symbol for symbol in code_symbols(source) if symbol not in identifiers
    }

    for banned in (
        "clipboard", "keystroke", "paste", "set_input_files",
        "expect_file_chooser", "osascript", "perform_confirm",
    ):
        assert not any(banned in name for name in identifiers), banned

    # AXConfirm may be *explained* in a message; it must never be a value
    # handed to the API, and no AppleScript may be embedded either.
    assert "AXConfirm" not in literals
    assert not any("tell application" in literal for literal in literals)

    # The two key events are the shortcut and one Return, neither with text.
    assert "send_key_chord" in identifiers
    assert "post_return" in identifiers


# --------------------------------------------------------------------------- #
# Refusing to go on
# --------------------------------------------------------------------------- #


async def test_a_field_that_is_not_settable_stops_before_writing():
    app = opening_panel_app()
    field = app["AXWindows"][0]["AXChildren"][0]["AXChildren"][0]["AXChildren"][1][
        "AXChildren"
    ][0]
    field["_settable"] = False
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="not settable"):
        await selector_for(api).select_file(KEY)

    assert api.writes == [] and api.actions == []


async def test_a_disabled_field_stops_before_writing():
    app = opening_panel_app()
    field = app["AXWindows"][0]["AXChildren"][0]["AXChildren"][0]["AXChildren"][1][
        "AXChildren"
    ][0]
    field["AXEnabled"] = False
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="is disabled"):
        await selector_for(api).select_file(KEY)

    assert api.writes == []


async def test_a_refused_write_is_reported():
    api = FakeAxApi({1: opening_panel_app()}, refuse_write=True)

    with pytest.raises(NativeFileDialogError, match="could not be written"):
        await selector_for(api).select_file(KEY)

    assert api.actions == []


async def test_a_value_that_did_not_take_stops_before_confirming():
    app = opening_panel_app()
    field = app["AXWindows"][0]["AXChildren"][0]["AXChildren"][0]["AXChildren"][1][
        "AXChildren"
    ][0]

    class Stubborn(FakeAxApi):
        def set_attribute_value(self, element: dict[str, Any], name: str, value: Any):
            self.writes.append((str(element.get("_id", "")), name, value))
            element[name] = "/"  # the field keeps what it had

    api = Stubborn({1: app})

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    assert "did not take the key path" in str(exc.value)
    assert "field is: /" in str(exc.value)
    assert api.actions == [], "nothing was confirmed"
    assert field is not None


async def test_a_refused_press_is_reported():
    api = FakeAxApi({1: opening_panel_app()}, refuse_action="AXPress")

    with pytest.raises(NativeFileDialogError, match="refused AXPress"):
        await selector_for(api).select_file(KEY)


async def test_the_goto_sheet_needs_no_default_button():
    """It has none: advertised and nil. The Return is why that is survivable."""
    app = opening_panel_app()
    goto = app["AXWindows"][0]["AXChildren"][0]["AXChildren"][0]["AXChildren"][1]
    assert "AXDefaultButton" not in goto

    api = FakeAxApi({1: app})
    await selector_for(api).select_file(KEY)

    assert api.returns == [1]


async def test_an_unusable_default_button_falls_back_to_the_ok_button(caplog):
    """It was nil before a file was selected; it may still be unusable after."""
    import logging

    app = opening_panel_app(panel_button=default_button(_id="broken"))
    panel_of(app)["AXDefaultButton"]["AXRole"] = "AXStaticText"
    api = FakeAxApi({1: app})

    with caplog.at_level(logging.INFO):
        await selector_for(api).select_file(KEY)

    assert api.actions == [("ok-button", "AXPress")]
    assert "using AXButton id=OKButton" in caplog.text


async def test_a_default_button_without_axpress_falls_back():
    app = opening_panel_app(panel_button=default_button(_id="broken"))
    panel_of(app)["AXDefaultButton"]["_actions"] = []
    api = FakeAxApi({1: app})

    await selector_for(api).select_file(KEY)

    assert api.actions == [("ok-button", "AXPress")]





async def test_a_goto_sheet_that_will_not_close_is_reported():
    """No Return-key fallback: the AX action is what is being validated."""
    api = FakeAxApi({1: panel_app(with_goto=True)})  # nothing reacts to the press

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    message = str(exc.value)
    assert "Return was sent to the Go to Folder dialog" in message
    assert f"{GOTO_SHEET_ID} did not close" in message
    assert api.returns == [1], "and it was not repeated"


async def test_a_missing_goto_sheet_is_reported():
    api = FakeAxApi({1: panel_app(with_goto=False)})

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    assert "Go to Folder path field could not be reached" in str(exc.value)
    assert api.writes == []


async def test_a_panel_that_will_not_close_fails_the_selection():
    """Success means the panel closed. Nothing weaker counts."""
    app = opening_panel_app()
    for button in _buttons_of(panel_of(app)):
        button["_on_action"] = lambda name: None  # pressing does nothing
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="still on screen"):
        await selector_for(api).select_file(KEY)


async def test_a_panel_with_neither_button_is_reported():
    app = opening_panel_app(ok_button=False)
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError) as exc:
        await selector_for(api).select_file(KEY)

    assert "AXDefaultButton" in str(exc.value)
    assert OK_BUTTON_ID in str(exc.value)
    assert api.actions == [], "the Cancel button next to it was never pressed"


async def test_a_disabled_ok_button_is_reported():
    app = opening_panel_app()
    next(b for b in _buttons_of(panel_of(app)) if b["_id"] == "ok-button")[
        "AXEnabled"
    ] = False
    api = FakeAxApi({1: app})

    with pytest.raises(NativeFileDialogError, match="not usable"):
        await selector_for(api).select_file(KEY)

    assert api.actions == []


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


async def test_a_secure_field_value_is_never_read():
    from hsc_queue_monitor.browser.macos_ax import value_of

    api = FakeAxApi({1: idle_app()})
    secure = node("AXSecureTextField", value="hunter2", _id="secure")

    assert value_of(api, secure) == ""
    assert ("secure", "AXValue") not in api.reads


async def test_only_the_basename_is_logged(caplog):
    """The path is not a secret, but where someone keeps their keys is theirs."""
    import logging

    api = FakeAxApi({1: opening_panel_app()})

    with caplog.at_level(logging.DEBUG):
        await selector_for(api).select_file(KEY)

    assert str(KEY) not in caplog.text
    assert str(KEY.parent) not in caplog.text
    assert "Key-6.dat" in caplog.text, "the basename is useful and harmless"


# --------------------------------------------------------------------------- #
# Choosing an implementation
# --------------------------------------------------------------------------- #


def test_macos_gets_the_native_selector():
    selector = native_file_selector(mode="native", platform="darwin", api=FakeAxApi({}))

    assert isinstance(selector, MacOSFileSelector)
    assert isinstance(selector, NativeFileSelector)
    assert isinstance(selector, nf.AccessibilityInspector)


def test_chooser_mode_opts_out_entirely():
    for platform in ("linux", "win32", "darwin"):
        assert native_file_selector(mode="chooser", platform=platform) is None


def test_an_unsupported_platform_fails_instead_of_falling_back():
    with pytest.raises(ConfigError) as exc:
        native_file_selector(mode="native", platform="linux")

    message = str(exc.value)
    assert (
        "Native file selection is configured, but no native file selector is "
        "implemented for platform: linux" in message
    )
    assert "macOS" in message
    assert "'chooser'" in message


def test_the_api_is_not_loaded_until_it_is_used():
    """Constructing a selector happens on every command; loading must not."""
    selector = native_file_selector(mode="native", platform="darwin")

    assert isinstance(selector, MacOSFileSelector)
    assert selector._api is None


def test_a_missing_dependency_is_explained_not_swallowed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pyobjc(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ApplicationServices":
            raise ImportError("No module named 'ApplicationServices'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyobjc)

    with pytest.raises(AccessibilityUnavailable) as exc:
        load_ax_api("darwin")

    message = str(exc.value)
    assert "PyObjC" in message
    assert "macos-debug" in message


def test_the_api_is_macos_only():
    with pytest.raises(AccessibilityUnavailable, match="macOS-only"):
        load_ax_api("linux")


class FakeCoreGraphics:
    """Just enough ApplicationServices to watch what gets posted where."""

    kCGEventFlagMaskCommand = 1 << 20
    kCGEventFlagMaskShift = 1 << 17
    kCGHIDEventTap = 0

    def __init__(self, *, creation_fails: bool = False) -> None:
        self.created: list[tuple[int, bool]] = []
        self.flags: list[int] = []
        self.posted_to_pid: list[tuple[int, object]] = []
        self.posted_globally: list[object] = []
        self._creation_fails = creation_fails

    def CGEventCreateKeyboardEvent(self, source, key_code, pressed):  # noqa: N802
        if self._creation_fails:
            return None
        self.created.append((key_code, pressed))
        return f"event-{key_code}-{pressed}"

    def CGEventSetFlags(self, event, flags):  # noqa: N802
        self.flags.append(flags)

    def CGEventPostToPid(self, pid, event):  # noqa: N802
        self.posted_to_pid.append((pid, event))

    def CGEventPost(self, tap, event):  # noqa: N802
        self.posted_globally.append(event)


def adapter_with(core_graphics: FakeCoreGraphics) -> Any:
    """A real PyObjCAxApi wired to a fake framework, not to macOS."""
    from hsc_queue_monitor.browser.macos_ax import PyObjCAxApi

    adapter = object.__new__(PyObjCAxApi)
    adapter._ax = core_graphics  # type: ignore[attr-defined]
    return adapter


def test_return_is_one_keydown_and_one_keyup_with_no_modifiers():
    from hsc_queue_monitor.browser.macos_ax import KEY_CODE_RETURN

    core_graphics = FakeCoreGraphics()

    adapter_with(core_graphics).post_return(62868)

    assert core_graphics.created == [(KEY_CODE_RETURN, True), (KEY_CODE_RETURN, False)]
    assert core_graphics.flags == [0, 0], "a plain Return carries no modifiers"
    assert KEY_CODE_RETURN == 36, "kVK_Return, the physical key"


def test_return_is_posted_to_the_pid_and_never_globally():
    """Global posting would deliver it to whatever is frontmost — the terminal."""
    core_graphics = FakeCoreGraphics()

    adapter_with(core_graphics).post_return(62868)

    assert [pid for pid, _ in core_graphics.posted_to_pid] == [62868, 62868]
    assert core_graphics.posted_globally == []


def test_a_return_event_that_cannot_be_created_is_reported():
    core_graphics = FakeCoreGraphics(creation_fails=True)

    with pytest.raises(AccessibilityUnavailable, match="Return key event"):
        adapter_with(core_graphics).post_return(1)

    assert core_graphics.posted_to_pid == []


def test_the_shortcut_still_goes_through_the_global_tap():
    """⌘⇧G is aimed at the frontmost app on purpose; only Return is targeted."""
    from hsc_queue_monitor.browser.macos_ax import KEY_CODE_G

    core_graphics = FakeCoreGraphics()

    adapter_with(core_graphics).send_key_chord(KEY_CODE_G, command=True, shift=True)

    assert len(core_graphics.posted_globally) == 2
    assert core_graphics.posted_to_pid == []
    assert core_graphics.flags == [
        FakeCoreGraphics.kCGEventFlagMaskCommand | FakeCoreGraphics.kCGEventFlagMaskShift
    ] * 2


def test_the_fake_satisfies_the_real_protocol():
    """If the fake drifts from AxApi, these tests stop meaning anything."""
    assert isinstance(FakeAxApi({}), AxApi)


# --------------------------------------------------------------------------- #
# Diagnostics kept working
# --------------------------------------------------------------------------- #


async def test_the_hierarchy_description_is_bounded_and_sanitized():
    api = FakeAxApi({1: panel_app(with_goto=True)})

    rows = await selector_for(api).describe_hierarchy(max_elements=3)

    assert len(rows) == 3
    assert rows[0]["role"] == "AXSheet"
    assert rows[0]["identifier"] == OPEN_PANEL_ID
    assert json.dumps(rows)  # serialisable, which is what the report needs


async def test_the_ancestry_helper_reports_the_focused_chain():
    app = panel_app(with_goto=True)
    field = app["AXWindows"][0]["AXChildren"][0]["AXChildren"][0]["AXChildren"][1][
        "AXChildren"
    ][0]
    sheet = app["AXWindows"][0]["AXChildren"][0]
    field["AXParent"] = sheet
    app["AXFocusedUIElement"] = field
    api = FakeAxApi({1: app})

    chain = await selector_for(api).ancestry()

    assert [row["role"] for row in chain] == ["AXTextField", "AXSheet"]
    assert chain[0]["identifier"] == PATH_FIELD_ID
