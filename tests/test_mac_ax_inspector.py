"""The standalone macOS Accessibility inspector.

No real Accessibility is involved: every osascript call is replaced by a
scripted stand-in, so these run anywhere. What is tested is the part that has
to be right when the tool is pointed at a dialog that has already hung us once
— the bounds, the timeouts, and what does *not* get read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import mac_ax_inspector as ax  # noqa: E402, I001

# --------------------------------------------------------------------------- #
# A scripted macOS
# --------------------------------------------------------------------------- #


def node_row(
    role: str = "AXGroup",
    *,
    subrole: str = "",
    title: str = "",
    description: str = "",
    value: str = "",
    enabled: str = "true",
    focused: str = "false",
    settable: str = "",
    children: int = 0,
    attributes: str = "AXRole,AXChildren",
    actions: str = "",
) -> str:
    return "\t".join(
        [
            role, subrole, title, description, value, enabled, focused,
            settable, str(children), attributes, actions,
        ]
    )


class FakeOsascript(ax.Osascript):
    """Answers from a canned tree instead of talking to System Events.

    ``tree`` maps an index path ("1", "1.2") to a row. ``timeouts`` names paths
    that hang, and ``failures`` names paths that error — both of which a real
    inspection has to survive.
    """

    def __init__(
        self,
        tree: dict[str, str] | None = None,
        *,
        processes: str = "",
        windows: int = 1,
        timeouts: set[str] | None = None,
        failures: set[str] | None = None,
        focused: str | None = None,
    ) -> None:
        super().__init__(timeout=0.1)
        self.tree = tree or {}
        self.processes = processes
        self.windows = windows
        self.timeouts = timeouts or set()
        self.failures = failures or set()
        self.focused = focused
        self.queries: list[tuple[str, tuple[str, ...]]] = []

    async def run(
        self, script: str, *arguments: str, timeout: float | None = None
    ) -> str:
        kind = self._classify(script)
        self.queries.append((kind, arguments))

        if kind == "processes":
            return self.processes
        if kind == "windows":
            return "no-process" if self.windows < 0 else str(self.windows)
        if kind == "focused":
            return self.focused if self.focused is not None else "none"

        path = arguments[1]
        if path in self.timeouts:
            raise TimeoutError
        if path in self.failures:
            raise OSError("System Events got an error: can't get UI element")
        return self.tree.get(path, node_row())

    @staticmethod
    def _classify(script: str) -> str:
        if "application processes" in script:
            return "processes"
        if "count of windows" in script:
            return "windows"
        if "AXFocusedUIElement" in script:
            return "focused"
        return "node"

    @property
    def paths_queried(self) -> list[str]:
        return [args[1] for kind, args in self.queries if kind == "node"]


# --------------------------------------------------------------------------- #
# No `entire contents`, ever
# --------------------------------------------------------------------------- #


def applescript_constants() -> list[str]:
    """Every AppleScript the module can send. Found by shape, not by name, so a
    new script cannot be added without these rules applying to it."""
    return [
        value
        for value in vars(ax).values()
        if isinstance(value, str) and "tell application" in value
    ]


def test_no_applescript_uses_entire_contents():
    """It has already hung this browser. It must not be in any script."""
    scripts = applescript_constants()

    assert len(scripts) >= 4, "the constants moved; this guard needs updating"
    for script in scripts:
        assert "entire contents" not in script


def test_the_node_script_counts_children_rather_than_walking_them():
    assert "count of UI elements of node" in ax._NODE
    assert "UI elements of node" not in ax._NODE.replace("count of UI elements of node", "")


# --------------------------------------------------------------------------- #
# Process discovery
# --------------------------------------------------------------------------- #


async def test_processes_are_parsed():
    runner = FakeOsascript(
        processes="\n".join(
            [
                "Finder\tfalse\ttrue",
                "Google Chrome for Testing\ttrue\ttrue",
                "PyCharm\tfalse\tfalse",
            ]
        )
    )

    processes = await ax.list_processes(runner)

    assert [p.name for p in processes] == [
        "Finder", "Google Chrome for Testing", "PyCharm"
    ]
    assert processes[1].frontmost is True
    assert processes[1].visible is True
    assert processes[2].frontmost is False
    assert processes[2].visible is False


async def test_a_malformed_process_row_does_not_break_the_listing():
    runner = FakeOsascript(processes="Finder\ttrue\n\nBroken")

    processes = await ax.list_processes(runner)

    assert [p.name for p in processes] == ["Finder", "Broken"]
    assert processes[1].frontmost is False


async def test_the_process_listing_does_not_count_windows():
    """One accessibility query per process is what made this time out."""
    assert "count of windows of p" not in ax._PROCESSES


async def test_the_process_listing_gets_its_own_budget():
    """It touches ~95 processes, so a single-node timeout is far too tight."""
    runner = FakeOsascript(processes="Finder\tfalse\ttrue")
    seen: list[float | None] = []

    async def record(script: str, *arguments: str, timeout: float | None = None) -> str:
        seen.append(timeout)
        return await FakeOsascript.run(runner, script, *arguments)

    runner.run = record  # type: ignore[method-assign]
    await ax.list_processes(runner)

    assert seen == [ax.PROCESS_LIST_TIMEOUT]


async def test_an_unknown_process_is_reported():
    runner = FakeOsascript(windows=-1)

    with pytest.raises(LookupError, match="no process named"):
        await ax.window_count(runner, "Nope")


# --------------------------------------------------------------------------- #
# Windows and traversal
# --------------------------------------------------------------------------- #


async def test_every_window_is_a_root_and_none_is_assumed_to_be_the_dialog():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", title="Browser"),
            "2": node_row("AXWindow", title="Open", children=1),
            "2.1": node_row("AXSheet"),
        },
        windows=2,
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert [e.id for e in walk.elements] == ["1", "2", "2.1"]
    assert [e.depth for e in walk.elements] == [0, 0, 1]
    assert walk.elements[2].parent_id == "2"


async def test_the_walk_is_breadth_first_and_direct_children_only():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", children=2),
            "1.1": node_row("AXGroup", children=1),
            "1.2": node_row("AXSheet"),
            "1.1.1": node_row("AXTextField"),
        }
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert [e.id for e in walk.elements] == ["1", "1.1", "1.2", "1.1.1"]
    assert runner.paths_queried == ["1", "1.1", "1.2", "1.1.1"]


async def test_the_element_budget_stops_the_walk_and_says_so():
    runner = FakeOsascript({"1": node_row("AXWindow", children=50)})

    walk = await ax.walk_tree(runner, "Chrome", max_elements=5)

    assert len(walk.elements) == 5
    assert walk.complete is False
    assert "max-elements" in walk.stop_reason


async def test_the_depth_budget_stops_descending():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", children=1),
            "1.1": node_row("AXGroup", children=1),
            "1.1.1": node_row("AXGroup", children=1),
        }
    )

    walk = await ax.walk_tree(runner, "Chrome", max_depth=1)

    assert [e.id for e in walk.elements] == ["1", "1.1"]
    assert walk.complete is False
    assert "max-depth" in walk.stop_reason


async def test_a_web_area_is_recorded_but_not_entered():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", children=2),
            "1.1": node_row("AXWebArea", title="the page", children=900),
            "1.2": node_row("AXSheet"),
        }
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert [e.id for e in walk.elements] == ["1", "1.1", "1.2"]
    assert walk.elements[1].role == "AXWebArea"
    assert walk.elements[1].child_count == 900, "the count is still reported"


async def test_a_web_area_can_be_entered_on_request():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", children=1),
            "1.1": node_row("AXWebArea", children=2),
        }
    )

    walk = await ax.walk_tree(runner, "Chrome", include_web_area=True)

    assert [e.id for e in walk.elements] == ["1", "1.1", "1.1.1", "1.1.2"]


# --------------------------------------------------------------------------- #
# Surviving bad nodes
# --------------------------------------------------------------------------- #


async def test_one_timing_out_node_does_not_end_the_run():
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", children=3),
            "1.1": node_row("AXGroup"),
            "1.3": node_row("AXSheet"),
        },
        timeouts={"1.2"},
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert [e.id for e in walk.elements] == ["1", "1.1", "1.2", "1.3"]
    assert walk.elements[2].query_error == ax.QUERY_TIMEOUT_MARKER
    assert walk.elements[2].role == "", "nothing was read from it"
    assert walk.complete is True


async def test_a_node_that_errors_is_recorded_with_its_error():
    runner = FakeOsascript(
        {"1": node_row("AXWindow", children=1)}, failures={"1.1"}
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert "can't get UI element" in walk.elements[1].query_error


async def test_a_window_count_timeout_returns_a_partial_result():
    class Hanging(FakeOsascript):
        async def run(
            self, script: str, *arguments: str, timeout: float | None = None
        ) -> str:
            if self._classify(script) == "windows":
                raise TimeoutError
            return await super().run(script, *arguments, timeout=timeout)

    walk = await ax.walk_tree(Hanging(), "Chrome")

    assert walk.elements == []
    assert walk.complete is False
    assert walk.stop_reason == "window-count-timeout"


# --------------------------------------------------------------------------- #
# What each element reports
# --------------------------------------------------------------------------- #


async def test_attributes_and_actions_are_parsed():
    runner = FakeOsascript(
        {
            "1": node_row(
                "AXComboBox",
                subrole="AXSearchField",
                title="Перейти до:",
                value="/",
                settable="true",
                attributes="AXRole,AXValue,AXFocused,AXSelectedText",
                actions="AXPress,AXConfirm,AXShowMenu",
            )
        }
    )

    walk = await ax.walk_tree(runner, "Chrome")
    element = walk.elements[0]

    assert element.attribute_names == ["AXRole", "AXValue", "AXFocused", "AXSelectedText"]
    assert element.action_names == ["AXPress", "AXConfirm", "AXShowMenu"]
    assert element.value_settable == "true"
    assert element.title == "Перейти до:"
    assert element.value == "/"


async def test_a_row_missing_fields_is_handled_safely():
    runner = FakeOsascript({"1": "AXGroup\t\t"})

    walk = await ax.walk_tree(runner, "Chrome")

    assert walk.elements[0].role == "AXGroup"
    assert walk.elements[0].attribute_names == []
    assert walk.elements[0].child_count == 0


async def test_a_secure_field_value_is_never_recorded():
    runner = FakeOsascript(
        {"1": node_row("AXSecureTextField", value="hunter2", title="Пароль")}
    )

    walk = await ax.walk_tree(runner, "Chrome")

    assert walk.elements[0].value == ax.SECURE_MARKER
    assert "hunter2" not in json.dumps(ax.build_report("Chrome", walk, []))


def test_long_values_are_clipped():
    assert len(ax.clip("x" * 5_000)) == ax.MAX_VALUE_CHARS + 1  # + the ellipsis


def test_the_node_script_refuses_to_read_secure_fields():
    """The rule is enforced in AppleScript too, so the value never travels."""
    assert 'if theRole is not "AXSecureTextField" then' in ax._NODE


# --------------------------------------------------------------------------- #
# PID mode: one exact process, through the native API
# --------------------------------------------------------------------------- #


def ax_node(
    role: str = "AXGroup",
    *,
    children: list[dict[str, object]] | None = None,
    attributes: list[str] | None = None,
    actions: list[str] | None = None,
    settable: bool = False,
    **values: object,
) -> dict[str, object]:
    node: dict[str, object] = {
        "AXRole": role,
        "AXChildren": children or [],
        "_attributes": attributes if attributes is not None else ["AXRole", "AXChildren"],
        "_actions": actions or [],
        "_settable": settable,
    }
    node.update(values)
    return node


class FakeAxApi:
    """The native Accessibility API, backed by plain dicts.

    Records which PIDs were used to create roots and every attribute read, so a
    test can prove both that the *exact* process was addressed and that a
    secure field's value was never even asked for.
    """

    def __init__(self, apps: dict[int, dict[str, object]]) -> None:
        self.apps = apps
        self.created: list[int] = []
        self.reads: list[tuple[str, str]] = []
        self.timeouts: list[float] = []

    def create_application(self, pid: int) -> dict[str, object]:
        self.created.append(pid)
        return self.apps.get(pid, ax_node("AXApplication"))

    def set_timeout(self, element: dict[str, object], seconds: float) -> None:
        self.timeouts.append(seconds)

    def attribute_names(self, element: dict[str, object]) -> list[str]:
        return list(element.get("_attributes", []))  # type: ignore[arg-type]

    def attribute_value(self, element: dict[str, object], name: str) -> object:
        self.reads.append((str(element.get("AXRole", "")), name))
        return element.get(name)

    def action_names(self, element: dict[str, object]) -> list[str]:
        return list(element.get("_actions", []))  # type: ignore[arg-type]

    def is_settable(self, element: dict[str, object], name: str) -> bool:
        return bool(element.get("_settable")) and name == "AXValue"

    def pid_of(self, element: dict[str, object]) -> int | None:
        return int(element.get("_pid", 0)) or None  # type: ignore[arg-type]

    def same_element(self, first: object, second: object) -> bool:
        """Compares by identity marker, never by Python identity.

        Mirrors the real API, where every read hands back a fresh object for
        the same element — so a fake that answered ``is`` would let a broken
        cycle check pass.
        """
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        return first.get("_id", id(first)) == second.get("_id", id(second))


def copy_of(node: dict[str, object]) -> dict[str, object]:
    """A different Python object for the same accessibility element."""
    return dict(node)


def panel_app(marker: str) -> dict[str, object]:
    """An openAndSavePanelService-shaped app, labelled so tests can tell two apart."""
    return ax_node(
        "AXApplication",
        AXWindows=[
            ax_node(
                "AXWindow",
                AXTitle=marker,
                children=[ax_node("AXTextField", AXValue="/", settable=True)],
            )
        ],
    )


async def test_the_root_is_created_from_the_exact_pid():
    api = FakeAxApi({59478: panel_app("right"), 2566: panel_app("wrong")})

    walk = await ax.walk_pid(api, 59478)

    assert api.created == [59478], "only the requested process was addressed"
    assert walk.elements[0].title == "right"


async def test_two_processes_sharing_a_name_are_walked_separately():
    """The bug this exists for: `tell process "<name>"` picks one at random."""
    api = FakeAxApi({59478: panel_app("panel-a"), 2566: panel_app("panel-b")})

    first = await ax.walk_pid(api, 59478)
    second = await ax.walk_pid(api, 2566)

    assert first.elements[0].title == "panel-a"
    assert second.elements[0].title == "panel-b"
    assert api.created == [59478, 2566]


def test_pid_mode_never_addresses_a_process_by_name():
    """A name lookup anywhere in this path would reintroduce the ambiguity."""
    source = (PROJECT_ROOT / "scripts" / "mac_ax_inspector.py").read_text(
        encoding="utf-8"
    )
    start = source.index("class NativeAxBackend")
    end = source.index("# Filtering")
    native = source[start:end]

    assert "tell process" not in native
    assert "_NODE" not in native and "_WINDOW_COUNT" not in native
    assert "resolve_process_name" not in native


async def test_windows_come_from_the_pid_root():
    api = FakeAxApi(
        {
            7: ax_node(
                "AXApplication",
                AXWindows=[ax_node("AXWindow", AXTitle="Open"), ax_node("AXWindow")],
            )
        }
    )

    walk = await ax.walk_pid(api, 7)

    assert [e.id for e in walk.elements] == ["1", "2"]
    assert [e.depth for e in walk.elements] == [0, 0]
    assert ("AXApplication", "AXWindows") in api.reads


async def test_the_pid_walk_uses_direct_children_only():
    grandchild = ax_node("AXButton", AXTitle="Відкрити")
    child = ax_node("AXSheet", children=[grandchild])
    api = FakeAxApi(
        {7: ax_node("AXApplication", AXWindows=[ax_node("AXWindow", children=[child])])}
    )

    walk = await ax.walk_pid(api, 7)

    assert [e.id for e in walk.elements] == ["1", "1.1", "1.1.1"]
    assert [e.role for e in walk.elements] == ["AXWindow", "AXSheet", "AXButton"]
    assert walk.elements[2].parent_id == "1.1"


async def test_the_pid_walk_respects_both_budgets():
    deep = ax_node("AXGroup", children=[ax_node("AXGroup", children=[ax_node("AXGroup")])])
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[ax_node("AXWindow",
                                                                   children=[deep])])})

    shallow = await ax.walk_pid(api, 7, max_depth=1)
    assert [e.id for e in shallow.elements] == ["1", "1.1"]
    assert "max-depth" in shallow.stop_reason

    small = await ax.walk_pid(api, 7, max_elements=2)
    assert len(small.elements) == 2
    assert "max-elements" in small.stop_reason


async def test_a_web_area_is_not_entered_by_default_in_pid_mode():
    page = ax_node("AXWebArea", children=[ax_node("AXStaticText", AXValue="page text")])
    api = FakeAxApi({7: ax_node("AXApplication",
                                AXWindows=[ax_node("AXWindow", children=[page])])})

    default = await ax.walk_pid(api, 7)
    assert [e.role for e in default.elements] == ["AXWindow", "AXWebArea"]
    assert default.elements[1].child_count == 1, "the count is still reported"

    opened = await ax.walk_pid(api, 7, include_web_area=True)
    assert [e.role for e in opened.elements] == ["AXWindow", "AXWebArea", "AXStaticText"]


async def test_a_secure_field_value_is_never_read_in_pid_mode():
    secure = ax_node("AXSecureTextField", AXValue="hunter2", AXTitle="Пароль")
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[secure])})

    walk = await ax.walk_pid(api, 7)

    assert walk.elements[0].value == ax.SECURE_MARKER
    assert ("AXSecureTextField", "AXValue") not in api.reads, "not even asked for"
    assert "hunter2" not in json.dumps(ax.build_report("x", walk, [], pid=7))


async def test_attributes_actions_and_settability_are_collected():
    field_node = ax_node(
        "AXComboBox",
        AXTitle="Перейти до:",
        AXValue="/Users",
        AXEnabled=True,
        AXFocused=False,
        attributes=["AXRole", "AXValue", "AXFocused", "AXSelectedText"],
        actions=["AXPress", "AXConfirm"],
        settable=True,
    )
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[field_node])})

    walk = await ax.walk_pid(api, 7)
    element = walk.elements[0]

    assert element.attribute_names == ["AXRole", "AXValue", "AXFocused", "AXSelectedText"]
    assert element.action_names == ["AXPress", "AXConfirm"]
    assert element.value_settable == "true"
    assert element.enabled == "true"
    assert element.focused == "false"
    assert element.looks_editable


async def test_the_native_calls_are_bounded_by_the_query_timeout():
    api = FakeAxApi({7: ax_node("AXApplication")})

    await ax.walk_pid(api, 7, timeout=1.5)

    assert api.timeouts == [1.5]


async def test_the_focused_element_is_read_from_the_pid_root():
    focused = ax_node("AXTextField", AXValue="/", actions=["AXConfirm"])
    api = FakeAxApi({7: ax_node("AXApplication", AXFocusedUIElement=focused)})

    element = await ax.NativeAxBackend(api, 7).focused()

    assert element is not None
    assert element.role == "AXTextField"
    assert element.action_names == ["AXConfirm"]
    assert api.created == [7]


async def test_a_missing_focused_element_in_pid_mode_is_none():
    api = FakeAxApi({7: ax_node("AXApplication")})

    assert await ax.NativeAxBackend(api, 7).focused() is None


def test_a_pid_that_is_not_running_is_reported():
    assert ax.process_exists(999_999) is False
    assert ax.process_exists(os.getpid()) is True


# --------------------------------------------------------------------------- #
# A panel whose application root publishes no AXWindows
# --------------------------------------------------------------------------- #
#
# Live behaviour from the Open panel: AXWindows is empty, yet the application
# has a focused AXOutline that carries AXWindow and AXTopLevelUIElement. The
# panel is there; the obvious question just does not reach it.


def panel_with_ancestry(chain: list[dict[str, object]]) -> dict[str, object]:
    """An application with no AXWindows, whose focused element has *chain* above it.

    ``chain[0]`` is the focused element; each entry after it is the parent of
    the one before. This is the shape the live panel actually has.
    """
    for child, parent in zip(chain, chain[1:], strict=False):
        child["AXParent"] = parent
    return ax_node("AXApplication", AXWindows=[], AXFocusedUIElement=chain[0])


def live_open_panel_chain() -> list[dict[str, object]]:
    """The chain --ancestry returned from the real panel, PID 62868.

    Reproduces the asymmetry that broke the previous attempt: the outer
    AXSplitGroup's AXParent is the AXSheet, but the AXSheet's AXChildren is
    ``[AXApplication]`` — so a walk that starts at the sheet leaves the panel
    immediately and finds nothing. The panel's contents hang off the split
    group, one step below.
    """
    field = ax_node("AXTextField", AXValue="/", settable=True, _id="field")
    application = ax_node("AXApplication", AXTitle="Chrome", _id="app")
    outer_split = ax_node("AXSplitGroup", _id="split-outer", children=[field])
    sheet = ax_node(
        "AXSheet",
        AXDescription="відкрити",
        _id="sheet",
        children=[application],  # not the split group: this graph is asymmetric
    )
    return [
        ax_node("AXOutline", AXDescription="перегляд списком", _id="outline"),
        ax_node("AXScrollArea", _id="scroll"),
        ax_node("AXSplitGroup", _id="split-inner"),
        outer_split,
        sheet,
        ax_node("AXWindow", AXTitle="data:text/html,<input type=file>", _id="window"),
        application,
    ]


async def test_normal_windows_are_still_used_when_present():
    api = FakeAxApi({7: panel_app("Open")})

    walk = await ax.walk_pid(api, 7)

    assert walk.root_source == ax.ROOT_FROM_WINDOWS
    assert [e.role for e in walk.elements] == ["AXWindow", "AXTextField"]


async def test_the_live_panel_root_is_the_element_below_the_sheet():
    """The case this exists for, with the real chain from PID 62868.

    Rooting at the AXSheet produced a two-element walk that left the panel
    immediately (AXSheet → AXApplication). The content is one step lower.
    """
    api = FakeAxApi({62868: panel_with_ancestry(live_open_panel_chain())})

    walk = await ax.walk_pid(api, 62868)

    assert walk.root_source == "AXFocusedUIElement ancestry child-of-AXSheet"
    assert walk.root_role == "AXSplitGroup"
    assert walk.enclosing_role == "AXSheet"
    assert walk.enclosing_description == "відкрити"
    # It walked the panel's contents, not back out to the application.
    assert [e.role for e in walk.elements] == ["AXSplitGroup", "AXTextField"]


async def test_the_sheet_itself_is_never_the_root():
    api = FakeAxApi({7: panel_with_ancestry(live_open_panel_chain())})

    walk = await ax.walk_pid(api, 7)

    assert walk.root_role != "AXSheet"
    assert walk.elements[0].role != "AXSheet"
    assert "AXApplication" not in [e.role for e in walk.elements]


async def test_the_asymmetric_parent_child_graph_is_tolerated():
    """child.AXParent → sheet, while sheet.AXChildren → [application].

    Both are true of the live panel. Root selection must not require the
    relation to be symmetric, because it is not.
    """
    chain = live_open_panel_chain()
    sheet, split = chain[4], chain[3]
    api = FakeAxApi({7: panel_with_ancestry(chain)})

    assert api.attribute_value(split, "AXParent") is sheet
    assert [c["AXRole"] for c in sheet["AXChildren"]] == ["AXApplication"]  # type: ignore[index]

    walk = await ax.walk_pid(api, 7)

    assert [e.role for e in walk.elements] == ["AXSplitGroup", "AXTextField"]


async def test_a_window_is_used_when_there_is_no_sheet():
    """Same rule one role down: the child of the window, not the window."""
    api = FakeAxApi(
        {
            7: panel_with_ancestry(
                [
                    ax_node("AXOutline", _id="outline"),
                    ax_node("AXScrollArea", _id="scroll",
                            children=[ax_node("AXComboBox")]),
                    ax_node("AXWindow", AXTitle="Відкрити", _id="window",
                            children=[ax_node("AXApplication")]),
                    ax_node("AXApplication", _id="app"),
                ]
            )
        }
    )

    walk = await ax.walk_pid(api, 7)

    assert walk.root_source == "AXFocusedUIElement ancestry child-of-AXWindow"
    assert walk.root_role == "AXScrollArea"
    assert walk.enclosing_role == "AXWindow"
    assert [e.role for e in walk.elements] == ["AXScrollArea", "AXComboBox"]


async def test_a_sheet_is_preferred_over_the_window_enclosing_it():
    """The panel is the sheet's; the window around it is unrelated chrome."""
    api = FakeAxApi({7: panel_with_ancestry(live_open_panel_chain())})

    walk = await ax.walk_pid(api, 7)

    assert walk.enclosing_role == "AXSheet"
    assert walk.root_role == "AXSplitGroup"


