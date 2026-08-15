"""The macOS Accessibility API, as much of it as this project needs.

Low-level and UI-agnostic on purpose: this module knows how to read, write and
act on accessibility elements, and nothing about file dialogs or ID.GOV.UA.
:mod:`.native_files` builds the file-panel behaviour on top of it, and the
debug inspector in ``scripts/`` imports the same adapter rather than carrying a
second copy of the PyObjC bindings.

Everything goes through :class:`AxApi` so the whole native path can be tested
without a Mac in the room, and so the PyObjC import lives in exactly one place.

Reads are bounded: searches take a depth and an element budget, because an
accessibility tree can be large and, on at least one live panel, cyclic.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..models import AccessibilityUnavailable

logger = logging.getLogger(__name__)

#: kAXErrorSuccess.
AX_SUCCESS = 0

AX_ROLE = "AXRole"
AX_SUBROLE = "AXSubrole"
AX_TITLE = "AXTitle"
AX_DESCRIPTION = "AXDescription"
AX_VALUE = "AXValue"
AX_ENABLED = "AXEnabled"
AX_FOCUSED = "AXFocused"
AX_IDENTIFIER = "AXIdentifier"
AX_PLACEHOLDER = "AXPlaceholderValue"
AX_CHILDREN = "AXChildren"
AX_WINDOWS = "AXWindows"
AX_PARENT = "AXParent"
AX_FOCUSED_ELEMENT = "AXFocusedUIElement"
AX_DEFAULT_BUTTON = "AXDefaultButton"
AX_SELECTED = "AXSelected"
AX_SELECTED_ROWS = "AXSelectedRows"

AX_PRESS = "AXPress"
AX_CONFIRM = "AXConfirm"

#: Whatever is in one of these is a password. Its value is never read.
SECURE_ROLE = "AXSecureTextField"

#: Virtual key code for "G" (kVK_ANSI_G), for the Go to Folder shortcut.
KEY_CODE_G = 5

#: kVK_Return. The physical Return key, not a Unicode carriage return: the
#: sheet responds to the key event, and a synthesised character is not one.
KEY_CODE_RETURN = 36

#: Bounds for a search. Generous enough for a file panel, small enough that a
#: pathological tree cannot hold anything up.
MAX_SEARCH_DEPTH = 14
MAX_SEARCH_ELEMENTS = 600

#: The XPC service that hosts every native Open/Save panel. One instance per
#: host application, so the bundle id identifies the *kind* of process and the
#: PID identifies which one.
PANEL_SERVICE_BUNDLE_ID = "com.apple.appkit.xpc.openAndSavePanelService"


@dataclass(frozen=True, slots=True)
class RunningProcess:
    pid: int
    bundle_id: str
    name: str


@runtime_checkable
class AxApi(Protocol):
    """The slice of the Accessibility C API this project uses.

    Reads, one write, one action, and the identity test. Deliberately small:
    everything above it is ordinary Python that can be tested.
    """

    def create_application(self, pid: int) -> Any: ...

    def set_timeout(self, element: Any, seconds: float) -> None: ...

    def attribute_names(self, element: Any) -> list[str]: ...

    def attribute_value(self, element: Any, name: str) -> Any: ...

    def set_attribute_value(self, element: Any, name: str, value: Any) -> None:
        """Write an attribute. Raises :class:`AccessibilityUnavailable` on refusal."""
        ...

    def action_names(self, element: Any) -> list[str]: ...

    def perform_action(self, element: Any, name: str) -> None:
        """Perform an accessibility action. Raises on an AX error."""
        ...

    def is_settable(self, element: Any, name: str) -> bool: ...

    def pid_of(self, element: Any) -> int | None: ...

    def same_element(self, first: Any, second: Any) -> bool:
        """Whether two handles refer to the same element.

        Python identity is the wrong test: two separately-obtained handles for
        one element are different objects.
        """
        ...

    def running_processes(self) -> list[RunningProcess]: ...

    def send_key_chord(
        self, key_code: int, *, command: bool = False, shift: bool = False
    ) -> None:
        """Post a keyboard shortcut to the frontmost application.

        Used only for ⌘⇧G. Nothing types text this way — text is written
        through :meth:`set_attribute_value`.
        """
        ...

    def post_return(self, pid: int) -> None:
        """Send one Return key press to *pid* and nothing else.

        Addressed at a process rather than at the front of the system: the
        terminal running this is very often frontmost, and a Return delivered
        there would be a Return delivered to the wrong window.
        """
        ...


class PyObjCAxApi:
    """:class:`AxApi` over PyObjC's ApplicationServices bindings."""

    def __init__(self) -> None:
        self._ax = _import_application_services()

    # ------------------------------------------------------------- reading --

    def create_application(self, pid: int) -> Any:
        return self._ax.AXUIElementCreateApplication(pid)

    def set_timeout(self, element: Any, seconds: float) -> None:
        self._ax.AXUIElementSetMessagingTimeout(element, seconds)

    def attribute_names(self, element: Any) -> list[str]:
        error, names = self._ax.AXUIElementCopyAttributeNames(element, None)
        return [str(name) for name in names] if error == AX_SUCCESS and names else []

    def attribute_value(self, element: Any, name: str) -> Any:
        error, value = self._ax.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == AX_SUCCESS else None

    def set_attribute_value(self, element: Any, name: str, value: Any) -> None:
        error = self._ax.AXUIElementSetAttributeValue(element, name, value)
        if error != AX_SUCCESS:
            raise AccessibilityUnavailable(
                f"macOS refused to set {name} (AX error {error})."
            )

    def action_names(self, element: Any) -> list[str]:
        error, names = self._ax.AXUIElementCopyActionNames(element, None)
        return [str(name) for name in names] if error == AX_SUCCESS and names else []

    def perform_action(self, element: Any, name: str) -> None:
        error = self._ax.AXUIElementPerformAction(element, name)
        if error != AX_SUCCESS:
            raise AccessibilityUnavailable(
                f"macOS refused the {name} action (AX error {error})."
            )

    def is_settable(self, element: Any, name: str) -> bool:
        error, settable = self._ax.AXUIElementIsAttributeSettable(element, name, None)
        return bool(settable) if error == AX_SUCCESS else False

    def pid_of(self, element: Any) -> int | None:
        error, pid = self._ax.AXUIElementGetPid(element, None)
        return int(pid) if error == AX_SUCCESS else None

    def same_element(self, first: Any, second: Any) -> bool:
        if first is second:
            return True
        with suppress(Exception):
            return bool(self._ax.CFEqual(first, second))
        return bool(first == second)  # pragma: no cover - CFEqual is always there

    # ------------------------------------------------------------ the system --

    def running_processes(self) -> list[RunningProcess]:
        from AppKit import NSWorkspace  # noqa: PLC0415

        processes: list[RunningProcess] = []
        for application in NSWorkspace.sharedWorkspace().runningApplications():
            with suppress(Exception):  # pragma: no cover - a process exiting
                processes.append(
                    RunningProcess(
                        pid=int(application.processIdentifier()),
                        bundle_id=str(application.bundleIdentifier() or ""),
                        name=str(application.localizedName() or ""),
                    )
                )
        return processes

    def send_key_chord(
        self, key_code: int, *, command: bool = False, shift: bool = False
    ) -> None:
        ax = self._ax
        flags = 0
        if command:
            flags |= ax.kCGEventFlagMaskCommand
        if shift:
            flags |= ax.kCGEventFlagMaskShift

        for pressed in (True, False):
            event = ax.CGEventCreateKeyboardEvent(None, key_code, pressed)
            if event is None:  # pragma: no cover - event creation failure
                raise AccessibilityUnavailable("macOS would not create a key event.")
            ax.CGEventSetFlags(event, flags)
            ax.CGEventPost(ax.kCGHIDEventTap, event)

    def post_return(self, pid: int) -> None:
        """One Return, down and up, delivered to one process.

        ``CGEventPostToPid`` rather than ``CGEventPost``: the latter goes to
        whatever is frontmost, which while this runs is usually the terminal.
        No modifiers are set — a plain Return is the whole event.
        """
        ax = self._ax
        for pressed in (True, False):
            event = ax.CGEventCreateKeyboardEvent(None, KEY_CODE_RETURN, pressed)
            if event is None:  # pragma: no cover - event creation failure
                raise AccessibilityUnavailable(
                    "macOS would not create a Return key event."
                )
            ax.CGEventSetFlags(event, 0)
            ax.CGEventPostToPid(pid, event)


