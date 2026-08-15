"""Driving the operating system's own file-open dialog.

Why this exists at all: ID.GOV.UA accepts a key only when it arrives through a
real OS file selection. Established by A/B test — ``set_input_files()`` on the
input and Playwright's intercepted ``FileChooser`` both leave the site able to
*read* the key and then reset the form, while the same file picked by hand in
the macOS Open panel authenticates.

So this module automates exactly one step: the Open panel. It is the only place
in the project that knows about an operating system, and it is reached through
:class:`NativeFileSelector`, so ``LoginPage`` never contains a line of it.

Every handle used here was measured, not guessed. On the live panel:

* the panel is hosted by ``com.apple.appkit.xpc.openAndSavePanelService`` — a
  separate XPC process, one instance per browser, so a name identifies the kind
  of process and only a PID identifies which one;
* the Open panel is ``AXSheet`` with ``AXIdentifier`` ``open-panel``;
* Go to Folder is ``AXSheet`` with ``AXIdentifier`` ``GoToWindow``;
* its path field is ``AXTextField`` with ``AXIdentifier`` ``PathTextField``,
  whose ``AXValue`` is settable;
* Go to Folder has no usable commit control at all: its ``AXDefaultButton``,
  ``AXCancelButton`` and ``AXSections`` are advertised and nil, its only button
  is ``CloseButton``, and ``AXConfirm`` on the path field reports success while
  changing nothing;
* Go to Folder is *navigation only* — given a full file path it lands in the
  parent directory and selects nothing;
* a filename in the list is an ``AXTextField`` inside an ``AXCell`` inside an
  ``AXRow`` in ``AXOutline`` ``ListView``. Only the row's ``AXSelected`` is
  settable; the filename's and the cell's are read-only;
* the panel commits through its ``AXDefaultButton`` when that is populated, and
  otherwise through ``AXButton`` ``OKButton``.

So the directory is written to ``AXValue`` and committed with a Return key —
the only mechanism that sheet responds to — and the file itself is then chosen
by setting its row's ``AXSelected`` and reading the list's ``AXSelectedRows``
back. Selection is written and verified rather than activated: the filename
offers ``AXOpen``, but an action cannot be checked, and this can. The Return is posted to the panel
process by PID rather than to the front of the system, because while this runs
the frontmost application is usually the terminal that started it. It is sent
once; a keystroke that did nothing is not worth repeating.

Nothing is typed, pasted or clipboarded: keystrokes were lost to a sheet that
was still appearing, and the clipboard is the user's. The only keyboard events
are ⌘⇧G and that Return, neither of which carries text.

Every wait is a condition poll — no fixed sleeps anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..logging_config import sanitize
from ..models import (
    AccessibilityUnavailable,
    ConfigError,
    NativeFileDialogError,
)
from .macos_ax import (
    AX_DEFAULT_BUTTON,
    AX_FOCUSED,
    AX_PRESS,
    AX_SELECTED,
    AX_SELECTED_ROWS,
    AX_VALUE,
    KEY_CODE_G,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_ELEMENTS,
    PANEL_SERVICE_BUNDLE_ID,
    AxApi,
    ancestors_of,
    find_by_identifier,
    find_where,
    identifier_of,
    is_enabled,
    load_ax_api,
    role_of,
    search_roots,
    value_of,
)

logger = logging.getLogger(__name__)


def _is_true(value: Any) -> bool:
    """AX booleans arrive as bools from PyObjC and as text from elsewhere."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"

#: Matches the polling cadence used by the page objects.
_POLL_INTERVAL_MS = 250

#: Accessibility identifiers, confirmed against the live panel.
OPEN_PANEL_ID = "open-panel"
GOTO_SHEET_ID = "GoToWindow"
PATH_FIELD_ID = "PathTextField"
PATH_FIELD_ROLE = "AXTextField"
BUTTON_ROLE = "AXButton"

#: The file list, and how one file in it is identified. Measured live: a
#: filename is an AXTextField inside an AXCell inside an AXRow, and only the
#: row's AXSelected is settable — the filename's and the cell's are not.
LIST_VIEW_ID = "ListView"
FILE_NAME_ROLE = "AXTextField"
ROW_ROLE = "AXRow"
OUTLINE_ROLE = "AXOutline"