async def test_the_container_itself_is_used_when_nothing_is_below_it():
    """Only when the focused element *is* the container is there no lower choice."""
    sheet = ax_node(
        "AXSheet", AXDescription="відкрити", _id="sheet",
        children=[ax_node("AXTextField")],
    )
    api = FakeAxApi({7: panel_with_ancestry([sheet, ax_node("AXWindow", _id="w")])})

    walk = await ax.walk_pid(api, 7)

    assert walk.root_source == "AXFocusedUIElement ancestry AXSheet"
    assert walk.root_role == "AXSheet"
    assert [e.role for e in walk.elements] == ["AXSheet", "AXTextField"]


async def test_the_application_is_never_used_as_a_root():
    """It is the whole process, not the panel."""
    api = FakeAxApi(
        {
            7: panel_with_ancestry(
                [
                    ax_node("AXOutline", _id="outline"),
                    ax_node("AXApplication", AXTitle="Chrome", _id="app",
                            children=[ax_node("AXGroup")]),
                ]
            )
        }
    )

    walk = await ax.walk_pid(api, 7)

    assert walk.elements == []
    assert walk.root_source == ax.ROOT_NONE
    assert walk.root_role == ""


async def test_nothing_focused_and_no_windows_is_genuinely_empty():
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[])})

    walk = await ax.walk_pid(api, 7)

    assert walk.elements == []
    assert walk.root_source == ax.ROOT_NONE
    assert walk.focused_present is False


