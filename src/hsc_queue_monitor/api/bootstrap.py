"""Whether opening the queue page is what mints the queue session.

One measured fact started this: after a verified ``/cabinet`` the browser
sometimes carries no ``__Host-next.equeue-session`` at all, and in that state the
departments endpoint answers ``204 No Content``. When the wizard had been opened
first, the same sequence returned JSON. So the API appears to need a session the
*queue page* creates and signing in does not.

This module is the smallest experiment that can test that: one navigation to the
queue URL, the cookie names fingerprinted either side of it, and no click of any
kind. It deliberately breaks the rule stated in
:mod:`~..pages.queue_page` — that ``/cabinet/queue`` is only ever reached by
clicking — because navigating straight there is precisely the variable under
test. It is opt-in (``--open-queue``), it is not on any production path, and it
does not enter the wizard: no registration button, no exam type, no category, no
centre, no date, no time.

Nothing here talks to Playwright. It compares cookie state and renders the
finding; the one navigation belongs to the caller, which already holds the page.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .probe import WIZARD_COOKIE, fingerprint

#: What the navigation did to the queue-session cookie.
STATE_MINTED = "minted"
STATE_CHANGED = "changed"
STATE_UNCHANGED = "unchanged"
STATE_ABSENT = "absent"

_MEANING = {
    STATE_MINTED: "the navigation created the queue session cookie",
    STATE_CHANGED: "the navigation replaced the existing queue session cookie",
    STATE_UNCHANGED: "the cookie was already there and the navigation left it alone",
    STATE_ABSENT: "the navigation did not create the queue session cookie",
}


def wizard_fingerprint(cookies: Iterable[Mapping[str, Any]]) -> str | None:
    """The queue-session cookie's fingerprint, or ``None`` when it is absent.

    A fingerprint, never the value — the whole point is to see *whether* the
    cookie changed, which needs nothing more than that.
    """
    for cookie in cookies:
        if cookie.get("name") == WIZARD_COOKIE:
            return fingerprint(str(cookie.get("value") or ""))
    return None


@dataclass(frozen=True, slots=True)
class QueueBootstrap:
    """One navigation to the queue page, and what it did to the session cookie."""

    url: str
    final_url: str
    before: str | None = None
    after: str | None = None

    @property
    def state(self) -> str:
        if self.after is None:
            return STATE_ABSENT
        if self.before is None:
            return STATE_MINTED
        return STATE_UNCHANGED if self.before == self.after else STATE_CHANGED

    @property
    def redirected(self) -> bool:
        """Whether HSC sent the browser somewhere other than the queue page."""
        return self.final_url.rstrip("/") != self.url.rstrip("/")

    @property
    def worked(self) -> bool:
        """Whether the queue session exists after the navigation, however it got there."""
        return self.after is not None

    @staticmethod
    def _describe(state: str | None) -> str:
        return "absent" if state is None else f"present ({state})"

    def render(self) -> list[str]:
        lines = [
            "Queue bootstrap:",
            f"  URL:       {self.url}",
            f"  final URL: {self.final_url}",
            f"  {WIZARD_COOKIE} before: {self._describe(self.before)}",
            f"  {WIZARD_COOKIE} after:  {self._describe(self.after)}",
            f"  -> {_MEANING[self.state]}",
        ]
        if self.redirected:
            lines.append("  -> HSC redirected away from the queue page")
        return lines
