"""Data models and tolerant parsers for HSC queue payloads.

The upstream API is undocumented and its field names are not stable, so every
parser here is defensive: unknown shapes degrade to ``None`` instead of raising,
and the untouched payload is kept in ``raw`` for later inspection.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path
from typing import Any

JsonValue = Any
JsonDict = dict[str, Any]

#: Keys that different HSC payload versions have used for the same concept.
_ID_KEYS = ("id", "departmentId", "department_id", "code", "value")
_NAME_KEYS = ("name", "title", "fullName", "shortName", "label", "text", "description")
_REGION_KEYS = ("region", "regionName", "area", "oblast", "regionTitle")
_CITY_KEYS = ("city", "cityName", "settlement", "locality", "town")
_STREET_KEYS = ("street", "streetName", "address", "addressLine")
_BUILDING_KEYS = ("building", "house", "houseNumber", "buildingNumber")
_OFFICE_KEYS = ("office", "room", "officeNumber", "cabinet")
_ALLOW_ONLINE_KEYS = ("allowOnlineCount", "allow_online_count", "onlineCount", "allowOnline")
_DATE_KEYS = ("date", "day", "dateStr", "scheduleDate", "value")
_TIME_KEYS = ("time", "hour", "timeStr", "startTime", "start", "value")
_AVAILABLE_KEYS = ("available", "isAvailable", "free", "isFree", "enabled", "active")
_COUNT_KEYS = ("count", "freeCount", "availableCount", "slots", "quantity", "allowOnlineCount")

#: Wrapper keys seen around list payloads (``{"data": [...]}`` and friends).
_CONTAINER_KEYS = ("data", "items", "results", "departments", "list", "content", "payload")


def _first(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "available", "free"}:
            return True
        if lowered in {"false", "no", "n", "0", "unavailable", "busy"}:
            return False
    return None


def unwrap_list(payload: JsonValue) -> list[JsonDict]:
    """Return the list of dict records hidden inside a JSON payload.

    Accepts a bare list, ``{"data": [...]}``, ``{"data": {"items": [...]}}`` and
    similar wrappers. Anything else yields an empty list.
    """
    seen = 0
    current = payload
    while seen < 5:
        if isinstance(current, list):
            return [item for item in current if isinstance(item, dict)]
        if isinstance(current, dict):
            for key in _CONTAINER_KEYS:
                if key in current:
                    current = current[key]
                    break
            else:
                return []
            seen += 1
            continue
        return []
    return []


def normalize_date(value: Any) -> str | None:
    """Normalise a date-ish value to ``YYYY-MM-DD`` when possible."""
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = _as_str(value)
    if text is None:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def normalize_time(value: Any) -> str | None:
    """Normalise a time-ish value to ``HH:MM`` when possible."""
    text = _as_str(value)
    if text is None:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).strftime("%H:%M")
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M", "%H.%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return text


@dataclass(frozen=True, slots=True)
class Department:
    """A single HSC service centre."""

    id: int | None = None
    name: str | None = None
    region: str | None = None
    city: str | None = None
    street: str | None = None
    building: str | None = None
    office: str | None = None
    allow_online_count: int | None = None
    raw: JsonDict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> Department:
        if not isinstance(payload, Mapping):  # pragma: no cover - defensive
            return cls()
        return cls(
            id=_as_int(_first(payload, _ID_KEYS)),
            name=_as_str(_first(payload, _NAME_KEYS)),
            region=_as_str(_first(payload, _REGION_KEYS)),
            city=_as_str(_first(payload, _CITY_KEYS)),
            street=_as_str(_first(payload, _STREET_KEYS)),
            building=_as_str(_first(payload, _BUILDING_KEYS)),
            office=_as_str(_first(payload, _OFFICE_KEYS)),
            allow_online_count=_as_int(_first(payload, _ALLOW_ONLINE_KEYS)),
            raw=dict(payload),
        )

    @property
    def address(self) -> str:
        parts = [p for p in (self.region, self.city, self.street, self.building) if p]
        if self.office:
            parts.append(f"каб. {self.office}")
        return ", ".join(parts)

    @property
    def label(self) -> str:
        name = self.name or self.address or "unknown department"
        return f"[{self.id}] {name}" if self.id is not None else name

    def describe(self) -> str:
        line = self.label
        address = self.address
        if address and address not in line:
            line = f"{line} — {address}"
        if self.allow_online_count is not None:
            line = f"{line} (allowOnlineCount={self.allow_online_count})"
        return line


def parse_departments(payload: JsonValue) -> list[Department]:
    """Parse a departments response into models, skipping unusable records."""
    records = unwrap_list(payload)
    departments = [Department.from_api(record) for record in records]
    return [d for d in departments if d.id is not None or d.name is not None]


@dataclass(frozen=True, slots=True)
class AvailableDate:
    """A calendar day reported by the queue for a department."""

    department_id: int | None
    date: str
    available: bool = False
    service_id: int | None = None
    free_count: int | None = None
    metadata: JsonDict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.service_id}|{self.department_id}|{self.date}"

    @classmethod
    def from_api(
        cls,
        payload: Mapping[str, Any],
        *,
        department_id: int | None = None,
        service_id: int | None = None,
    ) -> AvailableDate | None:
        date_value = normalize_date(_first(payload, _DATE_KEYS))
        if date_value is None:
            return None
        count = _as_int(_first(payload, _COUNT_KEYS))
        available = _as_bool(_first(payload, _AVAILABLE_KEYS))
        if available is None:
            available = bool(count) if count is not None else False
        return cls(
            department_id=_as_int(_first(payload, ("departmentId", "department_id")))
            or department_id,
            date=date_value,
            available=available,
            service_id=service_id,
            free_count=count,
            metadata=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """A concrete bookable time inside a day."""

    department_id: int | None
    date: str
    time: str
    available: bool = True
    service_id: int | None = None
    metadata: JsonDict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.service_id}|{self.department_id}|{self.date}|{self.time}"

    @classmethod
    def from_api(
        cls,
        payload: Mapping[str, Any],
        *,
        department_id: int | None = None,
        service_id: int | None = None,
        date: str | None = None,
    ) -> AvailableSlot | None:
        time_value = normalize_time(_first(payload, _TIME_KEYS))
        if time_value is None:
            return None
        date_value = normalize_date(_first(payload, _DATE_KEYS)) or date
        if date_value is None:
            return None
        available = _as_bool(_first(payload, _AVAILABLE_KEYS))
        if available is None:
            count = _as_int(_first(payload, _COUNT_KEYS))
            available = bool(count) if count is not None else True
        return cls(
            department_id=_as_int(_first(payload, ("departmentId", "department_id")))
            or department_id,
            date=date_value,
            time=time_value,
            available=available,
            service_id=service_id,
            metadata=dict(payload),
        )


@dataclass(slots=True)
class MonitorState:
    """Non-sensitive monitoring state persisted between runs.

    Only availability bookkeeping lives here. Cookies, tokens and any other
    session material stay inside the Playwright profile, never in this file.
    """

    version: int = 1
    seen_slots: set[str] = field(default_factory=set)
    date_availability: dict[str, bool] = field(default_factory=dict)
    last_check_at: str | None = None
    path: Path | None = field(default=None, repr=False, compare=False)

    # -- change detection ---------------------------------------------------
    def new_slots(self, slots: Iterable[AvailableSlot]) -> list[AvailableSlot]:
        """Available slots that were not seen before (deduplicated by key)."""
        fresh: list[AvailableSlot] = []
        batch: set[str] = set()
        for slot in slots:
            if not slot.available:
                continue
            if slot.key in self.seen_slots or slot.key in batch:
                continue
            batch.add(slot.key)
            fresh.append(slot)
        return fresh

    def newly_available_dates(self, dates: Iterable[AvailableDate]) -> list[AvailableDate]:
        """Dates that flipped from unknown/unavailable to available."""
        fresh: list[AvailableDate] = []
        batch: set[str] = set()
        for item in dates:
            if not item.available or item.key in batch:
                continue
            if self.date_availability.get(item.key, False):
                continue
            batch.add(item.key)
            fresh.append(item)
        return fresh

    def mark_slots_seen(self, slots: Iterable[AvailableSlot]) -> None:
        for slot in slots:
            if slot.available:
                self.seen_slots.add(slot.key)

    def mark_dates_seen(self, dates: Iterable[AvailableDate]) -> None:
        for item in dates:
            self.date_availability[item.key] = item.available

    def prune(self, today: date_cls) -> int:
        """Drop bookkeeping for days already in the past. Returns removals."""
        cutoff = today.isoformat()

        def _date_part(key: str, index: int) -> str | None:
            parts = key.split("|")
            return parts[index] if len(parts) > index else None

        stale_slots = {k for k in self.seen_slots if (d := _date_part(k, 2)) and d < cutoff}
        stale_dates = {k for k in self.date_availability if (d := _date_part(k, 2)) and d < cutoff}
        self.seen_slots -= stale_slots
        for key in stale_dates:
            del self.date_availability[key]
        return len(stale_slots) + len(stale_dates)

    def touch(self) -> None:
        self.last_check_at = datetime.now(UTC).isoformat(timespec="seconds")

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> JsonDict:
        return {
            "version": self.version,
            "last_check_at": self.last_check_at,
            "seen_slots": sorted(self.seen_slots),
            "date_availability": dict(sorted(self.date_availability.items())),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, path: Path | None = None) -> MonitorState:
        seen = payload.get("seen_slots") or []
        availability = payload.get("date_availability") or {}
        return cls(
            version=_as_int(payload.get("version")) or 1,
            seen_slots={str(item) for item in seen if isinstance(item, str)},
            date_availability={
                str(k): bool(v) for k, v in availability.items() if isinstance(k, str)
            },
            last_check_at=_as_str(payload.get("last_check_at")),
            path=path,
        )

    @classmethod
    def load(cls, path: Path) -> MonitorState:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path=path)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return cls(path=path)
        if not isinstance(payload, dict):
            return cls(path=path)
        return cls.from_dict(payload, path=path)

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("MonitorState has no path to save to")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        self.path = target