async def test_a_focused_element_with_no_usable_ancestor_is_reported_distinctly():
    """"Nothing is focused" would be a lie here, and would misdirect the search."""
    api = FakeAxApi(
        {7: panel_with_ancestry([ax_node("AXOutline", _id="outline")])}
    )

    walk = await ax.walk_pid(api, 7)

    assert walk.elements == []
    assert walk.root_source == ax.ROOT_NONE
    assert walk.focused_present is True


async def test_root_discovery_reuses_the_ancestry_walk():
    """One parent-walking implementation, not two that can drift apart."""
    chain = live_open_panel_chain()
    api = FakeAxApi({7: panel_with_ancestry(chain)})
    backend = ax.NativeAxBackend(api, 7)

    nodes, cycle = await backend.ancestor_chain()
    roots = await backend.roots()

    assert cycle is False
    assert [n.element.role for n in nodes] == [
        "AXOutline", "AXScrollArea", "AXSplitGroup", "AXSplitGroup",
        "AXSheet", "AXWindow", "AXApplication",
    ]
    # The root is the chain element immediately below the sheet.
    assert api.same_element(roots[0].handle, nodes[3].handle)
    assert not api.same_element(roots[0].handle, nodes[4].handle)


async def test_root_discovery_survives_an_ancestry_cycle():
    group = ax_node("AXGroup", _id="group", children=[ax_node("AXTextField")])
    sheet = ax_node("AXSheet", AXDescription="відкрити", _id="sheet")
    group["AXParent"] = sheet
    sheet["AXParent"] = copy_of(group)  # a different object, same element
    api = FakeAxApi({7: panel_with_ancestry([group])})
    api.apps[7]["AXFocusedUIElement"] = group

    walk = await ax.walk_pid(api, 7)

    assert walk.enclosing_role == "AXSheet"
    assert walk.root_role == "AXGroup"
    assert len(walk.elements) < ax.MAX_ANCESTRY_DEPTH


