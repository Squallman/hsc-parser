#!/usr/bin/env python3
"""Inspect the real macOS Accessibility hierarchy of a running GUI process.

A standalone local debug tool. Nothing in ``hsc_queue_monitor`` imports it, and
it must never be called from production code — it exists because two heuristics
have now failed against the native file dialog (``AXFocusedUIElement`` reports
nothing; the roles we expected were not where we expected them), and the next
move is to look at what is actually there.

It is inspection-only: attributes and actions are *listed*, never performed.

Recommended workflow
--------------------

A. Open Google Chrome for Testing by hand.
B. Trigger a file Open dialog (any page with a file input will do).
C. Press ⌘⇧G so "Go to Folder" is visible.
D. Leave the dialog open.
E. In another terminal, list the processes to find the right PID:

    python scripts/mac_ax_inspector.py

   then inspect one of them:

    python scripts/mac_ax_inspector.py --pid 59478 --windows
    python scripts/mac_ax_inspector.py --pid 59478 --tree \\
        --json data/debug/mac-ax-59478.json

Also useful:

    python scripts/mac_ax_inspector.py --pid 59478 --contains "Перейти"
    python scripts/mac_ax_inspector.py --pid 59478 --editable-only
    python scripts/mac_ax_inspector.py --pid 59478 --focused
    python scripts/mac_ax_inspector.py --pid 59478 --ancestry

Catching a control that only exists while focused
-------------------------------------------------

Running this from a terminal makes the *terminal* frontmost, which is enough to
move the panel's focused element off the Go to Folder field and back onto its
file list — so a static dump never contains the path field at all. ``--delay``
buys time to put focus back before anything is asked:

    python scripts/mac_ax_inspector.py --pid 59478 --delay 5 --focused --detail

During the wait: switch to the dialog, press ⌘⇧G, click inside the path field
and leave it focused. Nothing is queried until the delay has elapsed.

``AXIdentifier`` and ``AXPlaceholderValue`` are reported for every element,
because role alone cannot tell the panel's ordinary search field from the path
field — they share ``AXTextField``.

When AXWindows is empty
-----------------------

An application root does not always publish its panel through ``AXWindows``.
Observed live: an Open panel process whose ``AXWindows`` is empty, but whose
``AXFocusedUIElement`` is an ``AXOutline`` carrying ``AXWindow`` and
``AXTopLevelUIElement``. The panel is there — the obvious question just does not
reach it.

So PID mode falls back: ``AXWindows`` → ``AXFocusedUIElement.AXWindow`` →
``AXFocusedUIElement.AXTopLevelUIElement``, and records which route worked as
``root_source``. ``--ancestry`` shows the whole chain upwards from the focused
element, which is how to find out where a control actually lives.

Name or PID
-----------

``--process`` addresses a process the way System Events does, by name. That is
fine for a uniquely named application and useless for anything else: this
machine runs two processes called
``com.apple.appkit.xpc.openAndSavePanelService``, and ``tell process "<name>"``
picks one of them without saying which — which is exactly how a query comes
back with zero windows while the dialog is plainly on screen.

``--pid`` avoids that entirely. It goes through the native Accessibility API
(``AXUIElementCreateApplication(pid)``) and never resolves the id back into a
name, so it can only ever inspect the process asked for. It needs PyObjC:

    .venv/bin/pip install pyobjc-framework-ApplicationServices

How it avoids hanging
---------------------

``entire contents`` is never used: it has already hung on this browser. Python
drives the traversal in both modes, asking for one node's metadata and its
*direct* children at a time — by index path (``2.1.3`` = UI element 3 of UI
element 1 of window 2) for System Events, by ``AXChildren`` for the native API.
Every osascript subprocess has its own timeout and the native calls carry an
``AXUIElementSetMessagingTimeout``, so one bad element costs one
``<query timeout>`` row rather than the whole run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from hsc_queue_monitor.browser import macos_ax
from hsc_queue_monitor.models import AccessibilityUnavailable

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_ELEMENTS = 500
DEFAULT_QUERY_TIMEOUT = 3.0

#: Values are metadata here, not content. Long ones are clipped.
MAX_VALUE_CHARS = 200

#: Never read: whatever is in one of these is a password by definition.
SECURE_ROLES = frozenset({"AXSecureTextField"})

#: The browser page. Huge, irrelevant to a file dialog, and full of content
#: that has no business in a diagnostic. Traversed only on request.
WEB_AREA_ROLE = "AXWebArea"

#: What "looks editable" means when nothing states it outright.
EDITABLE_ROLES = frozenset(
    {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSecureTextField"}
)
EDITABLE_ACTIONS = frozenset({"AXSetValue", "AXConfirm"})

QUERY_TIMEOUT_MARKER = "<query timeout>"
SECURE_MARKER = "<secure>"

# --------------------------------------------------------------------------- #
# AppleScript
# --------------------------------------------------------------------------- #
#
# Small, single-purpose scripts. Everything variable — the process name, the
# node path — arrives as an argv argument, never as interpolated source.

_PROCESSES = """
on run argv
    set out to {}
    tell application "System Events"
        repeat with p in application processes
            try
                set nm to name of p
            on error
                set nm to "?"
            end try
            set fr to "false"
            try
                if frontmost of p then set fr to "true"
            end try
            set vi to "false"
            try
                if visible of p then set vi to "true"
            end try
            set pid to ""
            try
                set pid to (unix id of p) as text
            end try
            -- Deliberately no window count: that is one accessibility query
            -- per process, and it pushes the listing past any sane timeout.
            set end of out to nm & tab & fr & tab & vi & tab & pid
        end repeat
    end tell
    set AppleScript's text item delimiters to linefeed
    set answer to out as text
    set AppleScript's text item delimiters to ""
    return answer
end run
"""

_WINDOW_COUNT = """
on run argv
    tell application "System Events"
        if not (exists process (item 1 of argv)) then return "no-process"
        return (count of windows of process (item 1 of argv)) as text
    end tell
end run
"""

#: One node, addressed by index path. Direct children are counted, never walked.
_NODE = """
on nodeAt(procName, pathText)
    set AppleScript's text item delimiters to "."
    set idx to text items of pathText
    set AppleScript's text item delimiters to ""
    tell application "System Events"
        tell process procName
            set node to window ((item 1 of idx) as integer)
            if (count of idx) > 1 then
                repeat with k from 2 to (count of idx)
                    set node to UI element ((item k of idx) as integer) of node
                end repeat
            end if
            return node
        end tell
    end tell
end nodeAt

on cleanText(t)
    set s to ""
    try
        set s to t as text
    on error
        return ""
    end try
    if (count of s) > 200 then set s to text 1 thru 200 of s
    set out to ""
    repeat with c in characters of s
        set ch to c as text
        if ch is tab or ch is return or ch is linefeed then
            set out to out & " "
        else
            set out to out & ch
        end if
    end repeat
    return out
end cleanText