def _import_application_services() -> Any:
    try:
        import ApplicationServices  # noqa: PLC0415
    except ImportError as exc:
        raise AccessibilityUnavailable(
            "Native macOS file selection needs the Accessibility API, which "
            "comes from PyObjC:\n"
            "    pip install -e '.[macos-debug]'\n"
            "or: pip install pyobjc-framework-ApplicationServices\n"
            "This dependency is macOS-only; nothing else in the project needs it."
        ) from exc
    return ApplicationServices


def load_ax_api(platform: str | None = None) -> AxApi:
    """The Accessibility API for this machine, or a clear error saying why not.

    Never returns something that silently does nothing: a caller that asked for
    the native API and did not get it must find out here, not three steps later
    when a dialog fails to move.
    """
    system = platform if platform is not None else sys.platform
    if system != "darwin":
        raise AccessibilityUnavailable(
            "The macOS Accessibility API is not available on platform "
            f"{system!r}. Native file selection is macOS-only."
        )
    return PyObjCAxApi()


# --------------------------------------------------------------------------- #
# Bounded reading
# --------------------------------------------------------------------------- #


def identifier_of(api: AxApi, element: Any) -> str:
    return _text(api.attribute_value(element, AX_IDENTIFIER))


def role_of(api: AxApi, element: Any) -> str:
    return _text(api.attribute_value(element, AX_ROLE))


