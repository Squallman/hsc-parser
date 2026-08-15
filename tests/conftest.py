"""Shared fakes.

The unit tests must never need a browser: everything below is a duck-typed
stand-in for the small slice of the Playwright API this project uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


class FakeElement:
    def __init__(self, *, visible: bool = True, **attrs: Any) -> None:
        self.visible = visible
        self.attrs: dict[str, Any] = {
            "tag": attrs.pop("tag", "button"),
            "role": attrs.pop("role", None),
            "text": attrs.pop("text", ""),
            "aria_label": attrs.pop("aria_label", None),
            "id": attrs.pop("id", None),
            "data_testid": attrs.pop("data_testid", None),
            **attrs,
        }


class OptionNotFound(Exception):
    """Stands in for the error Playwright raises for an unknown ``<option>``."""


class FakeLocator:
    """Implements count/nth/is_visible/evaluate/click/fill/set_input_files.

    A ``<select>`` is faked by giving the element an ``options`` list and,
    optionally, a ``selected`` label — enough for ``select_option(label=...)``
    and for the select-state snapshot LoginPage evaluates.
    """

    def __init__(self, elements: list[FakeElement], index: int | None = None) -> None:
        self._elements = elements
        self._index = index
        self.clicked = False
        self.filled: str | None = None
        self.files: str | None = None

    # -- selection --------------------------------------------------------
    def nth(self, index: int) -> FakeLocator:
        child = FakeLocator(self._elements, index)
        child._parent = self  # type: ignore[attr-defined]
        return child

    async def count(self) -> int:
        return len(self._elements)

    def locator(self, value: str) -> FakeLocator:
        """A lookup *inside* this element, as Playwright's Locator does.

        A fake element declares what it contains in a ``children`` mapping, so a
        calendar month can hold its own day buttons and a day number can only
        ever be found through the month it belongs to.
        """
        children = self._element.attrs.get("children") or {}
        return FakeLocator(list(children.get(value, [])))

    @property
    def _element(self) -> FakeElement:
        assert self._index is not None, "nth() must be called before element access"
        return self._elements[self._index]

    # -- queries ----------------------------------------------------------
    async def is_visible(self) -> bool:
        return self._element.visible

    async def evaluate(self, script: str) -> Any:
        # Every script this project evaluates on an element is recorded, so a
        # test can prove a password field was only ever asked a yes/no question.
        self._element.attrs.setdefault("evaluated", []).append(script)

        if ".length > 0" in script:
            # Presence only: the value itself never leaves the "browser".
            return len(str(self._element.attrs.get("value") or "")) > 0
        if "selectedOptions" in script:
            selected = self._element.attrs.get("selected")
            return {
                "tag": self._element.attrs.get("tag", ""),
                "options": list(self._element.attrs.get("options") or []),
                "selected": [] if selected is None else [selected],
            }
        return dict(self._element.attrs)

    async def inner_text(self) -> str:
        return str(self._element.attrs.get("text", ""))

    async def is_disabled(self) -> bool:
        return bool(self._element.attrs.get("disabled", False))

    async def is_enabled(self) -> bool:
        return not await self.is_disabled()

    # -- actions ----------------------------------------------------------
    async def click(self) -> None:
        self._element.attrs["clicked"] = True
        self.clicked = True

    async def fill(self, value: str) -> None:
        self._element.attrs["value"] = value
        # Every fill is kept, so a test can prove the field was cleared first.
        self._element.attrs.setdefault("fills", []).append(value)
        self.filled = value

    async def set_input_files(self, path: str) -> None:
        self.files = path
        self._element.attrs["files"] = path

    async def select_option(self, *, label: str) -> list[str]:
        options = list(self._element.attrs.get("options") or [])
        if label not in options:
            # Playwright waits, then fails; the distinction does not matter to
            # a caller, which sees an exception either way.
            raise OptionNotFound(f"{label!r} is not one of {options}")
        self._element.attrs["selected"] = label
        return [label]

    async def highlight(self) -> None:
        return None


class FakeConsoleMessage:
    """Playwright's ConsoleMessage, reduced to what the observer reads."""

    def __init__(self, type: str, text: str) -> None:  # noqa: A002 - matches the API
        self.type = type
        self.text = text


class FakeRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class FakeResponse:
    """Playwright's Response, reduced to what the observer reads."""

    def __init__(
        self, url: str, status: int, method: str = "GET", content_type: str = "text/html"
    ) -> None:
        self.url = url
        self.status = status
        self.request = FakeRequest(method)
        self.headers = {"content-type": content_type}


class FakeNativeFileSelector:
    """Stands in for the OS Open dialog.

    Records that the native step ran, then performs what a real selection does
    to the page: the file arrives at the input the browser opened the dialog
    for. ``fails`` reproduces a dialog that could not be driven.
    """

    def __init__(self, page: Any, *, fails: str | None = None) -> None:
        self.page = page
        self.selected: list[str] = []
        self._fails = fails

    async def select_file(self, path: Path) -> None:
        self.page.record("native:select")
        if self._fails is not None:
            from hsc_queue_monitor.models import NativeFileDialogError

            raise NativeFileDialogError(self._fails)
        self.selected.append(str(path))
        self.page.native_file_selected(str(path))