on attributeText(node, attrName)
    tell application "System Events"
        try
            return my cleanText(value of attribute attrName of node)
        on error
            return ""
        end try
    end tell
end attributeText

on describe(node)
    tell application "System Events"
        set theRole to my attributeText(node, "AXRole")
        set theSubrole to my attributeText(node, "AXSubrole")
        set theTitle to my attributeText(node, "AXTitle")
        set theDescription to my attributeText(node, "AXDescription")

        set attrNames to ""
        try
            set AppleScript's text item delimiters to ","
            set attrNames to (name of attributes of node) as text
            set AppleScript's text item delimiters to ""
        end try

        set actionNames to ""
        try
            set AppleScript's text item delimiters to ","
            set actionNames to (name of actions of node) as text
            set AppleScript's text item delimiters to ""
        end try

        set theValue to ""
        if theRole is not "AXSecureTextField" then
            set theValue to my attributeText(node, "AXValue")
        end if

        set theSettable to ""
        try
            set theSettable to (settable of attribute "AXValue" of node) as text
        end try

        set theEnabled to my attributeText(node, "AXEnabled")
        set theFocused to my attributeText(node, "AXFocused")

        set kids to "0"
        try
            set kids to (count of UI elements of node) as text
        end try

        -- Appended, not inserted: the reader pads short rows, so an older
        -- answer still parses.
        set theIdentifier to my attributeText(node, "AXIdentifier")
        set thePlaceholder to my attributeText(node, "AXPlaceholderValue")
    end tell
    return theRole & tab & theSubrole & tab & theTitle & tab & theDescription & ¬
        tab & theValue & tab & theEnabled & tab & theFocused & tab & theSettable & ¬
        tab & kids & tab & attrNames & tab & actionNames & tab & theIdentifier & ¬
        tab & thePlaceholder
end describe

on run argv
    return my describe(my nodeAt(item 1 of argv, item 2 of argv))
end run
"""

#: The focused element, if System Events admits to having one.
_FOCUSED = _NODE.replace(
    """on run argv
    return my describe(my nodeAt(item 1 of argv, item 2 of argv))
end run""",
    """on run argv
    tell application "System Events"
        try
            set node to value of attribute "AXFocusedUIElement" of process (item 1 of argv)
        on error
            return "none"
        end try
        if node is missing value then return "none"
    end tell
    return my describe(node)