#: The panel's commit button, by identifier. Its label is localised — this is
#: not — and its sibling must never be pressed by accident.
OK_BUTTON_ID = "OKButton"
CANCEL_BUTTON_ID = "CancelButton"

#: Kept for configuration compatibility only. The native panel is found by
#: walking the accessibility trees of the panel-service processes, never by
#: this name — the panel does not live in the browser process at all.
DEFAULT_MACOS_PROCESS = "Chromium"


@runtime_checkable
class NativeFileSelector(Protocol):
    """Answers an operating-system file dialog that the page has opened."""

    async def select_file(self, path: Path) -> None:
        """Wait for the dialog, choose *path* in it, and wait for it to close.

        Raises :class:`NativeFileDialogError` if the dialog never appears, the
        selection cannot be made, or the dialog is still up afterwards. It
        never returns having "probably" worked.
        """
        ...


@runtime_checkable
class AccessibilityInspector(Protocol):
    """The extra, diagnostic-only surface used by ``ensure-auth-debug-native-ax``.

    Separate from :class:`NativeFileSelector` because production never needs
    it: selecting a file and inspecting a hierarchy are different jobs, and
    only one of them belongs in the authentication path.
    """

    async def wait_for_dialog(self) -> None: ...

    async def send_goto_shortcut(self) -> None: ...

    async def describe_hierarchy(
        self, *, max_depth: int = ..., max_elements: int = ...
    ) -> list[dict[str, Any]]: ...