class FakeElementHandle:
    """The ElementHandle a FileChooser exposes, reduced to what is read."""

    def __init__(self, element_id: str) -> None:
        self.element_id = element_id

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "matches" in script:
            # A CSS selector matches this element only if it is its own id.
            return {"id": self.element_id, "matches": arg == f"#{self.element_id}"}
        return self.element_id


class FakeFileChooser:
    """Playwright's FileChooser: the page asking for a file, not an OS dialog."""

    def __init__(self, element_id: str, on_set: Any = None) -> None:
        self.element = FakeElementHandle(element_id)
        self.files: str | None = None
        self._on_set = on_set

    async def set_files(self, path: str) -> None:
        self.files = path
        if self._on_set is not None:
            self._on_set(path)


class FakeEventInfo:
    """What ``async with expect_*()`` yields: a deferred ``.value``."""

    def __init__(self, resolve: Any) -> None:
        self._resolve = resolve

    @property
    def value(self) -> Any:
        async def _await_value() -> Any:
            resolved = self._resolve()
            if resolved is None:
                raise TimeoutError("no file chooser was opened")
            return resolved

        return _await_value()


class FakeExpectFileChooser:
    """``page.expect_file_chooser()`` — arms a listener, then yields its value.

    Entering it records the arming, so a test can prove the listener existed
    *before* the click that triggers the event.
    """

    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def __aenter__(self) -> FakeEventInfo:
        self._page.on_file_chooser_armed()
        return FakeEventInfo(lambda: self._page.pending_file_chooser)

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class FakePage:
    """Records which Playwright locator API was used, and with what arguments."""

    def __init__(
        self,
        elements: list[FakeElement] | None = None,
        matches: dict[str, list[FakeElement]] | None = None,
    ) -> None:
        """``elements`` answers every query; ``matches`` answers per selector
        value (the css/text/label argument, or the accessible name for roles)."""
        self.url = "https://eqn.hsc.gov.ua/cabinet/queue"
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._matches = matches
        self._listeners: dict[str, list[Any]] = {}
        if elements is not None:
            self._elements = elements
        else:
            self._elements = [] if matches is not None else [FakeElement()]

    # -- events -----------------------------------------------------------
    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        handlers = self._listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event: str, payload: Any) -> None:
        """Deliver an event the way Playwright does: synchronously."""
        for handler in list(self._listeners.get(event, [])):
            handler(payload)

    @property
    def listener_count(self) -> int:
        return sum(len(handlers) for handlers in self._listeners.values())

    @property
    def body_text(self) -> str:
        """What the observer reads as visible page text."""
        return ""

    # -- file chooser -----------------------------------------------------
    pending_file_chooser: FakeFileChooser | None = None

    def expect_file_chooser(self, **_kwargs: Any) -> FakeExpectFileChooser:
        return FakeExpectFileChooser(self)

    def on_file_chooser_armed(self) -> None:
        """Hook for subclasses that record the order of events."""

    def _record(self, api: str, *args: Any, **kwargs: Any) -> FakeLocator:
        self.calls.append((api, args, kwargs))
        if self._matches is None:
            return FakeLocator(self._elements)
        key = kwargs.get("name") if api == "get_by_role" else (args[0] if args else None)
        return FakeLocator(self._matches.get(key, self._elements))

    def get_by_role(self, role: str, **kwargs: Any) -> FakeLocator:
        return self._record("get_by_role", role, **kwargs)

    def get_by_text(self, value: str, **kwargs: Any) -> FakeLocator:
        return self._record("get_by_text", value, **kwargs)

    def get_by_label(self, value: str, **kwargs: Any) -> FakeLocator:
        return self._record("get_by_label", value, **kwargs)

    def get_by_placeholder(self, value: str, **kwargs: Any) -> FakeLocator:
        return self._record("get_by_placeholder", value, **kwargs)

    def get_by_test_id(self, value: str) -> FakeLocator:
        return self._record("get_by_test_id", value)

    def locator(self, value: str) -> FakeLocator:
        return self._record("locator", value)

    async def wait_for_load_state(self, *_a: Any, **_k: Any) -> None:
        return None

    async def goto(self, url: str, **_k: Any) -> None:
        self.calls.append(("goto", (url,), {}))
        self.url = url

    async def title(self) -> str:
        return "Fake"

    async def evaluate(self, script: str) -> Any:
        """Stands in for the two page-level scripts this project evaluates."""
        if "document.body" in script:
            return self.body_text
        return []  # the interactive-element dump used by diagnostics

    async def screenshot(self, path: str = "", **_k: Any) -> None:
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fake-png")

    @property
    def navigations(self) -> list[str]:
        return [args[0] for api, args, _ in self.calls if api == "goto"]

    @property
    def last_call(self) -> tuple[str, tuple[Any, ...], dict[str, Any]]:
        return self.calls[-1]


@pytest.fixture
def config_dir() -> Path:
    return CONFIG_DIR