def is_enabled(api: AxApi, element: Any) -> bool:
    return bool(api.attribute_value(element, AX_ENABLED))


def value_of(api: AxApi, element: Any) -> str:
    """The element's value, unless it is a secure field — those are never read."""
    if role_of(api, element) == SECURE_ROLE:
        return ""
    return _text(api.attribute_value(element, AX_VALUE))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def children_of(api: AxApi, element: Any) -> list[Any]:
    return list(api.attribute_value(element, AX_CHILDREN) or [])


def ancestors_of(
    api: AxApi, element: Any, *, max_depth: int = MAX_SEARCH_DEPTH
) -> list[Any]:
    """*element* and every parent above it, bounded and cycle-safe."""
    chain: list[Any] = []
    handle: Any = element
    while handle is not None and len(chain) < max_depth:
        if any(api.same_element(handle, seen) for seen in chain):
            break
        chain.append(handle)
        handle = api.attribute_value(handle, AX_PARENT)
    return chain


def search_roots(api: AxApi, app: Any) -> Iterator[Any]:
    """Everywhere worth starting a search in one application.

    ``AXWindows`` first, then the focused element's ancestry — because a live
    Open panel process was observed publishing no windows at all while its
    focused element still led all the way up to the panel.
    """
    yield from api.attribute_value(app, AX_WINDOWS) or []
    focused = api.attribute_value(app, AX_FOCUSED_ELEMENT)
    if focused is not None:
        yield from ancestors_of(api, focused)


def find_by_identifier(
    api: AxApi,
    roots: Sequence[Any],
    identifier: str,
    *,
    role: str | None = None,
    max_depth: int = MAX_SEARCH_DEPTH,
    max_elements: int = MAX_SEARCH_ELEMENTS,
) -> Any | None:
    """The first element with this ``AXIdentifier``, searched breadth-first.

    An identifier, not a role and not a position: the roles on this panel are
    shared by several controls, and the positions are the site's to change.
    """
    return find_where(
        api,
        roots,
        lambda element: identifier_of(api, element) == identifier
        and (role is None or role_of(api, element) == role),
        max_depth=max_depth,
        max_elements=max_elements,
    )


def find_where(
    api: AxApi,
    roots: Sequence[Any],
    matches: Callable[[Any], bool],
    *,
    max_depth: int = MAX_SEARCH_DEPTH,
    max_elements: int = MAX_SEARCH_ELEMENTS,
) -> Any | None:
    """Bounded breadth-first search. Never materialises a whole subtree."""
    pending: list[tuple[Any, int]] = [(root, 0) for root in roots]
    seen: list[Any] = []
    inspected = 0

    while pending and inspected < max_elements:
        element, depth = pending.pop(0)
        if any(api.same_element(element, other) for other in seen):
            continue
        seen.append(element)
        inspected += 1

        try:
            if matches(element):
                return element
        except Exception:  # pragma: no cover - element vanished mid-search
            continue

        if depth < max_depth:
            pending.extend((child, depth + 1) for child in children_of(api, element))

    return None