end run""",
)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Process:
    name: str
    frontmost: bool
    visible: bool
    #: 0 when System Events would not say. Names are not unique — this machine
    #: runs two processes called com.apple.appkit.xpc.openAndSavePanelService —
    #: so the PID is what actually identifies one of them.
    pid: int = 0


@dataclass(slots=True)
class Element:
    """One node of the accessibility tree, as this tool saw it."""

    id: str
    parent_id: str | None
    depth: int
    role: str = ""
    subrole: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    #: AXIdentifier and AXPlaceholderValue. Metadata, not content — and the
    #: only things that reliably tell the panel's ordinary search field apart
    #: from the Go to Folder path field, which share a role.
    identifier: str = ""
    placeholder: str = ""
    enabled: str = ""
    focused: str = ""
    value_settable: str = ""
    child_count: int = 0
    attribute_names: list[str] = field(default_factory=list)
    action_names: list[str] = field(default_factory=list)
    query_error: str = ""

    @property
    def looks_editable(self) -> bool:
        """Whether this element could plausibly be written to.

        Preference order: what the system says (``settable``), then what it
        offers (``AXSetValue``/``AXConfirm``), then the role. Nothing here is
        acted on — it only decides what ``--editable-only`` shows.
        """
        if self.value_settable.lower() == "true":
            return True
        if EDITABLE_ACTIONS & set(self.action_names):
            return True
        return self.role in EDITABLE_ROLES

    def one_line(self) -> str:
        bits = [f"{'  ' * self.depth}[{self.id}] {self.role or '?'}"]
        if self.subrole:
            bits.append(f"({self.subrole})")
        for label, value in (
            ("title", self.title),
            ("desc", self.description),
            ("value", self.value),
            ("id", self.identifier),
            ("placeholder", self.placeholder),
        ):
            if value:
                bits.append(f"{label}={value!r}")
        if self.focused.lower() == "true":
            bits.append("FOCUSED")
        if self.looks_editable:
            bits.append("EDITABLE")
        if self.child_count:
            bits.append(f"children={self.child_count}")
        if self.query_error:
            bits.append(self.query_error)
        return " ".join(bits)

    def detail(self) -> str:
        return (
            f"{self.one_line()}\n"
            f"{'  ' * self.depth}    settable(AXValue)={self.value_settable or '?'} "
            f"enabled={self.enabled or '?'}\n"
            f"{'  ' * self.depth}    attributes: "
            f"{', '.join(self.attribute_names) or '(none reported)'}\n"
            f"{'  ' * self.depth}    actions:    "
            f"{', '.join(self.action_names) or '(none reported)'}"
        )


@dataclass(slots=True)
class ButtonReport:
    """One button-valued attribute of a sheet, in three possible states."""

    attribute: str
    #: Whether the sheet lists the attribute at all.
    advertised: bool
    #: What reading it returned. ``None`` means nothing — which, as the live
    #: Go to Folder sheet showed, is entirely possible while still advertised.
    element: Element | None = None

    def describe(self) -> str:
        if self.element is not None:
            return f"{self.attribute}:\n{self.element.detail()}"
        if self.advertised:
            return f"{self.attribute}: advertised in attribute names, but missing (nil)"
        return f"{self.attribute}: missing (not advertised either)"


@dataclass(slots=True)
class AttributeReport:
    """One attribute read for real, rather than assumed from its name.

    ``advertised`` and ``present`` are separate answers because a sheet was
    observed listing AXDefaultButton and then returning nothing for it.
    """

    name: str
    advertised: bool
    present: bool
    summary: str = ""
    elements: list[Element] = field(default_factory=list)

    def describe(self) -> str:
        if not self.present:
            state = "nil" if self.advertised else "nil (not advertised either)"
            return f"{self.name}: {state}"
        return f"{self.name}: {self.summary}"


@dataclass(slots=True)
class SubtreeNode:
    element: Element
    attributes: list[AttributeReport] = field(default_factory=list)

    @property
    def activation_actions(self) -> list[str]:
        return [
            action for action in self.element.action_names if action in ACTIVATION_ACTIONS
        ]


@dataclass(slots=True)
class SemanticsReport:
    """What a sheet exposes beyond its children: sections, table, selection."""

    sheet: Element | None
    is_goto_window: bool = False
    sections: AttributeReport | None = None
    attributes: list[AttributeReport] = field(default_factory=list)
    subtree: list[SubtreeNode] = field(default_factory=list)

    @property
    def activation_candidates(self) -> list[SubtreeNode]:
        return [node for node in self.subtree if node.activation_actions]


@dataclass(slots=True)
class SheetReport:
    """A sheet, its button attributes, and one level of what is inside it."""

    sheet: Element | None
    buttons: list[ButtonReport] = field(default_factory=list)
    children: list[Element] = field(default_factory=list)
    grandchildren: dict[str, list[Element]] = field(default_factory=dict)


@dataclass(slots=True)
class Walk:
    elements: list[Element] = field(default_factory=list)
    complete: bool = True
    stop_reason: str = "complete"
    #: Which route found the roots — see the ROOT_* constants. Worth recording:
    #: "the panel was reached by climbing from the focused element" is a
    #: finding, not an implementation detail.
    root_source: str = ""
    root_role: str = ""
    root_description: str = ""
    #: The container the root sits inside. Distinct from the root on purpose:
    #: reporting the sheet as the root is what hid an empty walk.
    enclosing_role: str = ""
    enclosing_description: str = ""
    #: Whether the process had a focused element, whatever came of it.
    focused_present: bool = False


# --------------------------------------------------------------------------- #
# Running osascript
# --------------------------------------------------------------------------- #


class Osascript:
    """Runs one small AppleScript per call, each with its own timeout."""

    def __init__(self, timeout: float = DEFAULT_QUERY_TIMEOUT) -> None:
        self.timeout = timeout

    async def run(self, script: str, *arguments: str, timeout: float | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(script.encode("utf-8")),
                timeout=timeout if timeout is not None else self.timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise OSError(stderr.decode("utf-8", "replace").strip() or "osascript failed")
        return stdout.decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def clip(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= MAX_VALUE_CHARS else collapsed[:MAX_VALUE_CHARS] + "…"


def redact_path(value: str) -> str:
    """Keep a filename, drop the directories it sits in.

    Where someone keeps their files is theirs; the basename is what identifies
    what was selected. Applied to values in the semantic report, which is read
    from a panel that currently holds a real path.
    """
    if value.startswith(("/", "~")) and "/" in value[1:]:
        return f"…/{value.rsplit('/', 1)[-1]}"
    return value


def _names(raw: str) -> list[str]:
    return [name.strip() for name in raw.split(",") if name.strip()]


def parse_processes(answer: str) -> list[Process]:
    processes: list[Process] = []
    for row in answer.splitlines():
        if not row.strip():
            continue
        fields = row.split("\t")
        fields += [""] * (4 - len(fields))
        pid = fields[3].strip()
        processes.append(
            Process(
                name=clip(fields[0]),
                frontmost=fields[1].strip().lower() == "true",
                visible=fields[2].strip().lower() == "true",
                pid=int(pid) if pid.isdigit() else 0,
            )
        )
    return processes


def parse_node(answer: str, *, element: Element) -> Element:
    """Fill *element* in from one ``_NODE`` answer. Never raises on a short row."""
    fields = answer.split("\t")
    fields += [""] * (13 - len(fields))
    element.role = clip(fields[0])
    element.subrole = clip(fields[1])
    element.title = clip(fields[2])
    element.description = clip(fields[3])
    element.value = SECURE_MARKER if element.role in SECURE_ROLES else clip(fields[4])
    element.enabled = clip(fields[5])
    element.focused = clip(fields[6])
    element.value_settable = clip(fields[7])
    element.child_count = int(fields[8]) if fields[8].strip().isdigit() else 0
    element.attribute_names = _names(fields[9])
    element.action_names = _names(fields[10])
    element.identifier = clip(fields[11])
    element.placeholder = clip(fields[12])
    return element


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


#: Enumerating every GUI process is one call that touches many of them, so it
#: gets its own, more generous budget than a single-node query.
PROCESS_LIST_TIMEOUT = 20.0


async def list_processes(runner: Osascript) -> list[Process]:
    return parse_processes(
        await runner.run(_PROCESSES, timeout=max(runner.timeout, PROCESS_LIST_TIMEOUT))
    )


async def window_count(runner: Osascript, process: str) -> int:
    answer = await runner.run(_WINDOW_COUNT, process)
    if answer == "no-process":
        raise LookupError(f"System Events sees no process named {process!r}")
    return int(answer) if answer.isdigit() else 0


async def describe_node(
    runner: Osascript, process: str, *, element: Element
) -> Element:
    """One node. A timeout or an error is recorded on the element, not raised."""
    try:
        answer = await runner.run(_NODE, process, element.id)
    except TimeoutError:
        element.query_error = QUERY_TIMEOUT_MARKER
        return element
    except OSError as exc:
        element.query_error = clip(str(exc))
        return element
    return parse_node(answer, element=element)


async def describe_focused(runner: Osascript, process: str) -> Element | None:
    try:
        answer = await runner.run(_FOCUSED, process)
    except TimeoutError:
        return Element(id="focused", parent_id=None, depth=0,
                       query_error=QUERY_TIMEOUT_MARKER)
    except OSError as exc:
        return Element(id="focused", parent_id=None, depth=0, query_error=clip(str(exc)))
    if answer.strip() == "none":
        return None
    return parse_node(answer, element=Element(id="focused", parent_id=None, depth=0))


@dataclass(slots=True)
class Node:
    """One node queued for inspection: where it is, and what it is called.

    ``handle`` is whatever the backend needs to reach it again — an index path
    for AppleScript, a live AXUIElement for the native API.
    """

    element: Element
    handle: Any


class Backend(Protocol):
    """What a walk needs from a source of accessibility data."""

    #: How :meth:`roots` found what it returned, and what it landed on. All set
    #: by ``roots()``.
    root_source: str
    root_role: str
    root_description: str
    enclosing_role: str
    enclosing_description: str
    focused_present: bool

    async def roots(self) -> list[Node]:
        """The application's windows, as the starting points of the walk."""
        ...

    async def describe(self, node: Node) -> None:
        """Fill in ``node.element``, recording errors on it rather than raising."""
        ...

    async def children(self, node: Node) -> list[Node]:
        """Direct children only. Never a whole subtree."""
        ...


def _copy_root_metadata(walk: Walk, backend: Backend) -> None:
    """Carry what root discovery learned onto the walk, so the report can say it."""
    walk.root_source = backend.root_source
    walk.root_role = backend.root_role
    walk.root_description = backend.root_description
    walk.enclosing_role = backend.enclosing_role
    walk.enclosing_description = backend.enclosing_description
    walk.focused_present = backend.focused_present


async def walk_backend(
    backend: Backend,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    include_web_area: bool = False,
) -> Walk:
    """Breadth-first, one node per query, bounded on both axes.

    Python owns the traversal in both backends: each is only ever asked about a
    single node and its *direct* children. AppleScript's ``entire contents`` is
    never used — it has hung this browser before — and the native API is never
    asked to materialise a subtree either.
    """
    walk = Walk()
    try:
        pending: deque[Node] = deque(await backend.roots())
    except TimeoutError:
        walk.complete = False
        walk.stop_reason = "window-count-timeout"
        _copy_root_metadata(walk, backend)
        return walk
    _copy_root_metadata(walk, backend)

    while pending:
        if len(walk.elements) >= max_elements:
            walk.complete = False
            walk.stop_reason = f"max-elements ({max_elements})"
            return walk

        node = pending.popleft()
        await backend.describe(node)
        element = node.element
        walk.elements.append(element)

        if element.query_error:
            continue  # a node we could not read is a node we cannot descend
        if element.depth >= max_depth:
            walk.complete = False
            walk.stop_reason = f"max-depth ({max_depth})"
            continue
        if element.role == WEB_AREA_ROLE and not include_web_area:
            continue  # page content: recorded, not entered

        pending.extend(await backend.children(node))

    return walk


