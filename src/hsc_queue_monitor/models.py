"""Domain models, locator specifications and the exception hierarchy.

Nothing in this module talks to Playwright — it only describes *what* should be
located and *what* was found, so it can be unit-tested without a browser.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, time
from typing import Any, Literal

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class HscMonitorError(Exception):
    """Base class for every error this project raises deliberately."""


class ConfigError(HscMonitorError):
    """A YAML / environment configuration file is missing or malformed."""


class SelectorNotConfigured(HscMonitorError):
    """A selector is still a TODO placeholder and cannot be executed."""

    def __init__(self, key: str, detail: str = "") -> None:
        message = f"{key} has not been configured."
        if detail:
            message = f"{message} {detail}"
        # login.* selectors live on screens you can only reach while signed out,
        # so plain `inspect` (which starts in the cabinet) is the wrong tool.
        discover = "inspect-auth" if key.startswith("login.") else "inspect"
        message += (
            f"\nDiscover it with:  python -m hsc_queue_monitor.cli {discover}"
            f"\nThen set it in config/selectors.yaml and verify with:"
            f"\n  python -m hsc_queue_monitor.cli test-step {key}"
        )
        super().__init__(message)
        self.key = key


class LocatorNotFound(HscMonitorError):
    """A configured selector matched zero elements."""

    def __init__(self, key: str, description: str) -> None:
        super().__init__(f"{key} matched 0 visible elements.\nLocator: {description}")
        self.key = key


class LocatorAmbiguous(HscMonitorError):
    """A selector expected to be unique matched several visible elements."""

    def __init__(self, key: str, candidates: list[str]) -> None:
        listing = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(candidates))
        super().__init__(
            f"{key} matched {len(candidates)} visible elements.\n"
            f"Candidates:\n{listing}\n"
            "Make the selector more specific, or add an explicit `nth:` to the "
            "entry in config/selectors.yaml if the ambiguity is expected."
        )
        self.key = key
        self.candidates = candidates


class ChallengeDetected(HscMonitorError):
    """A CAPTCHA / anti-bot interstitial requires a human."""


class AuthenticationFailed(HscMonitorError):
    """The authenticated cabinet could not be reached.

    Raised when the ID.GOV.UA journey could not be completed, or when it
    completed but ``/cabinet`` still does not show the authenticated marker.
    Recovery is attempted exactly once per operation — this error is the end of
    that single attempt, never the start of a retry loop.
    """


class AuthenticationProcessingTimeout(AuthenticationFailed):
    """ID.GOV.UA was still reading the key when the timeout expired.

    Distinct from a rejection on purpose: the site never finished, so nothing
    at all is known about whether the key would have been accepted.
    """


class AuthIntermediateScreenReached(AuthenticationFailed):
    """ID.GOV.UA moved past the key form to a screen we have never seen.

    A stop, not a crash: the journey got further than the key form but landed
    somewhere this project has no selectors for. Nothing on that screen is
    clicked — the artifacts describe it so the step can be added deliberately.

    «Перевірте дані» used to arrive here and no longer does: it has a step of
    its own now. This stays for the screen that has not been met yet.
    """


class SignatureExtensionUnavailable(HscMonitorError):
    """ID.GOV.UA reports that its web-signature component is not available.

    Nothing about the requirement is emulated or bypassed: the component has to
    be installed and enabled in the persistent browser profile by hand.
    """

    DEFAULT_MESSAGE = (
        "ID.GOV.UA requires its web-signature component, but it was not detected\n"
        "in the Playwright browser profile.\n\n"
        "Install/enable the official signing component in the persistent browser\n"
        "profile and retry."
    )

    def __init__(self, detail: str = "") -> None:
        message = self.DEFAULT_MESSAGE
        if detail:
            message = f"{message}\n\nPage reported: {detail}"
        super().__init__(message)


class FlowError(HscMonitorError):
    """A flow step could not be completed."""


class ApiProbeError(HscMonitorError):
    """The direct-API diagnostic was asked to do something it must not do.

    Raised before anything is sent: an off-site URL (browser cookies never leave
    hsc.gov.ua) or a request method that is not GET (this diagnostic reads, and
    never books). It is a refusal, not a network failure.
    """


class AccessibilityUnavailable(HscMonitorError):
    """The macOS Accessibility API could not be used.

    Missing PyObjC, a refused permission, or an API call the system rejected —
    all cases where the native path cannot proceed and must say so rather than
    quietly doing something else.
    """


class NativeFileDialogError(HscMonitorError):
    """The operating system's file-open dialog could not be driven.

    Raised instead of falling back to an in-page upload: the native dialog is
    the mechanism ID.GOV.UA actually accepts, so quietly doing it another way
    would trade a clear failure here for an unexplained one later.
    """


class DepartmentNotFound(HscMonitorError):
    """No service centre on screen identifies itself as the requested one."""

    def __init__(self, service_center_id: str, candidates: list[str]) -> None:
        listing = (
            "\n".join(f"  - {c}" for c in candidates[:20])
            if candidates
            else "  (no service centre buttons matched the search term)"
        )
        super().__init__(
            f"Service centre {service_center_id!r} is not on this screen.\n"
            f"Buttons that matched the search term:\n{listing}\n"
            "Check that the ID in config/service_centers.yaml is the one the site "
            "shows, and that the search field really filtered the list."
        )
        self.service_center_id = service_center_id
        self.candidates = candidates


class DepartmentAmbiguous(HscMonitorError):
    """Several visible buttons identify themselves as the same service centre."""

    def __init__(self, service_center_id: str, candidates: list[str]) -> None:
        listing = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(candidates))
        super().__init__(
            f"Service centre {service_center_id!r} matched {len(candidates)} visible "
            f"buttons, so it is not safe to pick one:\n{listing}\n"
            "Nothing was clicked. Re-check the ID — a centre must be uniquely "
            "identifiable before it can be selected."
        )
        self.service_center_id = service_center_id
        self.candidates = candidates


class DepartmentUnavailable(HscMonitorError):
    """The service centre is on screen but its button is disabled."""

    def __init__(self, service_center_id: str, full_text: str = "") -> None:
        detail = f"\nButton text: {full_text}" if full_text else ""
        super().__init__(
            f"Service centre {service_center_id} is currently unavailable — the site "
            f"renders its button as disabled.{detail}\n"
            "A disabled button is never force-clicked."
        )
        self.service_center_id = service_center_id
        self.full_text = full_text


# --------------------------------------------------------------------------- #
# Locator specification
# --------------------------------------------------------------------------- #

Strategy = Literal["role", "text", "label", "placeholder", "css", "test_id"]

STRATEGIES: frozenset[str] = frozenset(
    {"role", "text", "label", "placeholder", "css", "test_id"}
)

#: Values that mean "a human still has to fill this in".
TODO_SENTINELS: frozenset[str] = frozenset({"TODO", "**TODO**", "todo", "?"})

#: Values that mean "supplied at runtime" (e.g. a service centre name).
DYNAMIC_SENTINELS: frozenset[str] = frozenset({"DYNAMIC", "**DYNAMIC**", "dynamic"})


def _is_sentinel(value: str | None, sentinels: frozenset[str]) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    if stripped in sentinels:
        return True
    # Catches TODO_SERVICE_CENTER_1 and friends.
    return stripped.upper().startswith("TODO")


@dataclass(frozen=True, slots=True)
class LocatorSpec:
    """A declarative, YAML-sourced description of one element.

    ``key`` is the dotted path in selectors.yaml (``login.password``).
    ``name`` is the *accessible name* used by ``strategy: role`` — it is
    deliberately not the same thing as ``key``.
    """

    key: str
    strategy: Strategy
    value: str | None = None
    role: str | None = None
    name: str | None = None
    exact: bool | None = None
    nth: int | None = None
    timeout: int | None = None
    visible: bool = True
    multiple: bool = False
    optional: bool = False

    # ---------------------------------------------------------------- state --

    @property
    def is_todo(self) -> bool:
        """True while the selector is still an unfilled placeholder."""
        if self.strategy == "role":
            return _is_sentinel(self.name, TODO_SENTINELS) or _is_sentinel(
                self.value, TODO_SENTINELS
            )
        return _is_sentinel(self.value, TODO_SENTINELS)

    @property
    def is_dynamic(self) -> bool:
        """True when the value must be supplied by the caller at runtime."""
        target = self.name if self.strategy == "role" else self.value
        return _is_sentinel(target, DYNAMIC_SENTINELS)

    @property
    def expects_unique(self) -> bool:
        """Whether matching more than one visible element is an error."""
        return not self.multiple and self.nth is None

    # ----------------------------------------------------------- derivation --

    def resolved(self, value: str | None = None, **params: str) -> LocatorSpec:
        """Return a copy with the dynamic value / format placeholders filled in.

        ``value`` replaces a DYNAMIC placeholder; ``params`` are applied with
        :meth:`str.format` to the value (or accessible name for role lookups),
        which lets a selector be written as ``"Категорія {category}"``.
        """
        targets_name = self.strategy == "role"
        current: str | None = self.name if targets_name else self.value

        if self.is_dynamic:
            if value is None:
                raise SelectorNotConfigured(
                    self.key,
                    "It is marked DYNAMIC, so a value must be supplied at runtime.",
                )
            current = value
        elif value is not None:
            current = value

        if params and current is not None:
            try:
                current = current.format(**params)
            except (KeyError, IndexError) as exc:  # pragma: no cover - misconfig
                raise ConfigError(
                    f"{self.key}: could not substitute {params!r} into {current!r}: {exc}"
                ) from exc

        return replace(self, name=current) if targets_name else replace(self, value=current)

    def describe(self) -> str:
        """A human-readable rendering of the Playwright call this maps to."""
        exact = "" if self.exact is None else f", exact={self.exact}"
        match self.strategy:
            case "role":
                base = f'get_by_role("{self.role}", name="{self.name}"{exact})'
            case "text":
                base = f'get_by_text("{self.value}"{exact})'
            case "label":
                base = f'get_by_label("{self.value}"{exact})'
            case "placeholder":
                base = f'get_by_placeholder("{self.value}"{exact})'
            case "test_id":
                base = f'get_by_test_id("{self.value}")'
            case _:
                base = f'locator("{self.value}")'
        if self.nth is not None:
            base += f".nth({self.nth})"
        return base

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.key} -> {self.describe()}"

    # ------------------------------------------------------------- parsing ---

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any]) -> LocatorSpec:
        if not isinstance(raw, Mapping):  # pragma: no cover - defensive
            raise ConfigError(f"{key}: expected a mapping, got {type(raw).__name__}")

        unknown = set(raw) - {
            "strategy",
            "value",
            "role",
            "name",
            "exact",
            "nth",
            "timeout",
            "visible",
            "multiple",
            "optional",
        }
        if unknown:
            raise ConfigError(f"{key}: unknown selector option(s): {sorted(unknown)}")

        strategy = raw.get("strategy")
        if strategy not in STRATEGIES:
            raise ConfigError(
                f"{key}: strategy must be one of {sorted(STRATEGIES)}, got {strategy!r}"
            )

        value = raw.get("value")
        role = raw.get("role")
        name = raw.get("name")

        if strategy == "role":
            if not role:
                raise ConfigError(f"{key}: strategy 'role' requires a `role:` key")
            if name is None:
                raise ConfigError(f"{key}: strategy 'role' requires a `name:` key")
        elif value is None:
            raise ConfigError(f"{key}: strategy {strategy!r} requires a `value:` key")

        nth = raw.get("nth")
        if nth is not None and (not isinstance(nth, int) or isinstance(nth, bool) or nth < 0):
            raise ConfigError(f"{key}: `nth` must be a non-negative integer, got {nth!r}")

        timeout = raw.get("timeout")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0
        ):
            raise ConfigError(f"{key}: `timeout` must be a positive integer (ms)")

        return cls(
            key=key,
            strategy=strategy,
            value=None if value is None else str(value),
            role=None if role is None else str(role),
            name=None if name is None else str(name),
            exact=_opt_bool(key, "exact", raw.get("exact")),
            nth=nth,
            timeout=timeout,
            visible=_opt_bool(key, "visible", raw.get("visible")) is not False,
            multiple=_opt_bool(key, "multiple", raw.get("multiple")) is True,
            optional=_opt_bool(key, "optional", raw.get("optional")) is True,
        )


def _opt_bool(key: str, field_name: str, value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: `{field_name}` must be true or false, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Domain
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """One bookable appointment observed in the UI.

    ``date`` is optional on purpose: the backend format is not known yet, and a
    slot whose date could not be parsed is still worth reporting.
    """

    service_center: str
    time: str
    date: date | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity used for de-duplication in the state file."""
        day = self.date.isoformat() if self.date else "unknown-date"
        return f"{self.service_center}|{day}|{self.time}"

    def human_date(self) -> str:
        return self.date.strftime("%d.%m.%Y") if self.date else "невідомо"


