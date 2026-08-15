"""What the messages say, in Ukrainian.

Plain text on purpose. MarkdownV2 would mean escaping every ``.``, ``-`` and
``(`` in a service-centre name, and a formatting bug that silently drops a
message is a worse outcome than unstyled text.

Ordering is centre, then date, then start time — the same order the rest of the
project reports in, so two runs of the same change read identically.

Chunking splits on centre, date and slot-line boundaries only. A time range is
never cut in half, every chunk keeps its heading, and nothing is dropped: a
change that is too long to send is still a change somebody needs to see.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from itertools import groupby
from typing import Final

from ..api.availability_snapshot import AvailabilityDiff, AvailableSlot, sort_key
from .telegram import MAX_TELEGRAM_TEXT

#: Headings. The emoji carry the meaning at a glance on a phone.
ADDED_HEADING: Final = "🟢 З'явилися нові слоти"
REMOVED_HEADING: Final = "🔴 Слоти більше недоступні"
BOTH_HEADING: Final = "🔄 Зміни доступності"
ADDED_SECTION: Final = "🟢 Нові слоти"
REMOVED_SECTION: Final = "🔴 Більше недоступні"

#: ASCII plus for additions, a real minus sign for removals, an en dash inside a
#: range. Distinct shapes, so a glance is enough.
ADDED_MARK: Final = "+"
REMOVED_MARK: Final = "−"
RANGE_DASH: Final = "–"

AUTH_REQUIRED: Final = "\n".join(
    [
        "🔐 Потрібна повторна авторизація",
        "",
        "Сесія HSC більше не дійсна.",
        "Моніторинг призупинено.",
        "",
        "Запусти локально:",
        "",
        "python -m hsc_queue_monitor.cli refresh-session",
        "",
        "Після успішної авторизації моніторинг відновиться автоматично.",
    ]
)

RATE_LIMITED: Final = "\n".join(
    [
        "🟠 HSC тимчасово обмежив запити",
        "",
        "Моніторинг отримав HTTP 429 і тимчасово призупинив запити.",
        "",
        "Моніторинг відновиться автоматично.",
    ]
)

SERVICE_UNAVAILABLE: Final = "\n".join(
    [
        "🟡 Сервіс HSC тимчасово недоступний",
        "",
        "Не вдалося виконати перевірку доступності запису.",
        "",
        "Моніторинг спробує знову автоматично під час наступного запуску.",
    ]
)

UNEXPECTED_ERROR: Final = "\n".join(
    [
        "🔴 Помилка моніторингу HSC",
        "",
        "Під час перевірки сталася неочікувана помилка.",
        "",
        "Монітор спробує знову автоматично під час наступного запуску.",
        "",
        "Перевір GitHub Actions, якщо проблема повторюється.",
    ]
)

PERSISTENCE_ERROR: Final = "\n".join(
    [
        "🔴 Помилка моніторингу HSC",
        "",
        "Монітор не зміг зберегти свій стан.",
        "",
        "Автоматичний моніторинг може працювати некоректно, доки проблема не буде усунена.",
        "",
        "Перевір GitHub Actions та MongoDB.",
    ]
)

#: Reasons safe to show a person: a status line, and nothing about the session.
_SAFE_REASON_PREFIX: Final = "HTTP "


#: Every centre line starts with this, which is how chunking recognises one.
_CENTRE_PREFIX: Final = "ТСЦ МВС №"


def centre_title(centre: str) -> str:
    return f"{_CENTRE_PREFIX}{centre}"


def format_date(day: date) -> str:
    """``26.08.2026`` — the format the site and the country both use."""
    return day.strftime("%d.%m.%Y")


def format_slot(slot: AvailableSlot) -> str:
    """``09:18–09:44``, or ``09:18`` when no end was reported."""
    start = slot.start_time.strftime("%H:%M")
    if slot.end_time is None:
        return start
    return f"{start}{RANGE_DASH}{slot.end_time.strftime('%H:%M')}"


def _by_centre(slots: Iterable[AvailableSlot]) -> list[tuple[str, list[AvailableSlot]]]:
    ordered = sorted(slots, key=sort_key)
    return [(centre, list(group)) for centre, group in groupby(ordered, lambda s: s.centre)]


def _date_lines(slots: Sequence[AvailableSlot], mark: str) -> list[str]:
    lines: list[str] = []
    for day, group in groupby(slots, lambda s: s.date):
        lines.append(f"📅 {format_date(day)}")
        lines += [f"{mark} {format_slot(slot)}" for slot in group]
    return lines


def render_availability(diff: AvailabilityDiff) -> list[str]:
    """The message(s) for one availability change. Empty when there is none.

    A list because a large change may not fit in one Telegram message — never
    because there is more than one thing to say. Additions and removals from the
    same scan are one notification, not two.
    """
    if not diff.changed:
        return []

    if diff.added and not diff.removed:
        return _chunked(ADDED_HEADING, _blocks(diff.added, ADDED_MARK))
    if diff.removed and not diff.added:
        return _chunked(REMOVED_HEADING, _blocks(diff.removed, REMOVED_MARK))
    return _chunked(BOTH_HEADING, _combined_blocks(diff))


def _blocks(slots: Sequence[AvailableSlot], mark: str) -> list[list[str]]:
    """One block per centre: its name, then its dates and times."""
    return [
        [centre_title(centre), *_date_lines(group, mark)]
        for centre, group in _by_centre(slots)
    ]


def _combined_blocks(diff: AvailabilityDiff) -> list[list[str]]:
    """One block per centre, with whichever of the two sections it needs."""
    added = dict(_by_centre(diff.added))
    removed = dict(_by_centre(diff.removed))

    blocks: list[list[str]] = []
    for centre in sorted(set(added) | set(removed), key=_centre_order):
        block = [centre_title(centre), ""]
        if centre in added:
            block += [ADDED_SECTION, *_date_lines(added[centre], ADDED_MARK)]
            if centre in removed:
                block.append("")
        if centre in removed:
            block += [REMOVED_SECTION, *_date_lines(removed[centre], REMOVED_MARK)]
        blocks.append(block)
    return blocks


def _centre_order(centre: str) -> tuple[int, str]:
    return (int(centre), "") if centre.isdigit() else (10**9, centre)


def render_auth_required(reason: str = "") -> list[str]:
    """The message for a session that needs a human.

    The reason is appended only when it is a plain status line. Anything else —
    a database error, a stack of detail, whatever a future caller passes — is
    dropped rather than shown: this message goes to a phone, and nothing about
    the session belongs on it.
    """
    if reason.startswith(_SAFE_REASON_PREFIX):
        return [f"{AUTH_REQUIRED}\n\nПричина: {reason}"]
    return [AUTH_REQUIRED]


def render_rate_limited(reason: str = "") -> list[str]:
    """The message for rate limiting."""
    if reason.startswith(_SAFE_REASON_PREFIX):
        return [f"{RATE_LIMITED}\n\nПричина: {reason}"]
    return [RATE_LIMITED]


def render_service_unavailable(reason: str = "") -> list[str]:
    """The message for service unavailability."""
    if reason.startswith(_SAFE_REASON_PREFIX):
        return [f"{SERVICE_UNAVAILABLE}\n\nПричина: {reason}"]
    return [SERVICE_UNAVAILABLE]


def render_unexpected_error() -> list[str]:
    """The message for an unexpected runtime error."""
    return [UNEXPECTED_ERROR]


def render_persistence_error() -> list[str]:
    """The message for a persistence/database error."""
    return [PERSISTENCE_ERROR]


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def _chunked(heading: str, blocks: Sequence[Sequence[str]]) -> list[str]:
    """Join blocks under a heading, splitting only between whole lines.

    Every line is a centre name, a section heading, a date or one slot — and a
    slot line is atomic, so a break between any two lines is safe. What makes a
    chunk readable on its own is the context: whichever centre, section and date
    were in force is re-emitted at the top of the next chunk, so no piece
    arrives saying ``+ 09:18`` with nothing to attach it to.
    """
    messages: list[str] = []
    current: list[str] = []
    centre = section = day = ""

    def flush() -> None:
        if current:
            messages.append("\n".join([heading, "", *current]).rstrip())
            current.clear()

    def context() -> list[str]:
        """What a fresh chunk needs before it can carry on."""
        lines = [line for line in (centre, section, day) if line]
        if centre and section:
            lines.insert(1, "")  # the combined layout keeps its blank line
        return lines

    def fits(addition: Sequence[str]) -> bool:
        return len("\n".join([heading, "", *current, *addition])) <= MAX_TELEGRAM_TEXT

    for index, block in enumerate(blocks):
        separator = [""] if index and current else []
        for line in block:
            if line.startswith(_CENTRE_PREFIX):
                centre, section, day = line, "", ""
            elif line in (ADDED_SECTION, REMOVED_SECTION):
                section, day = line, ""
            elif line.startswith("📅"):
                day = line

            addition = [*separator, line]
            if current and not fits(addition):
                flush()
                current.extend(context())
                # The context may already end with this very line.
                addition = [] if current[-1:] == [line] else [line]
            current.extend(addition)
            separator = []

    flush()
    return messages