class OsascriptBackend:
    """Addresses nodes by index path through System Events."""

    def __init__(self, runner: Osascript, process: str) -> None:
        self.runner = runner
        self.process = process
        self.root_source = ROOT_FROM_WINDOWS
        self.root_role = ""
        self.root_description = ""
        self.enclosing_role = ""
        self.enclosing_description = ""
        self.focused_present = False

    async def roots(self) -> list[Node]:
        windows = await window_count(self.runner, self.process)
        return [
            Node(Element(id=str(index), parent_id=None, depth=0), handle=str(index))
            for index in range(1, windows + 1)
        ]

    async def describe(self, node: Node) -> None:
        await describe_node(self.runner, self.process, element=node.element)

    async def children(self, node: Node) -> list[Node]:
        element = node.element
        return [
            Node(
                Element(
                    id=f"{element.id}.{child}",
                    parent_id=element.id,
                    depth=element.depth + 1,
                ),
                handle=f"{element.id}.{child}",
            )
            for child in range(1, element.child_count + 1)
        ]


async def walk_tree(
    runner: Osascript,
    process: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    include_web_area: bool = False,
) -> Walk:
    """Walk a process addressed by name, through System Events."""
    return await walk_backend(
        OsascriptBackend(runner, process),
        max_depth=max_depth,
        max_elements=max_elements,
        include_web_area=include_web_area,
    )


# --------------------------------------------------------------------------- #
# The native Accessibility API, for addressing one exact process
# --------------------------------------------------------------------------- #
#
# Several processes can share a name — this machine runs two called
# com.apple.appkit.xpc.openAndSavePanelService — and `tell process "<name>"`
# picks whichever System Events feels like. A PID is unambiguous, so PID mode
# goes straight to AXUIElementCreateApplication and never resolves the name
# back into an AppleScript query.

AX_SUCCESS = 0

#: Attributes read for every element, in the order the report lists them.
AX_ROLE = "AXRole"
AX_SUBROLE = "AXSubrole"
AX_TITLE = "AXTitle"
AX_DESCRIPTION = "AXDescription"
AX_VALUE = "AXValue"
AX_ENABLED = "AXEnabled"
AX_FOCUSED = "AXFocused"
AX_CHILDREN = "AXChildren"
AX_WINDOWS = "AXWindows"
AX_FOCUSED_ELEMENT = "AXFocusedUIElement"
AX_PARENT = "AXParent"
AX_WINDOW = "AXWindow"
AX_TOP_LEVEL = "AXTopLevelUIElement"
AX_IDENTIFIER = "AXIdentifier"
AX_PLACEHOLDER = "AXPlaceholderValue"
AX_DEFAULT_BUTTON = "AXDefaultButton"
AX_CANCEL_BUTTON = "AXCancelButton"
AX_SECTIONS = "AXSections"
SHEET_ROLE = "AXSheet"
GOTO_SHEET_IDENTIFIER = "GoToWindow"

#: Safe, structural attributes worth reading on a sheet and on the rows of its
#: completion table. All metadata — none of them is content.
SEMANTIC_ATTRIBUTES = (
    "AXSelectedRows",
    "AXSelectedCells",
    "AXVisibleRows",
    "AXRows",
    "AXColumns",
    "AXSelected",
    "AXFocused",
    "AXIndex",
    "AXIdentifier",
    "AXRoleDescription",
)

#: Actions that would *do* something rather than describe it. Reported, never
#: performed — the whole point is to find out which one commits the path.
ACTIVATION_ACTIONS = frozenset(
    {"AXPress", "AXConfirm", "AXPick", "AXShowDefaultUI", "AXOpen"}
)

#: How far into the completion table to look. Small on purpose.
SEMANTICS_MAX_DEPTH = 4
SEMANTICS_MAX_ELEMENTS = 120

#: Walking up from a focused element should reach the panel in a handful of
#: steps. The cap is there for the case where it never terminates.
MAX_ANCESTRY_DEPTH = 20

#: Where the walk found something to start from.
#:
#: The ancestry routes exist because ``AXWindows`` is not always populated and
#: ``AXFocusedUIElement.AXWindow`` is not always readable either — both were
#: empty on a live Open panel whose parent chain nonetheless ran
#: AXOutline → AXScrollArea → AXSplitGroup → AXSplitGroup → AXSheet → AXWindow.
#: Climbing is the route that actually works, so it is the one used.
ROOT_FROM_WINDOWS = "AXWindows"
ROOT_ANCESTRY_PREFIX = "AXFocusedUIElement ancestry"
ROOT_NONE = "none"

#: Containers that mark the panel, best first. AXSheet is preferred because the
#: file panel *is* the sheet: the enclosing browser window is a page's worth of
#: unrelated chrome around it.
#:
#: What gets walked, though, is the element one step *below* the container in
#: the ancestry — not the container itself. This graph is asymmetric: the live
#: panel's AXSheet answers ``AXChildren`` with ``[AXApplication]`` even though
#: the split group below it answers ``AXParent`` with that same sheet. Starting
#: at the sheet therefore walks straight back out to the application and finds
#: nothing; starting one step down walks the panel.
ENCLOSING_ROLES = ("AXSheet", "AXWindow")


class AxApi(Protocol):
    """The slice of the Accessibility C API this tool uses.

    An interface rather than direct calls so the whole native path can be
    exercised without a Mac in the room — and so the PyObjC import stays in one
    adapter instead of spreading through the walk.

    Read-only by construction: there is no ``perform_action`` here.
    """

    def create_application(self, pid: int) -> Any: ...

    def set_timeout(self, element: Any, seconds: float) -> None: ...

    def attribute_names(self, element: Any) -> list[str]: ...

    def attribute_value(self, element: Any, name: str) -> Any: ...

    def action_names(self, element: Any) -> list[str]: ...

    def is_settable(self, element: Any, name: str) -> bool: ...

    def pid_of(self, element: Any) -> int | None: ...

    def same_element(self, first: Any, second: Any) -> bool:
        """Whether two handles refer to the same accessibility element.

        Python identity is the wrong test: two separately-obtained handles for
        the same element are different objects. Cycle detection has to ask the
        framework, not the interpreter.
        """
        ...


#: The production adapter, reused rather than reimplemented: one set of PyObjC
#: wrappers for the whole project. A debug script may depend on production;
#: production must never depend on a debug script.
#:
#: It is a superset of the AxApi protocol above (it can also write attributes
#: and perform actions) — this inspector simply never calls those.
PyObjCAxApi = macos_ax.PyObjCAxApi


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - running, owned by someone else
        return True
    return True