@dataclass(frozen=True, slots=True)
class ServiceCenter:
    """One watched centre.

    ``id`` is the identity — it is what the code matches on and what gets typed
    into the search box. ``name`` and ``full_name`` are display/diagnostic text:
    the site is free to reword the address without breaking anything.
    """

    name: str
    enabled: bool = True
    id: str = ""
    full_name: str = ""

    @property
    def search_term(self) -> str:
        """What to type into the service-centre search field."""
        return self.id or self.name

    def matches(self, text: str) -> bool:
        """True when *text* (a button label) names this centre."""
        return identifies_service_center(text, self.id) if self.id else self.name in text


#: ``3242`` must not match ``13242`` or ``32421``, but must match
#: ``ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20``.
def identifies_service_center(text: str, service_center_id: str) -> bool:
    """Whether a visible label identifies exactly ``service_center_id``.

    Deliberately independent of the address: only the ID has to stay stable.
    """
    if not service_center_id:
        return False
    pattern = rf"(?<!\w){re.escape(service_center_id)}(?!\w)"
    return re.search(pattern, text) is not None


@dataclass(frozen=True, slots=True)
class DepartmentAvailability:
    """What one service-centre button looked like on the selection screen.

    The disabled -> available interpretation lives in :meth:`from_button` alone,
    so it can be revised in one place once more UI states are observed.
    """

    service_center_id: str
    name: str
    full_text: str
    found: bool
    disabled: bool
    available: bool

    @classmethod
    def from_button(
        cls, *, service_center_id: str, name: str, full_text: str, disabled: bool
    ) -> DepartmentAvailability:
        """A centre that is on screen, and whether its button can be clicked.

        ``available`` describes the *button*, not an appointment. A centre whose
        card is enabled can still have no free date, and a free date can still
        have no free time — see :class:`CentreAvailability`, which is what
        answers "is there something to book". The card state is kept because it
        is a cheap first filter: a disabled card is never clicked.
        """
        return cls(
            service_center_id=service_center_id,
            name=name,
            full_text=full_text,
            found=True,
            disabled=disabled,
            available=not disabled,
        )

    @classmethod
    def missing(cls, *, service_center_id: str, name: str = "") -> DepartmentAvailability:
        """A centre that the search did not turn up at all."""
        return cls(
            service_center_id=service_center_id,
            name=name,
            full_text="",
            found=False,
            disabled=False,
            available=False,
        )


