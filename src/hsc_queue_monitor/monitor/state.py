"""Which slots have already been reported.

The state file holds slot identities and timestamps only — never cookies,
tokens, MasterKey information or passwords.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..models import AvailableSlot

logger = logging.getLogger(__name__)

STATE_VERSION = 1

#: Forget slot keys nobody has seen for this long, so the file cannot grow forever.
RETENTION = timedelta(days=30)


class StateStore:
    """Tracks ``slot key -> last notification time``."""

    def __init__(self, path: Path, *, cooldown_seconds: int = 6 * 3600) -> None:
        self.path = path
        self.cooldown = timedelta(seconds=max(cooldown_seconds, 0))
        self._seen: dict[str, datetime] = {}

    # -------------------------------------------------------------- io ------

    def load(self) -> StateStore:
        if not self.path.exists():
            logger.debug("No state file at %s; starting fresh", self.path)
            return self

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s); starting fresh", self.path, exc)
            return self

        entries = raw.get("seen_slots", {}) if isinstance(raw, dict) else {}
        now = datetime.now(UTC)

        # A bare list is accepted so a hand-written state file still loads.
        if isinstance(entries, list):
            self._seen = {str(key): now for key in entries}
        elif isinstance(entries, dict):
            for key, stamp in entries.items():
                parsed = _parse_timestamp(stamp)
                if parsed is not None:
                    self._seen[str(key)] = parsed
        logger.debug("Loaded %d known slot(s) from %s", len(self._seen), self.path)
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.now(UTC).isoformat(),
            "seen_slots": {key: ts.isoformat() for key, ts in sorted(self._seen.items())},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ queries ---

    def is_new(self, slot: AvailableSlot, *, now: datetime | None = None) -> bool:
        """True when the slot has never been reported, or the cooldown expired."""
        now = now or datetime.now(UTC)
        last = self._seen.get(slot.key)
        if last is None:
            return True
        return (now - last) >= self.cooldown

    def select_new(
        self, slots: list[AvailableSlot], *, now: datetime | None = None
    ) -> list[AvailableSlot]:
        now = now or datetime.now(UTC)
        fresh: list[AvailableSlot] = []
        seen_keys: set[str] = set()
        for slot in slots:
            if slot.key in seen_keys:
                continue
            seen_keys.add(slot.key)
            if self.is_new(slot, now=now):
                fresh.append(slot)
        return fresh

    def mark_notified(
        self, slots: list[AvailableSlot], *, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        for slot in slots:
            self._seen[slot.key] = now

    def prune(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        stale = [key for key, ts in self._seen.items() if now - ts > RETENTION]
        for key in stale:
            del self._seen[key]
        return len(stale)

    @property
    def known_keys(self) -> set[str]:
        return set(self._seen)

    def __len__(self) -> int:
        return len(self._seen)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
