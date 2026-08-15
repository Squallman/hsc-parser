"""Debug artifacts: screenshots, element dumps and an event journal.

Everything written here is sanitized. We deliberately do **not** dump full HTML,
cookies, storage or request headers, because those carry the live session.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from ..logging_config import sanitize, sanitize_url
from .auth_observer import AuthObserver

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthArtifacts:
    """Where the evidence for one authentication outcome was written."""

    screenshot: Path | None = None
    elements: Path | None = None
    text: Path | None = None
    console: Path | None = None
    network: Path | None = None

    def describe(self) -> str:
        """The block that goes into the failure message.

        Paths are listed in full: a user who has to go hunting for an artifact
        is a user who will not read it.
        """
        rows = (
            ("screenshot", self.screenshot),
            ("elements", self.elements),
            ("visible text", self.text),
            ("console", self.console),
            ("network", self.network),
        )
        written = [f"  {label:<13} {path}" for label, path in rows if path is not None]
        if not written:
            return "  (nothing could be written — check that data/debug/ is writable)"
        return "\n".join(written)

#: Collect anything a user could plausibly interact with.
_INTERACTIVE_JS = """
() => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'label',
    '[role]', '[onclick]', '[tabindex]:not([tabindex="-1"])',
    '[data-testid]', '[data-test-id]', '[data-qa]'
  ].join(',');

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    return true;
  };

  const clip = (s, n) => {
    if (s === null || s === undefined) return null;
    const t = String(s).replace(/\\s+/g, ' ').trim();
    if (!t) return null;
    return t.length > n ? t.slice(0, n) + '…' : t;
  };

  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!isVisible(el) || seen.has(el)) continue;
    seen.add(el);

    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const isSecret = tag === 'input' && (type === 'password' || type === 'file');

    out.push({
      tag,
      type: type,
      role: el.getAttribute('role'),
      text: clip(el.innerText || el.textContent, 120),
      aria_label: clip(el.getAttribute('aria-label'), 120),
      placeholder: clip(el.getAttribute('placeholder'), 120),
      name: el.getAttribute('name'),
      id: el.getAttribute('id'),
      data_testid: el.getAttribute('data-testid')
                || el.getAttribute('data-test-id')
                || el.getAttribute('data-qa'),
      // Values of password and file inputs are never collected.
      value: isSecret ? null : clip(el.value, 80),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    });
  }
  return out;
}
"""


async def collect_interactive_elements(page: Page) -> list[dict[str, Any]]:
    """Sanitized description of every visible interactive element on the page."""
    raw = await page.evaluate(_INTERACTIVE_JS)
    elements: list[dict[str, Any]] = [sanitize(item) for item in raw]
    return elements


class Diagnostics:
    """Writes screenshots, element dumps and ``events.jsonl``."""

    def __init__(self, debug_dir: Path, *, enabled: bool = True) -> None:
        self.debug_dir = debug_dir
        self.error_dir = debug_dir / "errors"
        self.enabled = enabled
        self._counter = 0

    # ------------------------------------------------------------ helpers ---

    def _ensure_dirs(self) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    def _next_index(self) -> int:
        self._counter += 1
        return self._counter

    @staticmethod
    def _slug(text: str) -> str:
        keep = [c if c.isalnum() else "-" for c in text.lower()]
        return "".join(keep).strip("-").replace("--", "-") or "step"

    # -------------------------------------------------------- screenshots ---

    async def screenshot(self, page: Page, label: str, *, index: int | None = None
                         ) -> Path | None:
        """Save ``data/debug/00N-<label>.png``.

        ``index`` reuses a sequence number instead of taking the next one, so a
        screenshot and its element dump can share one.
        """
        if not self.enabled:
            return None
        self._ensure_dirs()
        number = self._next_index() if index is None else index
        path = self.debug_dir / f"{number:03d}-{self._slug(label)}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception as exc:  # pragma: no cover - page may be closing
            logger.debug("Screenshot failed for %s: %s", label, exc)
            return None
        logger.info("Screenshot: %s", path)
        return path

    async def capture_snapshot(self, page: Page, label: str) -> tuple[Path | None, Path | None]:
        """One numbered pair: ``00N-<label>.png`` + ``00N-<label>-elements.json``.

        Used where every capture has to survive — during authentication
        discovery a single overwritten ``page-elements.json`` would lose the
        screen you were trying to record.
        """
        index = self._next_index()
        shot = await self.screenshot(page, label, index=index)
        dump = await self.dump_elements(page, f"{label}-elements", index=index)
        return shot, dump

    async def dump_elements(
        self, page: Page, name: str = "page-elements", *, index: int | None = None
    ) -> Path | None:
        """Save the sanitized interactive-element dump as JSON."""
        self._ensure_dirs()
        prefix = "" if index is None else f"{index:03d}-"
        path = self.debug_dir / f"{prefix}{self._slug(name)}.json"
        try:
            elements = await collect_interactive_elements(page)
        except Exception as exc:  # pragma: no cover - page may be navigating
            logger.warning("Could not collect page elements: %s", exc)
            return None
        payload = {
            "captured_at": datetime.now(UTC).isoformat(),
            "url": sanitize_url(page.url),
            "title": sanitize(await page.title()),
            "element_count": len(elements),
            "elements": elements,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Wrote %d visible interactive elements to %s", len(elements), path)
        return path

    # ------------------------------------------------------------- events ---

    def record_event(
        self,
        *,
        step: str,
        url_before: str,
        url_after: str,
        selector_name: str | None,
        result: str,
        duration_ms: int,
        detail: str | None = None,
    ) -> None:
        """Append one line to ``data/debug/events.jsonl``. Never contains secrets."""
        if not self.enabled:
            return
        self._ensure_dirs()
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "step": step,
            "url_before": sanitize_url(url_before),
            "url_after": sanitize_url(url_after),
            "selector_name": selector_name,
            "result": result,
            "duration_ms": duration_ms,
        }
        if detail:
            entry["detail"] = sanitize(detail)
        with (self.debug_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------- authentication ------

    @property
    def auth_dir(self) -> Path:
        return self.debug_dir / "auth"

    async def capture_post_submit(
        self, page: Page, observer: AuthObserver, *, outcome: str
    ) -> AuthArtifacts:
        """Everything known about a post-submit outcome, in one timestamped set.

        Written unconditionally — an authentication that ends somewhere
        unexplained is exactly when artifacts must not be optional. Each file
        is written independently so one failure does not cost the rest.
        """
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = self.auth_dir / f"post-submit-{stamp}"
        url = sanitize_url(page.url)
        header = {
            "captured_at": datetime.now(UTC).isoformat(),
            "outcome": outcome,
            "url": url,
        }

        screenshot: Path | None = None
        try:
            await page.screenshot(path=f"{base}.png", full_page=False)
            screenshot = Path(f"{base}.png")
        except Exception as exc:  # pragma: no cover - page may be closing
            logger.debug("Post-submit screenshot failed: %s", exc)

        elements: list[dict[str, Any]] = []
        try:
            elements = await collect_interactive_elements(page)
        except Exception as exc:  # pragma: no cover - page may be navigating
            logger.warning("Could not collect page elements: %s", exc)

        return AuthArtifacts(
            screenshot=screenshot,
            elements=self._write_json(
                Path(f"{base}-elements.json"),
                {**header, "element_count": len(elements), "elements": elements},
            ),
            # The filename the transient-text observer promises; it is the one
            # to send when the form comes back with no visible explanation.
            text=self._write_json(
                Path(f"{base}-text.json"),
                {**header, "states": [state.as_dict() for state in observer.texts]},
            ),
            console=self._write_json(
                Path(f"{base}-console.json"),
                {**header, "messages": [item.as_dict() for item in observer.console]},
            ),
            network=self._write_json(
                Path(f"{base}-network.json"),
                {
                    **header,
                    "note": (
                        "Safe metadata only: no bodies, headers, cookies or "
                        "query strings are recorded."
                    ),
                    "phases_entered": observer.phases_entered,
                    "responses_by_phase": dict(observer.phases()),
                    "failed": [item.as_dict() for item in observer.failed_responses()],
                    "responses": [item.as_dict() for item in observer.responses],
                },
            ),
        )

    def write_artifact(self, stem: str, payload: dict[str, Any]) -> Path | None:
        """Save one timestamped, sanitized JSON report under ``data/debug/``.

        For diagnostics that are their own thing rather than part of a failure
        capture — the accessibility dump, and whatever comes after it.
        """
        self._ensure_dirs()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self._write_json(
            self.debug_dir / f"{self._slug(stem)}-{stamp}.json",
            {"captured_at": datetime.now(UTC).isoformat(), **payload},
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path | None:
        try:
            path.write_text(
                json.dumps(sanitize(payload), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover - disk full / permissions
            logger.warning("Could not write %s: %s", path.name, exc)
            return None
        return path

    # ------------------------------------------------------------- errors ---

    async def capture_failure(self, page: Page, step: str, error: BaseException) -> Path | None:
        """Screenshot + sanitized context for a failed action.

        Full HTML is intentionally not saved: it can contain personal data from
        the authenticated cabinet.
        """
        self.error_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = self.error_dir / f"{stamp}-{self._slug(step)}"
        try:
            await page.screenshot(path=f"{base}.png", full_page=False)
        except Exception as exc:  # pragma: no cover
            logger.debug("Error screenshot failed: %s", exc)

        try:
            elements = await collect_interactive_elements(page)
            url = page.url
        except Exception:  # pragma: no cover
            elements, url = [], "unknown"

        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "step": step,
            "url": sanitize_url(url),
            "error_type": type(error).__name__,
            "error": sanitize(str(error)),
            "elements": elements,
        }
        json_path = Path(f"{base}.json")
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.error("Failure artifacts saved: %s.png / %s", base, json_path.name)
        return json_path


def format_element(element: dict[str, Any]) -> str:
    """One-line rendering used by ``inspect`` and ambiguity diagnostics."""
    parts = [f"<{element.get('tag', '?')}>"]
    for key in ("role", "text", "aria_label", "placeholder", "name", "id", "data_testid"):
        value = element.get(key)
        if value:
            parts.append(f"{key}={value!r}")
    return " ".join(parts)
