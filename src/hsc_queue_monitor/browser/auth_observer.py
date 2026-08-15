"""Evidence collection for the ID.GOV.UA phase of authentication.

The site can process a submitted key and then quietly put the same form back on
screen. Nothing in the DOM says why, so this module records what a person would
have watched happen: the text that appeared, what the console complained about
and which requests came back with what status.

What is deliberately **not** recorded, at any verbosity:

* request or response bodies — the .dat and the password travel in them;
* headers, cookies, or anything carrying the session;
* query-string values that identify the session (``code``, ``state``, …);
* full page HTML.

Everything kept is passed through the redactor before it is stored, so an
artifact cannot leak a secret even if one reaches the page as visible text.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page

from ..logging_config import sanitize

logger = logging.getLogger(__name__)

#: A snapshot is a screenful of text, so the ring buffer is deliberately small.
MAX_TEXT_STATES = 12
MAX_CONSOLE_MESSAGES = 50
MAX_RESPONSES = 120
#: Enough for a form plus a message; not a page dump.
MAX_TEXT_CHARS = 4_000

#: Console levels worth keeping. Everything else on this site is noise.
KEPT_CONSOLE_LEVELS: frozenset[str] = frozenset({"error", "warning"})

_BODY_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


def _collapse(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"


@dataclass(frozen=True, slots=True)
class TextState:
    """One distinct rendering of the visible page text."""

    phase: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "text": self.text}


@dataclass(frozen=True, slots=True)
class ConsoleRecord:
    phase: str
    kind: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "kind": self.kind, "text": self.text}


@dataclass(frozen=True, slots=True)
class ResponseRecord:
    """Safe metadata about one response. Never its body."""

    phase: str
    method: str
    host: str
    path: str
    status: int
    content_type: str

    @property
    def failed(self) -> bool:
        return self.status >= 400

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "method": self.method,
            "host": self.host,
            "path": self.path,
            "status": self.status,
            "content_type": self.content_type,
        }


class AuthObserver:
    """Watches one page for the duration of the authentication journey.

    Attach with :meth:`start`, detach with :meth:`stop`. ``phase`` labels every
    record with the step that was running when it arrived, which is what makes
    it possible to tell a provider-change request from a submit request without
    guessing from timestamps.
    """

    def __init__(
        self,
        page: Page,
        *,
        hosts: Sequence[str] = ("id.gov.ua",),
        phase: str = "idgov",
    ) -> None:
        self.page = page
        self.hosts = tuple(hosts)
        self._phase = phase
        #: Phases in the order they became active. Kept so the artifact can
        #: distinguish "this step ran and made no requests" from "this step
        #: never ran" — a bare count dictionary cannot say which.
        self._entered: list[str] = [phase]
        self._texts: deque[TextState] = deque(maxlen=MAX_TEXT_STATES)
        self._console: deque[ConsoleRecord] = deque(maxlen=MAX_CONSOLE_MESSAGES)
        self._responses: deque[ResponseRecord] = deque(maxlen=MAX_RESPONSES)
        self._started = False

    # ---------------------------------------------------------------- phase --

    @property
    def phase(self) -> str:
        """The journey step currently running. Every record is tagged with it."""
        return self._phase

    @phase.setter
    def phase(self, name: str) -> None:
        self.enter_phase(name)

    def enter_phase(self, name: str) -> None:
        """Start attributing new records to *name*.

        Records are tagged when the event is *delivered*, which is the only
        thing an observer can honestly claim: a response that arrives after the
        next step began belongs, as far as anyone here can tell, to the step
        that was running when it landed.
        """
        if name == self._phase:
            return
        self._phase = name
        if name not in self._entered:
            self._entered.append(name)
        logger.debug("Authentication phase: %s", name)

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> None:
        if self._started:  # pragma: no cover - guarded by the journey
            return
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.on("response", self._on_response)
        self._started = True

    def stop(self) -> None:
        """Detach. Safe to call twice and safe if the page is already closed."""
        if not self._started:
            return
        self._started = False
        for event, handler in (
            ("console", self._on_console),
            ("pageerror", self._on_page_error),
            ("response", self._on_response),
        ):
            try:
                self.page.remove_listener(event, handler)
            except Exception:  # pragma: no cover - page already closed
                logger.debug("Could not detach the %s listener", event)

    # ------------------------------------------------------------ listeners --

    def _on_console(self, message: Any) -> None:
        try:
            level = str(getattr(message, "type", "") or "")
            if level not in KEPT_CONSOLE_LEVELS:
                return
            self._console.append(
                ConsoleRecord(
                    phase=self.phase,
                    kind=f"console.{level}",
                    text=sanitize(_collapse(str(getattr(message, "text", "")), 500)),
                )
            )
        except Exception:  # pragma: no cover - never break the journey
            logger.debug("Could not record a console message")

    def _on_page_error(self, error: Any) -> None:
        try:
            self._console.append(
                ConsoleRecord(
                    phase=self.phase,
                    kind="pageerror",
                    text=sanitize(_collapse(str(error), 500)),
                )
            )
        except Exception:  # pragma: no cover
            logger.debug("Could not record a page error")

    def _on_response(self, response: Any) -> None:
        """Record method/host/path/status/content-type — nothing else.

        The query string is dropped whole rather than filtered: an OIDC
        redirect carries the authorization code there, and a path plus a status
        already answers "did the submit fail".
        """
        try:
            parts = urlsplit(str(getattr(response, "url", "")))
            host = (parts.hostname or "").lower()
            if not any(host == h or host.endswith(f".{h}") for h in self.hosts):
                return

            content_type = ""
            try:
                content_type = str(response.headers.get("content-type", ""))
            except Exception:  # pragma: no cover - headers unavailable
                content_type = "(unavailable)"

            self._responses.append(
                ResponseRecord(
                    phase=self.phase,
                    method=str(getattr(response.request, "method", "?")),
                    host=host,
                    path=parts.path or "/",
                    status=int(getattr(response, "status", 0) or 0),
                    content_type=content_type.split(";")[0].strip(),
                )
            )
        except Exception:  # pragma: no cover - never break the journey
            logger.debug("Could not record a response")

    # ----------------------------------------------------------- page text ---

    @property
    def last_text(self) -> str | None:
        return self._texts[-1].text if self._texts else None

    async def capture_text(self) -> str | None:
        """Read the visible page text; store it only when it is new.

        Returns the current text so a caller can compare states without
        reaching into the buffer. Never raises: the page is allowed to navigate
        out from under a capture.
        """
        try:
            raw = await self.page.evaluate(_BODY_TEXT_JS)
        except Exception:  # pragma: no cover - mid-navigation
            return None

        text: str = sanitize(_collapse(str(raw or "")))
        if text and text != self.last_text:
            self._texts.append(TextState(phase=self.phase, text=text))
        return text

    # ------------------------------------------------------------- reading ---

    @property
    def texts(self) -> list[TextState]:
        return list(self._texts)

    @property
    def console(self) -> list[ConsoleRecord]:
        return list(self._console)

    @property
    def responses(self) -> list[ResponseRecord]:
        return list(self._responses)

    def failed_responses(self) -> list[ResponseRecord]:
        return [record for record in self._responses if record.failed]

    def responses_in(self, phase: str) -> list[ResponseRecord]:
        """What the site did during one step — the answer to "does changing the
        provider trigger a request?"."""
        return [record for record in self._responses if record.phase == phase]

    def summary(self) -> str:
        """One line for the log: counts only, no content.

        The per-phase breakdown is the answer to "does changing the provider
        start its own request?" — if ``provider`` is in there, it does.
        """
        failed = self.failed_responses()
        by_phase = dict(self.phases())
        parts = [
            f"{len(self._texts)} text state(s)",
            f"{len(self._console)} console/page error(s)",
            f"{len(self._responses)} id.gov.ua response(s)",
        ]
        if by_phase:
            parts.append(
                "by phase: " + ", ".join(f"{k}={v}" for k, v in sorted(by_phase.items()))
            )
        if failed:
            codes = ", ".join(f"{r.status} {r.method} {r.path}" for r in failed[:5])
            parts.append(f"{len(failed)} failed: {codes}")
        return "; ".join(parts)

    def phases(self) -> Iterator[tuple[str, int]]:
        """``(phase, response count)`` for every phase that ran, in order.

        Zero-filled deliberately. A phase missing from the mapping used to be
        ambiguous — no traffic, or no tagging? — which made the breakdown
        useless as evidence. Now an absent key means the step never ran.
        """
        counts = dict.fromkeys(self._entered, 0)
        for record in self._responses:
            counts[record.phase] = counts.get(record.phase, 0) + 1
        return iter(counts.items())

    @property
    def phases_entered(self) -> list[str]:
        return list(self._entered)