def resolve_process_name(pid: int) -> str:
    """A display name for the report. Never used to address anything."""
    try:
        from AppKit import NSRunningApplication  # noqa: PLC0415
    except ImportError:  # pragma: no cover - AppKit missing
        return ""
    application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if application is None:
        return ""
    name = application.localizedName() or application.bundleIdentifier() or ""
    return clip(str(name))


def _ax_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return clip(str(value))


class NativeAxBackend:
    """Walks one exact process through ``AXUIElementCreateApplication(pid)``."""

    def __init__(self, api: AxApi, pid: int, *, timeout: float = DEFAULT_QUERY_TIMEOUT):
        self.api = api
        self.pid = pid
        self.app = api.create_application(pid)
        self.root_source = ROOT_NONE
        self.root_role = ""
        self.root_description = ""
        #: The container the root was found under — kept as context, not walked.
        self.enclosing_role = ""
        self.enclosing_description = ""
        #: Whether anything was focused at all. Distinguishes "this process has
        #: nothing on screen" from "it has, but no container could be derived".
        self.focused_present = False
        with suppress(Exception):  # pragma: no cover - old bindings
            api.set_timeout(self.app, timeout)

    async def roots(self) -> list[Node]:
        """The elements to walk from — by whatever route actually finds them.

        An application root does not always publish its panel through
        ``AXWindows``, and its focused element does not always answer
        ``AXWindow`` either: both came back empty on a live Open panel whose
        parent chain nonetheless ran AXOutline → AXScrollArea → AXSplitGroup →
        AXSplitGroup → AXSheet → AXWindow → AXApplication.

        So the fallback climbs, using the same walk ``--ancestry`` uses — the
        one route already proven against this process — and takes the enclosing
        AXSheet, or the AXWindow if there is no sheet. Which child of the panel
        happens to hold focus does not matter; the point of climbing is to
        recover the container regardless.
        """
        windows = self.api.attribute_value(self.app, AX_WINDOWS) or []
        if windows:
            self.root_source = ROOT_FROM_WINDOWS
            return [
                Node(Element(id=str(index), parent_id=None, depth=0), handle=window)
                for index, window in enumerate(windows, start=1)
            ]

        chain, _cycle = await self.ancestor_chain()
        self.focused_present = bool(chain)
        if not chain:
            self.root_source = ROOT_NONE
            return []

        for role in ENCLOSING_ROLES:
            # AXApplication is deliberately not a candidate: it is the whole
            # process, not the panel.
            index = next(
                (i for i, node in enumerate(chain) if node.element.role == role), None
            )
            if index is None:
                continue

            enclosing = chain[index]
            # One step below the container is the content. Only when the
            # focused element *is* the container is there nothing lower to use.
            content = chain[index - 1] if index > 0 else enclosing
            self.root_source = (
                f"{ROOT_ANCESTRY_PREFIX} child-of-{role}"
                if index > 0
                else f"{ROOT_ANCESTRY_PREFIX} {role}"
            )
            self.root_role = content.element.role
            self.root_description = content.element.description
            self.enclosing_role = enclosing.element.role
            self.enclosing_description = enclosing.element.description
            return [
                Node(Element(id="1", parent_id=None, depth=0), handle=content.handle)
            ]

        self.root_source = ROOT_NONE
        return []

    async def ancestor_chain(
        self, *, max_depth: int = MAX_ANCESTRY_DEPTH
    ) -> tuple[list[Node], bool]:
        """The focused element and every parent above it, plus "was it a cycle?".

        The one parent-walking implementation: ``--ancestry`` renders it, and
        root discovery searches it. Bounded twice over — by depth, and by a
        same-element check, since identity is not usable here: every read of
        ``AXParent`` hands back a fresh Python object for the same element.
        """
        handle = self.api.attribute_value(self.app, AX_FOCUSED_ELEMENT)
        if handle is None:
            return [], False

        seen: list[Any] = []
        chain: list[Node] = []
        depth = 0
        while handle is not None and depth < max_depth:
            if any(self.api.same_element(handle, other) for other in seen):
                return chain, True
            seen.append(handle)

            element = Element(
                id="focused" if depth == 0 else f"ancestor-{depth}",
                parent_id=None,
                depth=depth,
            )
            try:
                self._fill(element, handle)
            except Exception as exc:  # pragma: no cover - element went away
                element.query_error = clip(f"{type(exc).__name__}: {exc}")
            chain.append(Node(element, handle=handle))

            handle = self.api.attribute_value(handle, AX_PARENT)
            depth += 1
        return chain, False

    async def focused_sheet(self, *, grandchild_limit: int = 6) -> SheetReport:
        """The sheet the focused element sits in, and what is directly in it.

        Written because "AXDefaultButton" appearing in a sheet's attribute
        *names* turned out not to mean the attribute has a value: reading it on
        the live Go to Folder sheet returned nothing. So the two are reported
        separately — advertised, and actually there — and the sheet's direct
        children are listed so a real commit control can be identified rather
        than guessed at.

        Inspection only. Nothing is pressed, confirmed, typed or written.
        """
        chain, _cycle = await self.ancestor_chain()
        sheet_node = next(
            (node for node in chain if node.element.role == SHEET_ROLE), None
        )
        if sheet_node is None:
            return SheetReport(sheet=None, buttons=[], children=[], grandchildren={})

        sheet = sheet_node.element
        sheet.id = "sheet"
        buttons = [
            self._describe_button(sheet_node.handle, sheet, name)
            for name in (AX_DEFAULT_BUTTON, AX_CANCEL_BUTTON)
        ]

        children: list[Element] = []
        grandchildren: dict[str, list[Element]] = {}
        for index, handle in enumerate(self._children_of(sheet_node.handle), start=1):
            child = Element(id=f"sheet.{index}", parent_id="sheet", depth=1)
            self._fill_safely(child, handle)
            children.append(child)

            # One level further, only when there is little of it: enough to see
            # what a group holds, not a traversal of the whole panel.
            kids = self._children_of(handle)
            if 0 < len(kids) <= grandchild_limit:
                described = []
                for position, kid in enumerate(kids, start=1):
                    element = Element(
                        id=f"{child.id}.{position}", parent_id=child.id, depth=2
                    )
                    self._fill_safely(element, kid)
                    described.append(element)
                grandchildren[child.id] = described

        return SheetReport(
            sheet=sheet, buttons=buttons, children=children, grandchildren=grandchildren
        )

    async def focused_sheet_semantics(
        self,
        *,
        max_depth: int = SEMANTICS_MAX_DEPTH,
        max_elements: int = SEMANTICS_MAX_ELEMENTS,
    ) -> SemanticsReport:
        """What the sheet exposes besides its children.

        Both button attributes came back nil and the field's own AXConfirm does
        nothing, so the remaining surfaces are worth reading before another
        commit mechanism is chosen: the sheet's AXSections, and the completion
        table — including which of its rows or cells offer an action that would
        actually activate something.

        Inspection only. Nothing is pressed, confirmed, set or dismissed.
        """
        chain, _cycle = await self.ancestor_chain()
        sheet_node = next(
            (node for node in chain if node.element.role == SHEET_ROLE), None
        )
        if sheet_node is None:
            return SemanticsReport(sheet=None)

        sheet = sheet_node.element
        sheet.id = "sheet"
        report = SemanticsReport(
            sheet=sheet,
            is_goto_window=sheet.identifier == GOTO_SHEET_IDENTIFIER,
            sections=self._attribute_report(sheet_node.handle, AX_SECTIONS, sheet),
            attributes=[
                self._attribute_report(sheet_node.handle, name, sheet)
                for name in SEMANTIC_ATTRIBUTES
            ],
        )

        # Bounded walk of the completion area. Breadth-first from the sheet's
        # own children so the scroll area, its table, rows and cells are all
        # reached without wandering into the rest of the panel.
        pending: list[tuple[Any, int, str]] = [
            (handle, 1, f"sheet.{index}")
            for index, handle in enumerate(self._children_of(sheet_node.handle), start=1)
        ]
        seen: list[Any] = []
        while pending and len(report.subtree) < max_elements:
            handle, depth, node_id = pending.pop(0)
            if any(self.api.same_element(handle, other) for other in seen):
                continue
            seen.append(handle)

            element = Element(id=node_id, parent_id=None, depth=depth)
            self._fill_safely(element, handle)
            element.value = redact_path(element.value)
            report.subtree.append(
                SubtreeNode(
                    element=element,
                    attributes=[
                        self._attribute_report(handle, name, element)
                        for name in SEMANTIC_ATTRIBUTES
                        if name in element.attribute_names
                    ],
                )
            )

            if depth < max_depth:
                pending.extend(
                    (child, depth + 1, f"{node_id}.{position}")
                    for position, child in enumerate(self._children_of(handle), start=1)
                )
        return report

    def _attribute_report(
        self, handle: Any, name: str, owner: Element
    ) -> AttributeReport:
        """Read one attribute and say what came back, in its own terms."""
        advertised = name in owner.attribute_names
        try:
            value = self.api.attribute_value(handle, name)
        except Exception:  # pragma: no cover - element vanished
            value = None

        if value is None:
            return AttributeReport(name=name, advertised=advertised, present=False)

        if isinstance(value, list):
            described = []
            for index, item in enumerate(value[:10], start=1):
                element = Element(
                    id=f"{owner.id}.{name}.{index}", parent_id=owner.id, depth=1
                )
                self._fill_safely(element, item)
                element.value = redact_path(element.value)
                described.append(element)
            return AttributeReport(
                name=name,
                advertised=advertised,
                present=True,
                summary=f"{len(value)} element(s)",
                elements=described,
            )

        if isinstance(value, str | bool | int | float):
            return AttributeReport(
                name=name,
                advertised=advertised,
                present=True,
                summary=clip(redact_path(str(value))),
            )

        # An accessibility element on its own.
        element = Element(id=f"{owner.id}.{name}", parent_id=owner.id, depth=1)
        self._fill_safely(element, value)
        element.value = redact_path(element.value)
        return AttributeReport(
            name=name,
            advertised=advertised,
            present=True,
            summary=f"{element.role or '?'} element",
            elements=[element],
        )

    def _children_of(self, handle: Any) -> list[Any]:
        try:
            return list(self.api.attribute_value(handle, "AXChildren") or [])
        except Exception:  # pragma: no cover - element vanished
            return []

    def _fill_safely(self, element: Element, handle: Any) -> None:
        try:
            self._fill(element, handle)
        except Exception as exc:  # pragma: no cover - element vanished
            element.query_error = clip(f"{type(exc).__name__}: {exc}")

    def _describe_button(
        self, handle: Any, sheet: Element, attribute: str
    ) -> ButtonReport:
        """Advertised, present, or neither — three different answers.

        The distinction is the point: the production selector read
        AXDefaultButton *because* the sheet advertised it, and got nothing.
        """
        advertised = attribute in sheet.attribute_names
        try:
            value = self.api.attribute_value(handle, attribute)
        except Exception:  # pragma: no cover - element vanished
            value = None
        if value is None:
            return ButtonReport(attribute=attribute, advertised=advertised, element=None)

        element = Element(id=f"sheet.{attribute}", parent_id="sheet", depth=1)
        self._fill_safely(element, value)
        return ButtonReport(attribute=attribute, advertised=advertised, element=element)

    async def ancestry(self, *, max_depth: int = MAX_ANCESTRY_DEPTH) -> list[Element]:
        """The focused element and everything above it, for display."""
        chain, cycle = await self.ancestor_chain(max_depth=max_depth)
        ancestors = [node.element for node in chain]
        if cycle:
            ancestors.append(
                Element(
                    id=f"ancestor-{len(chain)}",
                    parent_id=None,
                    depth=len(chain),
                    query_error="<cycle: already seen>",
                )
            )
        return ancestors

    async def describe(self, node: Node) -> None:
        element = node.element
        try:
            self._fill(element, node.handle)
        except Exception as exc:  # pragma: no cover - a node that went away
            element.query_error = clip(f"{type(exc).__name__}: {exc}")

    def _fill(self, element: Element, handle: Any) -> None:
        api = self.api
        element.attribute_names = api.attribute_names(handle)
        element.action_names = api.action_names(handle)
        element.role = _ax_text(api.attribute_value(handle, AX_ROLE))
        element.subrole = _ax_text(api.attribute_value(handle, AX_SUBROLE))
        element.title = _ax_text(api.attribute_value(handle, AX_TITLE))
        element.description = _ax_text(api.attribute_value(handle, AX_DESCRIPTION))
        element.enabled = _ax_text(api.attribute_value(handle, AX_ENABLED))
        element.focused = _ax_text(api.attribute_value(handle, AX_FOCUSED))
        # Identity, not contents: safe to read even on a secure field.
        element.identifier = _ax_text(api.attribute_value(handle, AX_IDENTIFIER))
        element.placeholder = _ax_text(api.attribute_value(handle, AX_PLACEHOLDER))
        element.value_settable = "true" if api.is_settable(handle, AX_VALUE) else "false"
        # A secure field's value is never read — not even to be discarded.
        element.value = (
            SECURE_MARKER
            if element.role in SECURE_ROLES
            else _ax_text(api.attribute_value(handle, AX_VALUE))
        )
        element.child_count = len(api.attribute_value(handle, AX_CHILDREN) or [])

    async def children(self, node: Node) -> list[Node]:
        kids = self.api.attribute_value(node.handle, AX_CHILDREN) or []
        element = node.element
        return [
            Node(
                Element(
                    id=f"{element.id}.{index}",
                    parent_id=element.id,
                    depth=element.depth + 1,
                ),
                handle=child,
            )
            for index, child in enumerate(kids, start=1)
        ]

    async def focused(self) -> Element | None:
        handle = self.api.attribute_value(self.app, AX_FOCUSED_ELEMENT)
        if handle is None:
            return None
        element = Element(id="focused", parent_id=None, depth=0)
        self._fill(element, handle)
        return element


