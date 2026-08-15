"""Everything this project reads *out of* UI text, as pure functions.

Kept apart from the page objects on purpose: parsing a Ukrainian month caption
has nothing to do with Playwright, so it is unit-tested directly and reused by
whichever screen happens to need it.
"""

from __future__ import annotations

import re
from datetime import date, time

#: Ukrainian month names -> month number. **The one place** this mapping lives.
#:
#: Both grammatical forms the site uses are here, because both are read: the
#: calendar captions are nominative («Серпень 2026») while free text is genitive
#: («20 серпня 2026»), and both have to answer 8.
MONTH_NUMBERS: dict[str, int] = {
    "січень": 1, "січня": 1,
    "лютий": 2, "лютого": 2,
    "березень": 3, "березня": 3,
    "квітень": 4, "квітня": 4,
    "травень": 5, "травня": 5,
    "червень": 6, "червня": 6,
    "липень": 7, "липня": 7,
    "серпень": 8, "серпня": 8,
    "вересень": 9, "вересня": 9,
    "жовтень": 10, "жовтня": 10,
    "листопад": 11, "листопада": 11,
    "грудень": 12, "грудня": 12,
}

_MONTH_ALTERNATION = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
#: A control whose whole label is a time — that is what a time slot looks like.
_EXACT_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
#: A day cell says the day number and nothing else.
_DAY_RE = re.compile(r"^(\d{1,2})$")

_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")  # 20.08.2026
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")  # 2026-08-20
_UK_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_ALTERNATION})(?:\s+(\d{{4}}))?\b",
                         re.IGNORECASE)
#: A calendar heading: month name then year, e.g. «Серпень 2026».
_MONTH_CAPTION_RE = re.compile(rf"\b({_MONTH_ALTERNATION})\s+(\d{{4}})\b", re.IGNORECASE)


def collapse(text: str | None) -> str:
    """Whitespace-normalised text, the way a person reads a label."""
    return " ".join((text or "").split())


def month_number(name: str) -> int | None:
    """Month number for a Ukrainian month name, in either form."""
    return MONTH_NUMBERS.get(name.strip().lower())


def parse_month_caption(text: str | None) -> tuple[int, int] | None:
    """``(year, month)`` from a calendar heading, or ``None``.

    Searches rather than matches: the caption is normally read off the whole
    month container, whose text also contains every day number in it.
    """
    match = _MONTH_CAPTION_RE.search(collapse(text))
    if match is None:
        return None
    number = month_number(match[1])
    return None if number is None else (int(match[2]), number)


def parse_day_number(text: str | None) -> int | None:
    """The day a day cell stands for, or ``None`` if it is not one.

    Deliberately strict: a cell whose label is anything but a bare number is not
    a day, and guessing which number in a longer label is the day is how a
    calendar starts booking the wrong thing.
    """
    match = _DAY_RE.match(collapse(text))
    if match is None:
        return None
    day = int(match[1])
    return day if 1 <= day <= 31 else None


def parse_date_text(text: str | None, *, today: date | None = None) -> date | None:
    """Extract a date from arbitrary UI text. Returns ``None`` when unclear."""
    if not text:
        return None

    if match := _ISO_DATE_RE.search(text):
        return safe_date(int(match[1]), int(match[2]), int(match[3]))

    if match := _NUMERIC_DATE_RE.search(text):
        return safe_date(int(match[3]), int(match[2]), int(match[1]))

    if match := _UK_DATE_RE.search(text):
        year = int(match[3]) if match[3] else (today or date.today()).year
        number = month_number(match[2])
        return None if number is None else safe_date(year, number, int(match[1]))

    return None


def safe_date(year: int, month: int, day: int) -> date | None:
    """A real date, or ``None`` — never a raised ValueError from parsing."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_times(text: str | None) -> list[str]:
    """Every ``HH:MM`` occurrence, normalised to zero-padded form."""
    if not text:
        return []
    return [f"{int(h):02d}:{m}" for h, m in _TIME_RE.findall(text)]


def parse_slot_time(text: str | None) -> time | None:
    """The time a slot control offers, or ``None`` if it is not a slot.

    Anchored, unlike :func:`parse_times`: a button labelled "Записатись на
    10:40" is a submit control, not a time slot, and must not be mistaken for
    one on a screen the scanner is forbidden to act on.
    """
    match = _EXACT_TIME_RE.match(collapse(text))
    if match is None:
        return None
    return time(int(match[1]), int(match[2]))