# --------------------------------------------------------------------------- #
# Availability scanning
# --------------------------------------------------------------------------- #
#
# What a scan produces, from the inside out:
#
#   TimeSlot            one selectable time on the "Час" step
#   DateAvailability    one enabled day, and the times it turned out to offer
#   CentreAvailability  one configured centre, and the days it turned out to offer
#
# None of these knows anything about Playwright: a scan result can be built in a
# test, printed, compared and stored without a browser anywhere near it.


@dataclass(frozen=True, slots=True)
class AvailableDate:
    """An enabled day button, dated by the month container it was found in.

    ``label`` is what the button actually said (normally just the day number).
    It is kept for diagnostics; nothing matches on it.
    """

    date: date
    label: str = ""


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """One selectable time. Observed only — the scanner never clicks these.

    ``time`` is when the appointment starts, and is what identifies the slot
    everywhere. ``end_time`` is optional because only one source reports it: the
    API returns a ``startTime``/``stopTime`` window, while the UI shows a single
    time on the button. A slot read from the browser therefore has no end, and
    that is a fact about the source rather than missing data.
    """

    time: time
    text: str
    #: Spelled ``datetime.time`` because the field above shadows the bare name.
    end_time: datetime.time | None = None

    @property
    def display(self) -> str:
        return self.time.strftime("%H:%M")

    @property
    def display_range(self) -> str:
        """``08:26-08:52`` when the source gave a window, ``08:26`` when it did not."""
        if self.end_time is None:
            return self.display
        return f"{self.display}-{self.end_time.strftime('%H:%M')}"