async def test_the_fallback_root_is_recorded_in_the_report(tmp_path):
    api = FakeAxApi({62868: panel_with_ancestry(live_open_panel_chain())})
    walk = await ax.walk_pid(api, 62868)

    path = tmp_path / "mac-ax-62868.json"
    ax.write_json(path, ax.build_report("panel", walk, [], pid=62868))
    written = json.loads(path.read_text(encoding="utf-8"))

    assert written["root_source"] == "AXFocusedUIElement ancestry child-of-AXSheet"
    assert written["root_role"] == "AXSplitGroup"
    assert written["enclosing_role"] == "AXSheet"
    assert written["enclosing_description"] == "відкрити"
    assert written["focused_present"] is True
    assert written["pid"] == 62868


def test_the_name_mode_report_records_its_root_source():
    walk = ax.Walk(root_source=ax.ROOT_FROM_WINDOWS)

    assert ax.build_report("Finder", walk, [])["root_source"] == "AXWindows"


# --------------------------------------------------------------------------- #
# Ancestry
# --------------------------------------------------------------------------- #


async def test_ancestry_walks_up_through_axparent():
    application = ax_node("AXApplication", _id="app")
    window = ax_node("AXWindow", AXTitle="Відкрити", _id="win", AXParent=application)
    group = ax_node("AXGroup", _id="group", AXParent=window)
    outline = ax_node(
        "AXOutline", AXDescription="перегляд списком", _id="outline", AXParent=group
    )
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[], AXFocusedUIElement=outline)})

    ancestors = await ax.NativeAxBackend(api, 7).ancestry()

    assert [e.role for e in ancestors] == [
        "AXOutline", "AXGroup", "AXWindow", "AXApplication"
    ]
    assert [e.depth for e in ancestors] == [0, 1, 2, 3]
    assert ancestors[0].id == "focused"
    assert ancestors[0].description == "перегляд списком"
    assert ancestors[2].title == "Відкрити"


async def test_ancestry_is_bounded_by_depth():
    """A chain that never ends must still return."""
    node: dict[str, object] = ax_node("AXGroup", _id="start")
    # Every parent gets a fresh id, so only the depth cap can stop this.
    current = node
    for index in range(60):
        parent = ax_node("AXGroup", _id=f"p{index}")
        current["AXParent"] = parent
        current = parent
    api = FakeAxApi({7: ax_node("AXApplication", AXFocusedUIElement=node)})

    ancestors = await ax.NativeAxBackend(api, 7).ancestry()

    assert len(ancestors) == ax.MAX_ANCESTRY_DEPTH == 20


async def test_a_cycle_in_ancestry_is_detected_and_reported():
    """Detected by asking the framework, not by Python identity.

    Each read of AXParent returns a *different* object for the same element,
    so an `is` check would loop until the depth cap and hide the cycle.
    """
    window = ax_node("AXWindow", _id="win")
    outline = ax_node("AXOutline", _id="outline")
    # A different Python object, same element: parent of the window is the
    # outline it came from.
    window["AXParent"] = copy_of(outline)
    outline["AXParent"] = window
    api = FakeAxApi({7: ax_node("AXApplication", AXFocusedUIElement=outline)})

    ancestors = await ax.NativeAxBackend(api, 7).ancestry()

    assert [e.role for e in ancestors[:2]] == ["AXOutline", "AXWindow"]
    assert ancestors[-1].query_error == "<cycle: already seen>"
    assert len(ancestors) < ax.MAX_ANCESTRY_DEPTH