def summarize_hierarchy(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for the terminal: how many elements, and of what roles."""
    roles: dict[str, int] = {}
    for element in elements:
        role = str(element.get("role") or "(none)")
        roles[role] = roles.get(role, 0) + 1
    return {
        "windows": sum(1 for e in elements if e.get("depth") == 0),
        "elements": len(elements),
        "roles": dict(sorted(roles.items(), key=lambda item: (-item[1], item[0]))),
        "max_depth_seen": max((int(e.get("depth", 0)) for e in elements), default=0),
    }


@dataclass(frozen=True, slots=True)
class OpenPanel:
    """One live Open panel: which process owns it, and its sheet."""

    pid: int
    app: Any
    sheet: Any
    name: str = ""


class MacOSFileSelector:
    """Drives the macOS Open panel through the Accessibility API."""

    def __init__(
        self,
        *,
        api: AxApi | None = None,
        appear_timeout_ms: int = 15_000,
        close_timeout_ms: int = 15_000,
        process_name: str = DEFAULT_MACOS_PROCESS,
    ) -> None:
        #: Kept so existing configuration keeps loading; not used to find the
        #: panel. See DEFAULT_MACOS_PROCESS.
        self.process_name = process_name
        self.appear_timeout_ms = appear_timeout_ms
        self.close_timeout_ms = close_timeout_ms
        self._api = api

    @property
    def api(self) -> AxApi:
        """The Accessibility API, loaded on first use.

        Deferred so that constructing a selector — which happens for every
        command, authenticating or not — cannot fail on a machine that will
        never use it.
        """
        if self._api is None:
            self._api = load_ax_api()
        return self._api

    # ------------------------------------------------------------- public ---

    async def select_file(self, path: Path) -> None:
        """Choose *path* in the Open panel that is currently on screen.

        Succeeds only when the panel actually closes. What the web page then
        makes of the file is the caller's business — nothing here looks at a
        browser.
        """
        target = path.expanduser().resolve()

        # The directory, not the file: Go to Folder is navigation. Given a full
        # path it lands in the parent folder and selects nothing, so the file
        # still has to be picked out of the list afterwards.
        await self.navigate_to(target.parent)
        logger.info("Navigated to parent directory")

        row, outline = self._find_file_row(target.name)
        logger.info("File found in Open panel: %s", target.name)

        self._select_row(row, outline, target.name)
        await self._open_selected_file()

    async def navigate_to(self, path: Path) -> OpenPanel:
        """Put *path* into Go to Folder and commit it, then stop.

        Everything up to and including the Go to Folder sheet closing —
        nothing after it. Split out so a diagnostic can navigate to a
        directory and then look at what the panel is showing, without any of
        this deciding to select something.

        Returns the panel it worked on, though callers that go on to act must
        re-resolve it: AppKit may rebuild the panel as the sheet closes.
        """
        target = path.expanduser().resolve()

        panel = await self._await_panel()
        await self._open_goto_sheet(panel)
        _goto, field = await self._await_path_field(panel)

        self._require_writable(field)
        self._write_path(field, target)
        logger.info("Go to Folder path accepted: %s", target.name)

        # Committing the path is a Return key, because this sheet exposes no
        # way to do it through Accessibility: measured live, its
        # AXDefaultButton, AXCancelButton and AXSections are all advertised and
        # nil, its only button is CloseButton, and AXConfirm on the field
        # reports success while changing nothing.
        self._require_focused(field)
        logger.info("Sending Return to GoToWindow (panel pid %d)", panel.pid)
        self._post_return(panel.pid)

        await self._await_goto_sheet_closed(panel)
        logger.info("GoToWindow closed")
        return panel

    # ------------------------------------------------------- panel lookup ---

    def panel_service_pids(self) -> list[int]:
        """Every running Open/Save panel service, by PID.

        There is one per host application — three of them on the machine this
        was built against — so the bundle id says *what* a process is and only
        the PID says *which*. No PID is ever hardcoded, and the browser process
        name plays no part.
        """
        return [
            process.pid
            for process in self.api.running_processes()
            if process.bundle_id == PANEL_SERVICE_BUNDLE_ID
        ]

    def find_open_panel(self) -> OpenPanel | None:
        """The panel-service process that actually holds an open panel.

        Chosen by what its accessibility tree contains, not by which PID is
        newest: several of these processes exist at once, and the others are
        idle leftovers from other applications.
        """
        api = self.api
        for pid in self.panel_service_pids():
            app = api.create_application(pid)
            with _suppressed_ax():
                api.set_timeout(app, self.appear_timeout_ms / 1000)
            sheet = find_by_identifier(api, list(search_roots(api, app)), OPEN_PANEL_ID)
            if sheet is not None:
                name = next(
                    (
                        process.name
                        for process in api.running_processes()
                        if process.pid == pid
                    ),
                    "",
                )
                return OpenPanel(pid=pid, app=app, sheet=sheet, name=name)
        return None

    async def _await_panel(self) -> OpenPanel:
        deadline = self._deadline(self.appear_timeout_ms)
        while True:
            panel = self.find_open_panel()
            if panel is not None:
                logger.info(
                    "macOS Open panel found (pid %d%s)",
                    panel.pid,
                    f", {sanitize(panel.name)}" if panel.name else "",
                )
                return panel
            if self._expired(deadline):
                raise NativeFileDialogError(
                    "No native Open panel appeared within "
                    f"{self.appear_timeout_ms // 1000}s.\n"
                    "Nothing was selected. Either the click did not reach the "
                    "site's upload control, or the file chooser was intercepted "
                    f"before the system could show it. Looked for an AXSheet "
                    f"with AXIdentifier {OPEN_PANEL_ID!r} in every running "
                    f"{PANEL_SERVICE_BUNDLE_ID} process."
                )
            await self._tick()

    # --------------------------------------------------------- go to folder --

    async def _open_goto_sheet(self, panel: OpenPanel) -> None:
        """Ask for Go to Folder. The only keystroke this module ever sends."""
        logger.debug("Opening the Go to Folder sheet")
        try:
            self.api.send_key_chord(KEY_CODE_G, command=True, shift=True)
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"macOS would not accept the ⌘⇧G shortcut: {exc}"
            ) from exc

    async def _await_path_field(self, panel: OpenPanel) -> tuple[Any, Any]:
        """Wait for the Go to Folder sheet and its path field. Returns both.

        The sheet is returned too because committing the path is the *sheet's*
        default button, not an action on the field.
        """
        deadline = self._deadline(self.appear_timeout_ms)
        while True:
            goto = self._find_goto_sheet(panel)
            if goto is not None:
                field = find_by_identifier(
                    self.api, [goto], PATH_FIELD_ID, role=PATH_FIELD_ROLE
                )
                if field is not None:
                    logger.debug("Go to Folder path field found")
                    return goto, field
            if self._expired(deadline):
                seen = "the Go to Folder sheet did not open"
                if goto is not None:
                    seen = (
                        f"the {GOTO_SHEET_ID!r} sheet opened but held no "
                        f"{PATH_FIELD_ROLE} with AXIdentifier {PATH_FIELD_ID!r}"
                    )
                raise NativeFileDialogError(
                    "The Go to Folder path field could not be reached within "
                    f"{self.appear_timeout_ms // 1000}s: {seen}.\n"
                    "Nothing was written anywhere, so the dialog is untouched."
                )
            await self._tick()

    def _find_goto_sheet(self, panel: OpenPanel) -> Any:
        # Searched from the panel *and* from the application: this graph is
        # asymmetric — a sheet's AXChildren has been seen pointing back out to
        # the application while its children still name it as their AXParent.
        roots = [panel.sheet, *search_roots(self.api, panel.app)]
        return find_by_identifier(self.api, roots, GOTO_SHEET_ID)

    # ------------------------------------------------------------- writing --

    def _require_writable(self, field: Any) -> None:
        api = self.api
        if not is_enabled(api, field):
            raise NativeFileDialogError(
                f"The Go to Folder path field ({PATH_FIELD_ID}) is disabled, so "
                "the key path was not written."
            )
        if not api.is_settable(field, AX_VALUE):
            raise NativeFileDialogError(
                f"The Go to Folder path field ({PATH_FIELD_ID}) reports AXValue "
                "as not settable, so the key path was not written.\n"
                "Nothing was typed as a fallback — a half-entered path is what "
                "leaves the panel open with nothing to explain it."
            )

    def _write_path(self, field: Any, target: Path) -> None:
        """Set AXValue directly. Nothing is typed, pasted, or clipboarded."""
        logger.debug("Writing the key path into %s", PATH_FIELD_ID)
        try:
            self.api.set_attribute_value(field, AX_VALUE, str(target))
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"The key path could not be written into {PATH_FIELD_ID}: {exc}"
            ) from exc

        written = value_of(self.api, field)
        if written != str(target):
            # The exact failure this replaces: a partially-filled field, then a
            # confirm that goes nowhere.
            raise NativeFileDialogError(
                f"{PATH_FIELD_ID} did not take the key path.\n"
                f"  wanted:   {target}\n"
                f"  field is: {written or '(empty)'}\n"
                "Nothing was confirmed, so the dialog is still on screen and "
                "no wrong file was opened."
            )

    def _require_focused(self, field: Any) -> None:
        """Make sure the key event will land in the path field, not near it.

        A Return is aimed at a process, not at a control, so the control has to
        be the one holding focus before it is sent. If focus cannot be
        established, nothing is sent at all — a keystroke delivered to an
        unknown target is worse than a clear failure.
        """
        api = self.api
        if _is_true(api.attribute_value(field, AX_FOCUSED)):
            return

        if not api.is_settable(field, AX_FOCUSED):
            raise NativeFileDialogError(
                f"{PATH_FIELD_ID} is not focused and its AXFocused is not "
                "settable, so a Return could not be aimed at it.\n"
                "Nothing was sent. The path is entered; the sheet is untouched."
            )

        try:
            api.set_attribute_value(field, AX_FOCUSED, True)
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"{PATH_FIELD_ID} could not be focused: {exc}\nNothing was sent."
            ) from exc

        if not _is_true(api.attribute_value(field, AX_FOCUSED)):
            raise NativeFileDialogError(
                f"{PATH_FIELD_ID} did not take focus, so a Return could not be "
                "aimed at it.\nNothing was sent."
            )

    def _post_return(self, pid: int) -> None:
        """One Return, to that process. Never repeated, never broadcast."""
        try:
            self.api.post_return(pid)
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"The Return key event could not be delivered to pid {pid}: {exc}"
            ) from exc

    # ------------------------------------------------------------- closing --

    async def _await_goto_sheet_closed(self, panel: OpenPanel) -> None:
        deadline = self._deadline(self.close_timeout_ms)
        while True:
            if self._find_goto_sheet(panel) is None:
                logger.debug("Go to Folder sheet closed")
                return
            if self._expired(deadline):
                raise NativeFileDialogError(
                    "Return was sent to the Go to Folder dialog, but "
                    f"{GOTO_SHEET_ID} did not close within "
                    f"{self.close_timeout_ms // 1000}s.\n"
                    "No further Return was sent: repeating a keystroke that "
                    "did nothing would only make the failure harder to read."
                )
            await self._tick()

    def _find_file_row(self, basename: str) -> tuple[Any, Any]:
        """The row holding *basename*, and the list it belongs to.

        Matched on an exact ``AXValue``: a prefix would take
        ``Key-6.dat.backup`` just as happily, and an index or a position would
        take whatever the panel happened to sort first.
        """
        panel = self.find_open_panel()
        if panel is None:
            raise NativeFileDialogError(
                "The Open panel disappeared after navigating to the directory, "
                f"so {basename} could not be selected."
            )

        api = self.api
        roots = [panel.sheet, *search_roots(api, panel.app)]
        name_element = find_where(
            api,
            roots,
            lambda element: role_of(api, element) == FILE_NAME_ROLE
            and value_of(api, element) == basename,
        )
        if name_element is None:
            raise NativeFileDialogError(
                f"{basename} is not in the Open panel's file list.\n"
                "Nothing was selected. The panel is in the right directory — "
                "check that the file is there and visible to the dialog."
            )

        chain = ancestors_of(api, name_element)[1:]
        row = next((e for e in chain if role_of(api, e) == ROW_ROLE), None)
        if row is None:
            raise NativeFileDialogError(
                f"{basename} was found, but it has no {ROW_ROLE} ancestor to "
                "select. Nothing was selected."
            )

        outline = next(
            (
                e
                for e in chain
                if role_of(api, e) == OUTLINE_ROLE
                and identifier_of(api, e) == LIST_VIEW_ID
            ),
            None,
        ) or next((e for e in chain if role_of(api, e) == OUTLINE_ROLE), None)
        if outline is None:
            raise NativeFileDialogError(
                f"{basename} was found, but its row is not inside an "
                f"{OUTLINE_ROLE}, so the selection could not be verified.\n"
                "Nothing was selected."
            )
        return row, outline

    def _select_row(self, row: Any, outline: Any, basename: str) -> None:
        """Select the file by writing the row's AXSelected.

        This is the one writable selection semantic the panel offers: the
        filename's and the cell's AXSelected are both read-only. ``AXOpen`` on
        the filename is deliberately unused — writing the selection and reading
        it back is a step that can be *verified*, which activating something
        is not.
        """
        api = self.api
        if AX_SELECTED not in api.attribute_names(row):
            raise NativeFileDialogError(
                f"The row for {basename} does not expose {AX_SELECTED}, so it "
                "could not be selected. Nothing was written."
            )
        if not api.is_settable(row, AX_SELECTED):
            raise NativeFileDialogError(
                f"The row for {basename} reports {AX_SELECTED} as not settable, "
                "so it could not be selected. Nothing was written."
            )

        logger.info("Selecting file row: %s", basename)
        try:
            api.set_attribute_value(row, AX_SELECTED, True)
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"The row for {basename} refused the selection: {exc}"
            ) from exc

        if not _is_true(api.attribute_value(row, AX_SELECTED)):
            raise NativeFileDialogError(
                f"The row for {basename} did not stay selected after being "
                "set.\nNothing was opened."
            )

        # AppKit's own acknowledgement, not ours: the list has to agree that
        # this row is what is selected.
        selected = api.attribute_value(outline, AX_SELECTED_ROWS) or []
        if not any(api.same_element(row, other) for other in selected):
            raise NativeFileDialogError(
                f"The row for {basename} reports itself selected, but the file "
                f"list's {AX_SELECTED_ROWS} does not contain it "
                f"({len(selected)} row(s) selected).\n"
                "Nothing was opened: the panel would act on whatever it thinks "
                "is selected, which is not this file."
            )
        logger.info("File row selected: %s", basename)

    async def _open_selected_file(self) -> None:
        """Press the panel's commit button, then require the panel to close.

        The panel is looked up again from scratch first: AppKit may have
        rebuilt it while the sheet closed or the selection changed, and a
        handle from before that is a handle to something that no longer exists.
        """
        panel = self.find_open_panel()
        if panel is None:
            logger.info("Open panel closed after selection; nothing left to press")
            return

        button = self._commit_button(panel.sheet)
        self._press(button, OPEN_PANEL_ID)
        await self._await_panel_closed()
        logger.info("Open panel closed")

    def _commit_button(self, sheet: Any) -> Any:
        """The default button if it is usable, else the one named OKButton.

        ``AXDefaultButton`` was nil on this panel before a file was selected,
        so it is tried first and not depended on. The fallback is by
        identifier: the label is localised, the position is the site's to
        change, and the button next to it cancels.
        """
        api = self.api
        candidate = api.attribute_value(sheet, AX_DEFAULT_BUTTON)
        if candidate is not None and self._is_usable_button(candidate):
            logger.info("Pressing Open panel default button")
            return candidate

        logger.info(
            "Open panel has no usable %s; using %s id=%s",
            AX_DEFAULT_BUTTON,
            BUTTON_ROLE,
            OK_BUTTON_ID,
        )
        button = find_where(
            api,
            [sheet],
            lambda element: role_of(api, element) == BUTTON_ROLE
            and identifier_of(api, element) == OK_BUTTON_ID,
        )
        if button is None:
            raise NativeFileDialogError(
                f"The Open panel exposes no usable {AX_DEFAULT_BUTTON} and no "
                f"{BUTTON_ROLE} with AXIdentifier {OK_BUTTON_ID!r}.\n"
                "Nothing was pressed: a localised label or a button position "
                "is how the wrong control — Cancel — gets clicked."
            )
        if identifier_of(api, button) == CANCEL_BUTTON_ID:  # pragma: no cover
            raise NativeFileDialogError("Refusing to press the panel's Cancel button.")
        if not self._is_usable_button(button):
            raise NativeFileDialogError(
                f"The Open panel's {OK_BUTTON_ID} is not usable: it must be an "
                f"enabled {BUTTON_ROLE} offering {AX_PRESS}. Nothing was pressed."
            )
        return button

    def _is_usable_button(self, button: Any) -> bool:
        api = self.api
        return (
            role_of(api, button) == BUTTON_ROLE
            and identifier_of(api, button) != CANCEL_BUTTON_ID
            and is_enabled(api, button)
            and AX_PRESS in api.action_names(button)
        )

    def _press(self, button: Any, label: str) -> None:
        """Exactly one press. Never repeated, never a second control."""
        try:
            self.api.perform_action(button, AX_PRESS)
        except AccessibilityUnavailable as exc:
            raise NativeFileDialogError(
                f"The {label} button refused {AX_PRESS}: {exc}"
            ) from exc

    async def _await_panel_closed(self) -> None:
        deadline = self._deadline(self.close_timeout_ms)
        while True:
            if self.find_open_panel() is None:
                return
            if self._expired(deadline):
                raise NativeFileDialogError(
                    "The macOS Open panel was still on screen "
                    f"{self.close_timeout_ms // 1000}s after the file was "
                    "chosen.\nThe key was not handed to the page."
                )
            await self._tick()

    # ------------------------------------------------------- diagnostics ----

    async def wait_for_dialog(self) -> None:
        """Public wrapper: wait until an Open panel is on screen."""
        await self._await_panel()

    async def send_goto_shortcut(self) -> None:
        """Send ⌘⇧G. Nothing more — no waiting, no typing."""
        self.api.send_key_chord(KEY_CODE_G, command=True, shift=True)

    async def describe_hierarchy(
        self,
        *,
        max_depth: int = MAX_SEARCH_DEPTH,
        max_elements: int = MAX_SEARCH_ELEMENTS,
    ) -> list[dict[str, Any]]:
        """A flat, sanitized description of the panel's accessibility tree."""
        panel = self.find_open_panel()
        if panel is None:
            return []

        api = self.api
        rows: list[dict[str, Any]] = []
        pending: list[tuple[Any, int, str]] = [(panel.sheet, 0, "")]
        seen: list[Any] = []

        while pending and len(rows) < max_elements:
            element, depth, parent_role = pending.pop(0)
            if any(api.same_element(element, other) for other in seen):
                continue
            seen.append(element)

            role = role_of(api, element)
            children = api.attribute_value(element, "AXChildren") or []
            rows.append(
                {
                    "depth": depth,
                    "role": sanitize(role),
                    "identifier": sanitize(identifier_of(api, element)),
                    "value": sanitize(value_of(api, element)),
                    "enabled": is_enabled(api, element),
                    "actions": [sanitize(name) for name in api.action_names(element)],
                    "child_count": len(children),
                    "parent_role": sanitize(parent_role),
                }
            )
            if depth < max_depth:
                pending.extend((child, depth + 1, role) for child in children)
        return rows

    async def ancestry(self) -> list[dict[str, Any]]:
        """The focused element and its parents, for diagnostics."""
        api = self.api
        panel = self.find_open_panel()
        if panel is None:
            return []
        focused = api.attribute_value(panel.app, "AXFocusedUIElement")
        if focused is None:
            return []
        return [
            {
                "depth": depth,
                "role": sanitize(role_of(api, element)),
                "identifier": sanitize(identifier_of(api, element)),
                "value": sanitize(value_of(api, element)),
                "actions": [sanitize(name) for name in api.action_names(element)],
            }
            for depth, element in enumerate(ancestors_of(api, focused))
        ]

    # ------------------------------------------------------------ plumbing --

    @staticmethod
    def _deadline(timeout_ms: int) -> float:
        return asyncio.get_running_loop().time() + timeout_ms / 1000

    @staticmethod
    def _expired(deadline: float) -> bool:
        return asyncio.get_running_loop().time() >= deadline

    @staticmethod
    async def _tick() -> None:
        await asyncio.sleep(_POLL_INTERVAL_MS / 1000)


class _suppressed_ax:  # noqa: N801 - a context manager, used like one
    """Ignore an Accessibility hiccup that must not stop a diagnostic."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


def native_file_selector(
    *,
    mode: str,
    process_name: str = DEFAULT_MACOS_PROCESS,
    appear_timeout_ms: int = 15_000,
    platform: str | None = None,
    api: AxApi | None = None,
) -> NativeFileSelector | None:
    """The selector for this machine, or ``None`` in ``chooser`` mode.

    ``native`` never degrades into ``chooser``. The two are not
    interchangeable: the chooser path is *known* not to authenticate against
    ID.GOV.UA, so silently substituting it would turn "this platform is not
    supported yet" into a run that looks configured, does the whole journey and
    resets at the key form for reasons nothing on screen explains.

    So an unsupported platform is a configuration error, raised before the
    browser is touched.
    """
    if mode == "chooser":
        logger.info(
            "authentication.file_selection is 'chooser': using Playwright's "
            "intercepted file chooser instead of the native dialog."
        )
        return None

    system = platform if platform is not None else sys.platform
    if system != "darwin":
        raise ConfigError(
            "Native file selection is configured, but no native file selector "
            f"is implemented for platform: {system}\n"
            "\n"
            "This is not falling back to Playwright's file chooser on purpose: "
            "ID.GOV.UA reads a key uploaded that way and then resets the form, "
            "so the run would fail later and less clearly.\n"
            "Run this on macOS, or set authentication.file_selection to "
            "'chooser' in config/flow.yaml if you are deliberately reproducing "
            "that failure."
        )

    return MacOSFileSelector(
        api=api, process_name=process_name, appear_timeout_ms=appear_timeout_ms
    )