@dataclass(frozen=True, slots=True)
class DateAvailability:
    """One available date and every enabled time it offered.

    An empty ``slots`` is a normal, expected observation: the day button was
    enabled and the time step turned out to have nothing free. That is not an
    error and is never reported as one.
    """

    date: date
    slots: tuple[TimeSlot, ...] = ()
    #: Set only when scanning this date failed. Empty on the normal path.
    error: str = ""

    @property
    def has_slots(self) -> bool:
        return bool(self.slots)


@dataclass(frozen=True, slots=True)
class CentreAvailability:
    """What one configured centre offered, end to end.

    :attr:`bookable` is the project's definition of "worth telling someone
    about": at least one enabled date carrying at least one enabled time. An
    enabled centre card is explicitly *not* enough — it only decides whether the
    centre is worth opening at all.
    """

    centre_id: str
    centre_name: str
    found: bool = True
    card_enabled: bool = True
    dates: tuple[DateAvailability, ...] = ()
    #: Set only when scanning this centre failed. Empty on the normal path.
    error: str = ""

    @property
    def bookable(self) -> bool:
        """The availability rule. Not the card state — a real free time."""
        return any(day.has_slots for day in self.dates)

    @property
    def slot_count(self) -> int:
        return sum(len(day.slots) for day in self.dates)

    @property
    def status(self) -> str:
        """Why there is nothing to book, in one word, for the report."""
        if self.error:
            return "error"
        if not self.found:
            return "not-found"
        if not self.card_enabled:
            return "centre-unavailable"
        if not self.dates:
            return "no-dates"
        if not self.bookable:
            return "no-times"
        return "bookable"

    @classmethod
    def missing(cls, *, centre_id: str, centre_name: str) -> CentreAvailability:
        """The centre was not on the service-centre screen at all."""
        return cls(
            centre_id=centre_id, centre_name=centre_name, found=False, card_enabled=False
        )

    @classmethod
    def unavailable(cls, *, centre_id: str, centre_name: str) -> CentreAvailability:
        """The centre is there, but its card is disabled — so it is not opened."""
        return cls(
            centre_id=centre_id, centre_name=centre_name, found=True, card_enabled=False
        )
