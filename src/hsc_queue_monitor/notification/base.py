"""Notifier interface and the message model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import AvailableSlot


@dataclass(frozen=True, slots=True)
class Notification:
    """A ready-to-send message. Carries no authentication information."""

    service_center: str
    slots: tuple[AvailableSlot, ...]
    exam: str = "Practical"
    category: str = "A"

    def render(self) -> str:
        lines = [
            "🏍 HSC appointment available",
            "",
            f"Exam: {self.exam}",
            f"Category: {self.category}",
            f"Service center: {self.service_center}",
        ]
        if len(self.slots) == 1:
            slot = self.slots[0]
            lines += [f"Date: {slot.human_date()}", f"Time: {slot.time}"]
        else:
            lines.append(f"Slots: {len(self.slots)}")
            for slot in self.slots[:20]:
                lines.append(f"  • {slot.human_date()} {slot.time}")
            if len(self.slots) > 20:
                lines.append(f"  … and {len(self.slots) - 20} more")
        return "\n".join(lines)


class Notifier(ABC):
    """Anything that can deliver a :class:`Notification`."""

    name = "notifier"

    @abstractmethod
    async def send(self, notification: Notification) -> None: ...

    async def close(self) -> None:
        """Release resources. Default: nothing to do."""
        return None