async def test_ancestry_is_empty_when_nothing_is_focused():
    api = FakeAxApi({7: ax_node("AXApplication")})

    assert await ax.NativeAxBackend(api, 7).ancestry() == []


async def test_ancestry_hides_a_secure_field_value():
    secure = ax_node("AXSecureTextField", AXValue="hunter2", _id="secure")
    api = FakeAxApi({7: ax_node("AXApplication", AXFocusedUIElement=secure)})

    ancestors = await ax.NativeAxBackend(api, 7).ancestry()

    assert ancestors[0].value == ax.SECURE_MARKER
    assert ("AXSecureTextField", "AXValue") not in api.reads


def test_the_parser_accepts_ancestry():
    args = ax.build_parser().parse_args(["--pid", "62868", "--ancestry"])

    assert args.ancestry is True
    assert args.pid == 62868


# --------------------------------------------------------------------------- #
# Delayed capture
# --------------------------------------------------------------------------- #
#
# Running the inspector from a terminal makes the terminal frontmost, which is
# enough to move the panel's focused element off the Go to Folder field and
# back onto its file list. --delay buys time to put focus back.


class DelayRecorder:
    """Records when the sleep happened, relative to the first query."""

    def __init__(self, api: FakeAxApi) -> None:
        self.api = api
        self.slept: list[float] = []
        self.reads_before_sleep: int | None = None

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.reads_before_sleep = len(self.api.reads)


def delayed_run_args(*extra: str) -> object:
    return ax.build_parser().parse_args(["--pid", "62868", "--delay", "5", *extra])


async def run_with(api: FakeAxApi, monkeypatch, *extra: str) -> DelayRecorder:
    recorder = DelayRecorder(api)
    monkeypatch.setattr(ax, "process_exists", lambda pid: True)
    monkeypatch.setattr(ax, "PyObjCAxApi", lambda: api)
    monkeypatch.setattr(ax.asyncio, "sleep", recorder.sleep)
    await ax.run(delayed_run_args(*extra))
    return recorder


def go_to_folder_field() -> dict[str, object]:
    """What the panel's path field looks like, as opposed to its search field."""
    return ax_node(
        "AXTextField",
        AXValue="/Users/someone",
        AXIdentifier="GoToFolderTextField",
        AXPlaceholderValue="Перейти до:",
        AXFocused=True,
        settable=True,
        attributes=["AXRole", "AXValue", "AXIdentifier", "AXPlaceholderValue"],
        actions=["AXConfirm"],
        _id="goto",
    )


def test_the_parser_accepts_a_delay():
    args = ax.build_parser().parse_args(["--pid", "62868", "--delay", "5", "--focused"])

    assert args.delay == 5.0
    assert args.focused is True


def test_the_delay_defaults_to_no_wait():
    assert ax.build_parser().parse_args(["--pid", "1"]).delay == 0.0


async def test_nothing_is_queried_before_the_delay_elapses(monkeypatch, capsys):
    """A query taken now would describe this terminal, not the dialog."""
    api = FakeAxApi({62868: ax_node("AXApplication", AXFocusedUIElement=go_to_folder_field())})

    recorder = await run_with(api, monkeypatch, "--focused")

    assert recorder.slept == [5.0]
    assert recorder.reads_before_sleep == 0, "the AX tree was read before waiting"
    assert api.reads, "and it was read afterwards"

    out = capsys.readouterr().out
    assert "Waiting 5 seconds." in out
    assert "press Cmd+Shift+G" in out


async def test_the_delay_applies_to_focused_mode(monkeypatch, capsys):
    api = FakeAxApi({62868: ax_node("AXApplication", AXFocusedUIElement=go_to_folder_field())})

    await run_with(api, monkeypatch, "--focused", "--detail")

    out = capsys.readouterr().out
    assert "AXFocusedUIElement (PID 62868)" in out
    assert "AXTextField" in out
    # The two fields that tell this control apart from the search field.
    assert "GoToFolderTextField" in out
    assert "Перейти до:" in out
    assert "AXConfirm" in out


async def test_the_delay_applies_to_ancestry_mode(monkeypatch, capsys):
    field = go_to_folder_field()
    api = FakeAxApi({62868: panel_with_ancestry([field, ax_node("AXSheet", _id="sheet")])})

    recorder = await run_with(api, monkeypatch, "--ancestry")

    assert recorder.reads_before_sleep == 0
    out = capsys.readouterr().out
    assert "Ancestry of AXFocusedUIElement (PID 62868)" in out
    assert "GoToFolderTextField" in out
    assert "AXSheet" in out


async def test_delay_zero_leaves_the_old_behaviour_alone(monkeypatch, capsys):
    api = FakeAxApi({7: ax_node("AXApplication", AXFocusedUIElement=go_to_folder_field())})
    recorder = DelayRecorder(api)
    monkeypatch.setattr(ax, "process_exists", lambda pid: True)
    monkeypatch.setattr(ax, "PyObjCAxApi", lambda: api)
    monkeypatch.setattr(ax.asyncio, "sleep", recorder.sleep)

    await ax.run(ax.build_parser().parse_args(["--pid", "7", "--focused"]))

    assert recorder.slept == [], "no delay means no waiting at all"
    assert "Waiting" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Identifier and placeholder
# --------------------------------------------------------------------------- #


async def test_identifier_and_placeholder_are_captured():
    """Role alone cannot tell the path field from the panel's search field."""
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[go_to_folder_field()])})

    walk = await ax.walk_pid(api, 7)
    element = walk.elements[0]

    assert element.identifier == "GoToFolderTextField"
    assert element.placeholder == "Перейти до:"
    assert element.value == "/Users/someone"


async def test_a_search_field_is_distinguishable_from_the_path_field():
    search = ax_node(
        "AXTextField",
        subrole="AXSearchField",
        AXIdentifier="",
        AXPlaceholderValue="Пошук",
        _id="search",
    )
    api = FakeAxApi(
        {7: ax_node("AXApplication", AXWindows=[search, go_to_folder_field()])}
    )

    walk = await ax.walk_pid(api, 7)

    assert [e.placeholder for e in walk.elements] == ["Пошук", "Перейти до:"]
    assert [e.identifier for e in walk.elements] == ["", "GoToFolderTextField"]


async def test_a_secure_field_still_gives_up_no_value():
    """Identifier and placeholder are metadata; the value is still off limits."""
    secure = ax_node(
        "AXSecureTextField",
        AXValue="hunter2",
        AXIdentifier="PasswordField",
        AXPlaceholderValue="Пароль",
        _id="secure",
    )
    api = FakeAxApi({7: ax_node("AXApplication", AXWindows=[secure])})

    walk = await ax.walk_pid(api, 7)
    element = walk.elements[0]

    assert element.value == ax.SECURE_MARKER
    assert ("AXSecureTextField", "AXValue") not in api.reads
    assert element.identifier == "PasswordField"
    assert element.placeholder == "Пароль"
    assert "hunter2" not in json.dumps(ax.build_report("x", walk, [], pid=7))


def test_the_name_mode_parser_reads_the_new_columns():
    """Appended, so an answer without them still parses."""
    row = node_row(
        "AXTextField",
        value="/",
        settable="true",
        attributes="AXRole,AXValue",
        actions="AXConfirm",
    ) + "\tGoToFolderTextField\tПерейти до:"
    element = ax.parse_node(row, element=ax.Element(id="1", parent_id=None, depth=0))

    assert element.identifier == "GoToFolderTextField"
    assert element.placeholder == "Перейти до:"

    short = ax.parse_node("AXGroup", element=ax.Element(id="1", parent_id=None, depth=0))
    assert short.identifier == ""
    assert short.placeholder == ""