async def walk_pid(
    api: AxApi,
    pid: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    include_web_area: bool = False,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> Walk:
    """Walk one exact process. No name is involved at any point."""
    return await walk_backend(
        NativeAxBackend(api, pid, timeout=timeout),
        max_depth=max_depth,
        max_elements=max_elements,
        include_web_area=include_web_area,
    )


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def matches(
    element: Element,
    *,
    role: str | None = None,
    contains: str | None = None,
    editable_only: bool = False,
) -> bool:
    """Whether an element passes the display filters.

    Filters only ever decide what is *shown*: the traversal above is identical
    with or without them, so one run's JSON is comparable with another's.
    """
    if role is not None and element.role != role:
        return False
    if editable_only and not element.looks_editable:
        return False
    if contains is not None:
        haystack = " ".join(
            [
                element.role,
                element.subrole,
                element.title,
                element.description,
                element.value,
                element.id,
            ]
        ).lower()
        if contains.lower() not in haystack:
            return False
    return True


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def build_report(
    process: str, walk: Walk, windows: list[Element], *, pid: int | None = None
) -> dict[str, Any]:
    return {
        "pid": pid,
        "process": process,
        "timestamp": datetime.now(UTC).isoformat(),
        "complete": walk.complete,
        "stop_reason": walk.stop_reason,
        "root_source": walk.root_source,
        "root_role": walk.root_role,
        "root_description": walk.root_description,
        "enclosing_role": walk.enclosing_role,
        "enclosing_description": walk.enclosing_description,
        "focused_present": walk.focused_present,
        "windows": [asdict(window) for window in windows],
        "elements": [asdict(element) for element in walk.elements],
    }


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def print_processes(processes: list[Process]) -> None:
    print(f"\n{len(processes)} GUI process(es) visible to System Events:\n")
    for process in sorted(processes, key=lambda p: p.name.lower()):
        flags = []
        if process.frontmost:
            flags.append("frontmost")
        if process.visible:
            flags.append("visible")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        pid = str(process.pid) if process.pid else "?"
        print(f"  {pid:>7}  {process.name}{suffix}")
    print()


def print_elements(elements: list[Element], *, detailed: bool) -> None:
    for element in elements:
        print(element.detail() if detailed else element.one_line())


def print_sheet_report(report: SheetReport, target: str) -> None:
    print(f"\nSheet containing AXFocusedUIElement ({target}):\n")
    if report.sheet is None:
        print("  No AXSheet above the focused element — or nothing is focused.\n")
        return

    print(report.sheet.detail())
    print()
    for button in report.buttons:
        print(f"  {button.describe()}")
    print(f"\n{len(report.children)} direct child(ren) of the sheet:\n")

    for child in report.children:
        print(child.detail())
        for grandchild in report.grandchildren.get(child.id, []):
            print(grandchild.detail())
        print()


def print_semantics_report(report: SemanticsReport, target: str) -> None:
    print(f"\nSheet semantics ({target}):\n")
    if report.sheet is None:
        print("  No AXSheet above the focused element — or nothing is focused.\n")
        return

    print(report.sheet.detail())
    identity = "GoToWindow" if report.is_goto_window else report.sheet.identifier or "?"
    print(f"\n  identified as: {identity}")

    if report.sections is not None:
        print(f"\n  {report.sections.describe()}")
        for element in report.sections.elements:
            print(element.detail())

    print("\n  other semantic attributes:")
    for attribute in report.attributes:
        print(f"    {attribute.describe()}")
        for element in attribute.elements:
            print(element.detail())

    print(f"\n{len(report.subtree)} element(s) in the sheet subtree:\n")
    for node in report.subtree:
        print(node.element.detail())
        for attribute in node.attributes:
            print(f"{'  ' * node.element.depth}    {attribute.describe()}")

    candidates = report.activation_candidates
    print(f"\n{len(candidates)} element(s) expose an activation action:\n")
    for node in candidates:
        print(
            f"  [{node.element.id}] {node.element.role}"
            f"{' id=' + repr(node.element.identifier) if node.element.identifier else ''}"
            f" -> {', '.join(node.activation_actions)}"
        )
    if not candidates:
        print("  (none — nothing in this sheet offers a way to activate it)")
    print("\nNothing was pressed, confirmed or written.\n")


def role_histogram(elements: list[Element]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for element in elements:
        role = element.role or "(unreadable)"
        roles[role] = roles.get(role, 0) + 1
    return dict(sorted(roles.items(), key=lambda item: (-item[1], item[0])))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac_ax_inspector",
        description=(
            "Inspect the macOS Accessibility hierarchy of a running GUI process. "
            "Local debug tool: it lists attributes and actions, and never performs "
            "any of them."
        ),
        epilog=(
            "Workflow: open the browser by hand, trigger a file Open dialog, press "
            "Cmd+Shift+G so Go to Folder is visible, leave it open, then run this "
            "from another terminal."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--process", help="process name, e.g. 'Google Chrome for Testing'")
    target.add_argument(
        "--pid",
        type=int,
        help=(
            "exact process id. Use this when several processes share a name — "
            "it goes through the native Accessibility API and never resolves "
            "the name back into an ambiguous query."
        ),
    )
    parser.add_argument("--windows", action="store_true", help="list the process's windows")
    parser.add_argument("--tree", action="store_true", help="walk the accessibility tree")
    parser.add_argument("--focused", action="store_true", help="report AXFocusedUIElement")
    parser.add_argument(
        "--focused-sheet-children",
        action="store_true",
        help=(
            "describe the sheet the focused element is in, its default/cancel "
            "button attributes, and its direct children (PID mode)"
        ),
    )
    parser.add_argument(
        "--focused-sheet-semantics",
        action="store_true",
        help=(
            "read the sheet's AXSections and its completion table, and report "
            "which rows or cells expose an activation action (PID mode)"
        ),
    )
    parser.add_argument(
        "--ancestry",
        action="store_true",
        help=(
            "walk AXParent up from the focused element, showing where it sits "
            "when AXWindows will not say (PID mode)"
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=(
            "wait this many seconds before querying anything, so you can put "
            "focus where you want it. Running this from a terminal makes the "
            "terminal frontmost, which is enough to move the panel's focused "
            "element off the control you care about."
        ),
    )
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-elements", type=int, default=DEFAULT_MAX_ELEMENTS)
    parser.add_argument("--query-timeout", type=float, default=DEFAULT_QUERY_TIMEOUT,
                        help="seconds per osascript query (default: 3)")
    parser.add_argument("--include-web-area", action="store_true",
                        help="also descend into AXWebArea (the browser page)")
    parser.add_argument("--role", help="only show elements with this AXRole")
    parser.add_argument("--contains", help="only show elements whose text contains this")
    parser.add_argument("--editable-only", action="store_true",
                        help="only show elements that look writable")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="write the full report here, e.g. data/debug/mac-ax.json")
    parser.add_argument("--detail", action="store_true",
                        help="print attributes and actions for every element")
    return parser


async def run(args: argparse.Namespace) -> int:
    runner = Osascript(timeout=args.query_timeout)

    if not args.process and args.pid is None:
        print_processes(await list_processes(runner))
        print(
            'Re-run with --process "<name>" --tree, or --pid <id> --tree when '
            "several processes share a name.\n"
        )
        return 0

    api: AxApi | None = None
    pid: int = args.pid if args.pid is not None else 0
    if args.pid is not None:
        if not process_exists(pid):
            raise LookupError(f"No running macOS process with PID {pid}")
        api = PyObjCAxApi()

    target = f"PID {pid}" if api is not None else repr(args.process)

    if args.delay > 0:
        # Before anything is asked, not after: the point is to inspect the
        # state you leave behind, and a query taken now would describe this
        # terminal being frontmost instead.
        seconds = int(args.delay) if args.delay.is_integer() else args.delay
        print(f"\nWaiting {seconds} seconds.")
        print(
            "Switch to the Open dialog, press Cmd+Shift+G, click inside the "
            "Go to Folder\npath field, and leave focus there."
        )
        sys.stdout.flush()
        await asyncio.sleep(args.delay)
        print("Querying now.")

    if args.focused:
        if api is not None:
            focused = await NativeAxBackend(
                api, pid, timeout=args.query_timeout
            ).focused()
        else:
            focused = await describe_focused(runner, args.process)
        print(f"\nAXFocusedUIElement ({target}):")
        print(focused.detail() if focused is not None else "  none / missing value")
        print()

    if args.focused_sheet_children:
        if api is None:
            print(
                "\n--focused-sheet-children needs --pid: it reads the native "
                "API.\n",
                file=sys.stderr,
            )
            return 2
        report = await NativeAxBackend(
            api, pid, timeout=args.query_timeout
        ).focused_sheet()
        print_sheet_report(report, target)

    if args.focused_sheet_semantics:
        if api is None:
            print(
                "\n--focused-sheet-semantics needs --pid: it reads the native "
                "API.\n",
                file=sys.stderr,
            )
            return 2
        semantics = await NativeAxBackend(
            api, pid, timeout=args.query_timeout
        ).focused_sheet_semantics()
        print_semantics_report(semantics, target)

    if args.ancestry:
        if api is None:
            print(
                "\n--ancestry needs --pid: it walks AXParent through the native "
                "API.\n",
                file=sys.stderr,
            )
            return 2
        ancestors = await NativeAxBackend(api, pid, timeout=args.query_timeout).ancestry()
        print(f"\nAncestry of AXFocusedUIElement ({target}):\n")
        if not ancestors:
            print("  none / missing value — nothing is focused in this process.")
        for element in ancestors:
            print(element.detail())
            print()

    # A filter is a request to look at the tree, so it implies --tree.
    wants_tree = args.tree or args.role or args.contains or args.editable_only
    if not (wants_tree or args.windows):
        if not (
            args.focused
            or args.ancestry
            or args.focused_sheet_children
            or args.focused_sheet_semantics
        ):
            print("Nothing to do: pass --windows, --tree, --focused or a filter.")
        return 0

    if api is not None:
        walk = await walk_pid(
            api,
            pid,
            max_depth=args.max_depth,
            max_elements=args.max_elements,
            include_web_area=args.include_web_area,
            timeout=args.query_timeout,
        )
    else:
        walk = await walk_tree(
            runner,
            args.process,
            max_depth=args.max_depth,
            max_elements=args.max_elements,
            include_web_area=args.include_web_area,
        )
    windows = [element for element in walk.elements if element.depth == 0]

    if walk.root_source.startswith(ROOT_ANCESTRY_PREFIX):
        # A finding, not a footnote: the panel exists, it is just not published
        # where the obvious question looks.
        if walk.enclosing_role and walk.enclosing_role != walk.root_role:
            described = (
                f" desc={walk.enclosing_description!r}"
                if walk.enclosing_description
                else ""
            )
            print(
                f"\nAXWindows is empty; using {walk.root_role} from focused "
                f"ancestry directly under {walk.enclosing_role}{described} as root."
            )
        else:
            print(
                f"\nAXWindows is empty; using {walk.root_role} from focused "
                "ancestry as root."
            )

    if args.windows or not windows:
        print(f"\n{len(windows)} root(s) to walk for {target}:\n")
        for index, window in enumerate(windows, start=1):
            print(f"  index {index}: {window.one_line().strip()}")
        if not windows:
            # Two different failures, said differently: claiming nothing is
            # focused when something plainly is sends you looking in the wrong
            # place.
            if walk.focused_present:
                print(
                    f"  {target} has an AXFocusedUIElement, but no "
                    "AXSheet/AXWindow ancestor could be derived from it.\n"
                    "  Run --ancestry to see the chain that was walked."
                )
            else:
                print(
                    f"  {target} exposes no accessibility windows, and nothing "
                    "is focused in it either.\n"
                    "  If the dialog is on screen, it belongs to a different "
                    "process — run without --pid/--process to list them all."
                )
        print()

    if wants_tree:
        shown = [
            element
            for element in walk.elements
            if matches(
                element,
                role=args.role,
                contains=args.contains,
                editable_only=args.editable_only,
            )
        ]
        print(f"\n{len(shown)} of {len(walk.elements)} element(s) shown:\n")
        print_elements(shown, detailed=args.detail or bool(args.role or args.contains))
        print("\nroles found:")
        for role, count in role_histogram(walk.elements).items():
            print(f"  {role}: {count}")

    print(f"\ncomplete: {walk.complete}  stop_reason: {walk.stop_reason}")

    if args.json_path is not None:
        # Written even when the walk stopped early: a partial tree is still
        # the evidence, and losing it to a timeout would be the worst outcome.
        name = args.process or (resolve_process_name(pid) if api is not None else "")
        write_json(
            args.json_path,
            build_report(name, walk, windows, pid=pid if api is not None else None),
        )
        print(f"written: {args.json_path}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print("This inspector only works on macOS.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args))
    except LookupError as exc:
        print(f"\n{exc}\nRun without --process to list what System Events sees.\n",
              file=sys.stderr)
        return 1
    except AccessibilityUnavailable as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except TimeoutError:
        # Caught before OSError on purpose: TimeoutError is a subclass of it,
        # and reporting a timeout as "osascript failed: " (with an empty
        # message, since there is no stderr) explains nothing.
        print(
            f"\nTimed out after {args.query_timeout}s waiting for System Events.\n"
            "Raise --query-timeout, or narrow the walk with --max-elements.\n",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        detail = str(exc) or "no error output"
        print(
            f"\nosascript failed: {detail}\n"
            "If this mentions assistive access, grant Accessibility to this "
            "terminal:\n  System Settings → Privacy & Security → Accessibility\n",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(main())