# --------------------------------------------------------------------------- #
# The focused element's sheet
# --------------------------------------------------------------------------- #
#
# Written because the production selector read GoToWindow.AXDefaultButton —
# which the sheet advertises in its attribute names — and got nothing back.
# Advertised and present are different things, and the difference decides what
# the commit mechanism has to be.


def goto_sheet_app(
    *,
    default_button: dict[str, object] | None = None,
    advertise_default: bool = True,
    children: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """The live shape: PathTextField inside GoToWindow inside open-panel."""
    field = ax_node(
        "AXTextField",
        AXIdentifier="PathTextField",
        AXValue="/",
        settable=True,
        _id="path-field",
    )
    attributes = ["AXRole", "AXChildren", "AXCancelButton"]
    if advertise_default:
        attributes.insert(0, "AXDefaultButton")

    sheet = ax_node(
        "AXSheet",
        AXIdentifier="GoToWindow",
        AXDescription="перейти до",
        _id="goto",
        attributes=attributes,
        children=children if children is not None else [field],
    )
    if default_button is not None:
        sheet["AXDefaultButton"] = default_button

    panel = ax_node("AXSheet", AXIdentifier="open-panel", _id="panel")
    field["AXParent"] = sheet
    sheet["AXParent"] = panel
    return ax_node("AXApplication", AXWindows=[], AXFocusedUIElement=field, _id="app")


async def test_the_first_sheet_above_the_focused_element_is_chosen():
    api = FakeAxApi({7: goto_sheet_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert report.sheet is not None
    assert report.sheet.role == "AXSheet"
    assert report.sheet.identifier == "GoToWindow"
    assert report.sheet.description == "перейти до"
    # Not the open-panel sheet above it.
    assert report.sheet.identifier != "open-panel"


async def test_the_sheets_direct_children_are_enumerated():
    children = [
        ax_node("AXStaticText", AXValue="Перейти до:", _id="label"),
        ax_node("AXTextField", AXIdentifier="PathTextField", settable=True, _id="field"),
        ax_node("AXButton", AXTitle="Go", _id="go", actions=["AXPress"]),
        ax_node("AXButton", AXTitle="Cancel", _id="cancel", actions=["AXPress"]),
    ]
    api = FakeAxApi({7: goto_sheet_app(children=children)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert len(report.children) == 4
    assert [child.role for child in report.children] == [
        "AXStaticText", "AXTextField", "AXButton", "AXButton"
    ]
    assert [child.id for child in report.children] == [
        "sheet.1", "sheet.2", "sheet.3", "sheet.4"
    ]
    assert report.children[2].action_names == ["AXPress"]


async def test_an_advertised_but_missing_default_button_is_reported_as_such():
    """The live case: the name is listed, the value is nil."""
    api = FakeAxApi({7: goto_sheet_app(default_button=None, advertise_default=True)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()
    default = next(b for b in report.buttons if b.attribute == "AXDefaultButton")

    assert default.advertised is True
    assert default.element is None
    assert "advertised in attribute names, but missing" in default.describe()


async def test_an_unadvertised_missing_button_reads_differently():
    api = FakeAxApi({7: goto_sheet_app(advertise_default=False)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()
    default = next(b for b in report.buttons if b.attribute == "AXDefaultButton")

    assert default.advertised is False
    assert default.element is None
    assert "not advertised either" in default.describe()


async def test_a_present_default_button_is_described():
    button = ax_node(
        "AXButton",
        AXIdentifier="GoButton",
        AXTitle="Перейти",
        AXEnabled=True,
        actions=["AXPress"],
        _id="go-button",
    )
    api = FakeAxApi({7: goto_sheet_app(default_button=button)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()
    default = next(b for b in report.buttons if b.attribute == "AXDefaultButton")

    assert default.element is not None
    assert default.element.role == "AXButton"
    assert default.element.identifier == "GoButton"
    assert default.element.title == "Перейти"
    assert default.element.action_names == ["AXPress"]
    assert "AXButton" in default.describe()


async def test_the_cancel_button_is_reported_the_same_way():
    cancel = ax_node("AXButton", AXTitle="Скасувати", _id="cancel", actions=["AXPress"])
    app = goto_sheet_app()
    app["AXFocusedUIElement"]["AXParent"]["AXCancelButton"] = cancel  # type: ignore[index]
    api = FakeAxApi({7: app})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()
    reported = next(b for b in report.buttons if b.attribute == "AXCancelButton")

    assert reported.element is not None
    assert reported.element.title == "Скасувати"


async def test_one_extra_level_is_shown_for_small_children():
    group = ax_node(
        "AXGroup",
        _id="group",
        children=[ax_node("AXButton", AXTitle="inner", _id="inner")],
    )
    api = FakeAxApi({7: goto_sheet_app(children=[group])})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert report.grandchildren["sheet.1"][0].title == "inner"


async def test_a_large_child_is_not_expanded():
    """One extra level, not a traversal of the whole panel."""
    crowd = ax_node(
        "AXOutline",
        _id="outline",
        children=[ax_node("AXRow", _id=f"row{i}") for i in range(40)],
    )
    api = FakeAxApi({7: goto_sheet_app(children=[crowd])})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert report.children[0].child_count == 40
    assert "sheet.1" not in report.grandchildren


async def test_nothing_focused_gives_an_empty_report():
    api = FakeAxApi({7: ax_node("AXApplication")})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert report.sheet is None
    assert report.children == []


async def test_a_secure_field_in_the_sheet_keeps_its_value():
    secure = ax_node("AXSecureTextField", AXValue="hunter2", _id="secure")
    api = FakeAxApi({7: goto_sheet_app(children=[secure])})

    report = await ax.NativeAxBackend(api, 7).focused_sheet()

    assert report.children[0].value == ax.SECURE_MARKER
    assert ("AXSecureTextField", "AXValue") not in api.reads


async def test_the_sheet_diagnostic_performs_no_actions_and_writes_nothing():
    """Inspection only: it looks at a commit control, it does not use one."""
    button = ax_node("AXButton", _id="go", actions=["AXPress"])
    api = FakeAxApi({7: goto_sheet_app(default_button=button)})

    await ax.NativeAxBackend(api, 7).focused_sheet()

    # The inspector's API surface has no way to act or write at all.
    assert not hasattr(api, "performed")
    assert not any(name in dir(ax.NativeAxBackend) for name in ("press", "confirm"))


async def test_the_delay_applies_to_the_sheet_diagnostic(monkeypatch, capsys):
    api = FakeAxApi({62868: goto_sheet_app()})

    recorder = await run_with(api, monkeypatch, "--focused-sheet-children")

    assert recorder.reads_before_sleep == 0
    out = capsys.readouterr().out
    assert "Sheet containing AXFocusedUIElement (PID 62868)" in out
    assert "GoToWindow" in out
    assert "AXDefaultButton" in out


def test_the_parser_accepts_the_sheet_diagnostic():
    args = ax.build_parser().parse_args(["--pid", "1", "--focused-sheet-children"])

    assert args.focused_sheet_children is True


# --------------------------------------------------------------------------- #
# Sheet semantics
# --------------------------------------------------------------------------- #
#
# Both button attributes came back nil and the field's AXConfirm does nothing,
# so what is left to read is AXSections and the completion table.


def completion_table(rows: int = 2, *, row_actions: list[str] | None = None) -> dict:
    cells = [
        ax_node(
            "AXRow",
            _id=f"row{index}",
            AXSelected=index == 1,
            AXIndex=index,
            actions=row_actions if row_actions is not None else [],
            attributes=["AXRole", "AXSelected", "AXIndex", "AXChildren"],
            children=[
                ax_node("AXCell", _id=f"cell{index}", AXValue=f"file-{index}.dat")
            ],
        )
        for index in range(1, rows + 1)
    ]
    table = ax_node(
        "AXTable",
        _id="table",
        AXRows=cells,
        AXSelectedRows=[cells[0]],
        AXVisibleRows=cells,
        attributes=["AXRole", "AXRows", "AXSelectedRows", "AXVisibleRows", "AXChildren"],
        children=cells,
    )
    return ax_node("AXScrollArea", _id="scroll", children=[table])


def semantics_app(
    *,
    sections: object = None,
    advertise_sections: bool = True,
    table: dict | None = None,
    extra_children: list[dict] | None = None,
) -> dict:
    field = ax_node(
        "AXTextField",
        AXIdentifier="PathTextField",
        AXValue="/private/var/folders/qg/tmp2js8vf8b/hsc-smoke-test.txt",
        settable=True,
        actions=["AXShowMenu", "AXConfirm"],
        _id="path-field",
    )
    attributes = ["AXRole", "AXChildren", "AXIdentifier"]
    if advertise_sections:
        attributes.append("AXSections")

    children = [field, table if table is not None else completion_table()]
    children.extend(extra_children or [])
    sheet = ax_node(
        "AXSheet",
        AXIdentifier="GoToWindow",
        _id="goto",
        attributes=attributes,
        children=children,
    )
    if sections is not None:
        sheet["AXSections"] = sections

    field["AXParent"] = sheet
    return ax_node("AXApplication", AXWindows=[], AXFocusedUIElement=field, _id="app")


async def test_the_sheet_is_identified_as_the_goto_window():
    api = FakeAxApi({7: semantics_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sheet is not None
    assert report.is_goto_window is True
    assert report.sheet.identifier == "GoToWindow"


async def test_a_nil_sections_attribute_is_reported():
    api = FakeAxApi({7: semantics_app(sections=None, advertise_sections=True)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sections is not None
    assert report.sections.advertised is True
    assert report.sections.present is False
    assert report.sections.describe() == "AXSections: nil"


async def test_an_unadvertised_nil_sections_reads_differently():
    api = FakeAxApi({7: semantics_app(advertise_sections=False)})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sections is not None
    assert "not advertised either" in report.sections.describe()


async def test_sections_containing_elements_are_described():
    api = FakeAxApi(
        {
            7: semantics_app(
                sections=[
                    ax_node("AXGroup", AXIdentifier="section-1", _id="s1"),
                    ax_node("AXButton", AXTitle="Go", _id="s2", actions=["AXPress"]),
                ]
            )
        }
    )

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sections is not None
    assert report.sections.present is True
    assert report.sections.summary == "2 element(s)"
    assert [element.role for element in report.sections.elements] == [
        "AXGroup", "AXButton"
    ]
    assert report.sections.elements[1].action_names == ["AXPress"]


async def test_a_scalar_attribute_is_summarised_rather_than_described():
    api = FakeAxApi({7: semantics_app(sections="two sections")})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sections is not None
    assert report.sections.summary == "two sections"
    assert report.sections.elements == []


async def test_the_table_subtree_is_walked_within_its_bounds():
    api = FakeAxApi({7: semantics_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics(max_depth=4)
    roles = [node.element.role for node in report.subtree]

    assert "AXScrollArea" in roles
    assert "AXTable" in roles
    assert "AXRow" in roles
    assert "AXCell" in roles


async def test_the_walk_stops_at_the_configured_depth():
    api = FakeAxApi({7: semantics_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics(max_depth=2)
    roles = [node.element.role for node in report.subtree]

    assert "AXTable" in roles
    assert "AXCell" not in roles, "depth 2 stops above the cells"


async def test_the_walk_stops_at_the_element_budget():
    api = FakeAxApi({7: semantics_app(table=completion_table(rows=50))})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics(max_elements=8)

    assert len(report.subtree) == 8


async def test_selection_metadata_is_surfaced():
    api = FakeAxApi({7: semantics_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()
    table = next(node for node in report.subtree if node.element.role == "AXTable")
    selected = next(a for a in table.attributes if a.name == "AXSelectedRows")

    assert selected.present is True
    assert selected.summary == "1 element(s)"
    assert selected.elements[0].role == "AXRow"

    rows = [node for node in report.subtree if node.element.role == "AXRow"]
    assert any(
        attribute.name == "AXSelected" and attribute.summary == "True"
        for attribute in rows[0].attributes
    )
    assert any(
        attribute.name == "AXIndex" and attribute.summary == "1"
        for attribute in rows[0].attributes
    )


async def test_activation_actions_are_surfaced():
    """The whole question: is there anything here that would commit the path?"""
    api = FakeAxApi(
        {7: semantics_app(table=completion_table(row_actions=["AXPress", "AXShowMenu"]))}
    )

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    rows = [
        node for node in report.activation_candidates if node.element.role == "AXRow"
    ]
    assert rows, "rows offering AXPress must be reported"
    assert all("AXPress" in node.activation_actions for node in rows)
    # AXShowMenu opens a menu; it is not an activation.
    assert all("AXShowMenu" not in node.activation_actions for node in rows)


async def test_rows_without_an_activation_action_are_not_listed_as_candidates():
    api = FakeAxApi({7: semantics_app(table=completion_table(row_actions=[]))})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert not [
        node for node in report.activation_candidates if node.element.role == "AXRow"
    ]
    # The path field still is one: it offers AXConfirm, which is exactly the
    # action we know reports success and commits nothing.
    assert [node.element.role for node in report.activation_candidates] == [
        "AXTextField"
    ]


async def test_directories_are_redacted_from_reported_values():
    """The panel currently holds a real path; the basename is the useful part."""
    api = FakeAxApi({7: semantics_app()})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()
    values = [node.element.value for node in report.subtree]

    assert "…/hsc-smoke-test.txt" in values
    assert not any("/private/var" in value for value in values)


def test_the_redaction_leaves_ordinary_values_alone():
    assert ax.redact_path("Перейти до:") == "Перейти до:"
    assert ax.redact_path("") == ""
    assert ax.redact_path("/etc/hosts") == "…/hosts"
    assert ax.redact_path("~/keys/Key-6.dat") == "…/Key-6.dat"


async def test_a_secure_field_in_the_subtree_keeps_its_value():
    secure = ax_node("AXSecureTextField", AXValue="hunter2", _id="secure")
    api = FakeAxApi({7: semantics_app(extra_children=[secure])})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()
    values = [node.element.value for node in report.subtree]

    assert ax.SECURE_MARKER in values
    assert ("AXSecureTextField", "AXValue") not in api.reads


async def test_the_semantics_diagnostic_writes_and_performs_nothing():
    api = FakeAxApi(
        {7: semantics_app(table=completion_table(row_actions=["AXPress"]))}
    )

    await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    # This fake has no write or action surface at all — the inspector's API
    # protocol does not include them, so it could not act even by mistake.
    assert not hasattr(api, "set_attribute_value")
    assert not hasattr(api, "perform_action")


async def test_nothing_focused_gives_an_empty_semantics_report():
    api = FakeAxApi({7: ax_node("AXApplication")})

    report = await ax.NativeAxBackend(api, 7).focused_sheet_semantics()

    assert report.sheet is None
    assert report.subtree == []


async def test_the_delay_applies_to_the_semantics_diagnostic(monkeypatch, capsys):
    api = FakeAxApi({62868: semantics_app()})

    recorder = await run_with(api, monkeypatch, "--focused-sheet-semantics")

    assert recorder.reads_before_sleep == 0
    out = capsys.readouterr().out
    assert "Sheet semantics (PID 62868)" in out
    assert "AXSections" in out
    assert "Nothing was pressed, confirmed or written." in out


def test_the_parser_accepts_the_semantics_diagnostic():
    args = ax.build_parser().parse_args(["--pid", "1", "--focused-sheet-semantics"])

    assert args.focused_sheet_semantics is True


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def element(**kwargs: object) -> ax.Element:
    base: dict[str, object] = {"id": "1", "parent_id": None, "depth": 0}
    return ax.Element(**{**base, **kwargs})  # type: ignore[arg-type]


def test_the_role_filter_selects_exactly_that_role():
    field = element(role="AXTextField")
    group = element(role="AXGroup")

    assert ax.matches(field, role="AXTextField")
    assert not ax.matches(group, role="AXTextField")


def test_the_contains_filter_searches_the_visible_metadata():
    target = element(role="AXStaticText", title="Перейти до:")

    assert ax.matches(target, contains="Перейти")
    assert ax.matches(target, contains="перейти"), "case-insensitive"
    assert ax.matches(element(value="/"), contains="/")
    assert not ax.matches(target, contains="Відкрити")


def test_editable_only_uses_what_the_system_reports_first():
    settable = element(role="AXGroup", value_settable="true")
    by_action = element(role="AXGroup", action_names=["AXConfirm"])
    by_role = element(role="AXComboBox")
    plain = element(role="AXStaticText")

    assert ax.matches(settable, editable_only=True)
    assert ax.matches(by_action, editable_only=True)
    assert ax.matches(by_role, editable_only=True)
    assert not ax.matches(plain, editable_only=True)


def test_filters_combine():
    target = element(role="AXComboBox", title="Перейти до:")

    assert ax.matches(target, role="AXComboBox", contains="Перейти", editable_only=True)
    assert not ax.matches(target, role="AXTextField", contains="Перейти")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


async def test_the_report_carries_everything_needed_to_read_it_later(tmp_path):
    runner = FakeOsascript(
        {
            "1": node_row("AXWindow", title="Open", children=1),
            "1.1": node_row("AXTextField", value="/Users", settable="true"),
        }
    )
    walk = await ax.walk_tree(runner, "Chrome")
    windows = [e for e in walk.elements if e.depth == 0]

    report = ax.build_report("Chrome", walk, windows)
    path = tmp_path / "debug" / "mac-ax.json"
    ax.write_json(path, report)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["process"] == "Chrome"
    assert written["complete"] is True
    assert written["stop_reason"] == "complete"
    assert written["timestamp"]
    assert len(written["windows"]) == 1
    assert {
        "id", "parent_id", "depth", "role", "subrole", "title", "description",
        "value", "enabled", "focused", "attribute_names", "action_names",
        "child_count", "query_error", "value_settable",
    } <= set(written["elements"][0])


async def test_a_partial_walk_is_still_written(tmp_path):
    """Losing the evidence to a timeout would be the worst outcome."""
    runner = FakeOsascript({"1": node_row("AXWindow", children=99)})
    walk = await ax.walk_tree(runner, "Chrome", max_elements=3)

    path = tmp_path / "mac-ax.json"
    ax.write_json(path, ax.build_report("Chrome", walk, []))

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["complete"] is False
    assert "max-elements" in written["stop_reason"]
    assert len(written["elements"]) == 3


# --------------------------------------------------------------------------- #
# Focused element
# --------------------------------------------------------------------------- #


async def test_a_missing_focused_element_is_reported_as_such():
    """The symptom that started this: Chrome reports nothing focused."""
    assert await ax.describe_focused(FakeOsascript(focused="none"), "Chrome") is None


async def test_a_focused_element_is_described_in_full():
    runner = FakeOsascript(
        focused=node_row("AXTextField", attributes="AXValue,AXFocused", actions="AXConfirm")
    )

    focused = await ax.describe_focused(runner, "Chrome")

    assert focused is not None
    assert focused.role == "AXTextField"
    assert focused.action_names == ["AXConfirm"]


# --------------------------------------------------------------------------- #
# Inspection only
# --------------------------------------------------------------------------- #


def test_no_accessibility_action_is_ever_performed():
    """It lists AXPress and friends. It must never do them."""
    for script in applescript_constants():
        assert "perform action" not in script
        assert "perform " not in script
        assert "keystroke" not in script
        assert "key code" not in script
        # Reading the names of the actions is the whole point, though.
    assert "name of actions of node" in ax._NODE

    # The native path is read-only by construction: there is no such call.
    native = (PROJECT_ROOT / "scripts" / "mac_ax_inspector.py").read_text(
        encoding="utf-8"
    )
    assert "AXUIElementPerformAction" not in native
    assert "AXUIElementSetAttributeValue" not in native


def test_the_parser_accepts_a_pid():
    args = ax.build_parser().parse_args(["--pid", "59478", "--tree"])

    assert args.pid == 59478
    assert args.process is None


def test_process_and_pid_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        ax.build_parser().parse_args(["--pid", "1", "--process", "Finder"])

    assert "not allowed with argument" in capsys.readouterr().err


def test_every_mode_works_with_a_pid():
    parser = ax.build_parser()

    for extra in (
        ["--windows"],
        ["--tree"],
        ["--focused"],
        ["--role", "AXTextField"],
        ["--contains", "Перейти"],
        ["--editable-only"],
        ["--detail"],
        ["--include-web-area"],
        ["--json", "data/debug/mac-ax-59478.json"],
    ):
        args = parser.parse_args(["--pid", "59478", *extra])
        assert args.pid == 59478


async def test_the_report_carries_the_pid(tmp_path):
    api = FakeAxApi({59478: panel_app("Open")})
    walk = await ax.walk_pid(api, 59478)

    path = tmp_path / "mac-ax-59478.json"
    ax.write_json(path, ax.build_report("panel", walk, [], pid=59478))
    written = json.loads(path.read_text(encoding="utf-8"))

    assert written["pid"] == 59478
    assert written["process"] == "panel"


def test_the_process_report_keeps_its_shape_without_a_pid():
    """Name mode stays backwards compatible; pid is simply null."""
    report = ax.build_report("Finder", ax.Walk(), [])

    assert report["pid"] is None
    assert report["process"] == "Finder"
    assert {"timestamp", "complete", "stop_reason", "windows", "elements"} <= set(report)


async def test_duplicate_process_names_are_distinguishable_by_pid():
    runner = FakeOsascript(
        processes="\n".join(
            [
                "com.apple.appkit.xpc.openAndSavePanelService\tfalse\tfalse\t59478",
                "com.apple.appkit.xpc.openAndSavePanelService\tfalse\tfalse\t2566",
                "Google Chrome for Testing\ttrue\ttrue\t58579",
            ]
        )
    )

    processes = await ax.list_processes(runner)

    assert [p.pid for p in processes] == [59478, 2566, 58579]
    assert processes[0].name == processes[1].name, "the names really are identical"
    assert len({p.pid for p in processes}) == 3


def test_the_defaults_are_the_documented_bounds():
    args = ax.build_parser().parse_args([])

    assert args.max_depth == ax.DEFAULT_MAX_DEPTH == 12
    assert args.max_elements == ax.DEFAULT_MAX_ELEMENTS == 500
    assert args.query_timeout == ax.DEFAULT_QUERY_TIMEOUT == 3.0
    assert args.include_web_area is False
    assert args.process is None


def test_the_documented_invocations_parse():
    parser = ax.build_parser()

    for argv in (
        ["--process", "Google Chrome for Testing"],
        ["--process", "Google Chrome for Testing", "--tree"],
        ["--process", "Google Chrome for Testing", "--windows"],
        ["--process", "Google Chrome for Testing", "--contains", "Перейти"],
        ["--process", "Google Chrome for Testing", "--editable-only"],
        ["--process", "X", "--tree", "--json", "data/debug/mac-ax.json"],
        ["--process", "X", "--focused"],
        ["--process", "X", "--role", "AXTextField"],
    ):
        parser.parse_args(argv)


def test_it_refuses_to_run_off_macos(monkeypatch, capsys):
    monkeypatch.setattr(ax.sys, "platform", "linux")

    assert ax.main([]) == 2
    assert "only works on macOS" in capsys.readouterr().err


def test_nothing_in_the_project_imports_the_inspector():
    """A debug tool that production depends on stops being a debug tool."""
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        assert "mac_ax_inspector" not in path.read_text(encoding="utf-8"), path
