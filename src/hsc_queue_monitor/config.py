"""Configuration loading: environment settings plus the three YAML files.

The whole point of this module is that *no* site-specific knowledge lives in
Python. Selectors come from ``config/selectors.yaml``, navigation order from
``config/flow.yaml`` and the watched centres from ``config/service_centers.yaml``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .api.probe import CONNECT_TIMEOUT, READ_TIMEOUT
from .api.retry import RetryConfig
from .models import ConfigError, LocatorSpec, SelectorNotConfigured, ServiceCenter

#: Never poll faster than this, whatever the .env says.
MIN_POLL_INTERVAL_SECONDS = 30

#: How the MasterKey file reaches ID.GOV.UA. See AuthenticationConfig.
FILE_SELECTION_MODES: frozenset[str] = frozenset({"native", "chooser"})


def _find_config_dir() -> Path:
    """Resolve config directory for both development and installed contexts.

    In development: config/ is at repository root (parents[2] from src/package).
    When installed: config/ must be provided explicitly or discovered from cwd.
    """
    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parent.parent
    candidate = repo_root / "config"

    # If we're in a development checkout, use repository config
    if candidate.is_dir():
        return candidate

    # If running from an installed package, try current directory
    cwd_config = Path.cwd() / "config"
    if cwd_config.is_dir():
        return cwd_config

    # Fallback: use repository root if it exists (for editable installs)
    if repo_root.name == "mreo-parser" or (repo_root.parent / "config").is_dir():
        return repo_root / "config"

    # Last resort: raise error with guidance
    raise ConfigError(
        f"Cannot locate config/ directory. "
        f"Tried: {candidate}, {cwd_config}. "
        f"When running an installed package outside the repository, "
        f"use: python -m hsc_queue_monitor.cli --config-dir /path/to/config monitor-once"
    )


def _find_data_dir() -> Path:
    """Resolve data directory (for debug artifacts, browser profile, state)."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidate = repo_root / "data"
    if candidate.is_dir() or candidate.parent.is_dir():
        return candidate
    return Path.cwd() / "data"


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = _find_config_dir()
DEFAULT_DATA_DIR = _find_data_dir()


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


#: The only values this project ever reads from the environment. Everything
#: else lives in version-controlled YAML, because everything else is ordinary
#: configuration and belongs in a diff.
SECRET_ENV_VARS: tuple[str, ...] = (
    "IDGOV_SIGNING_KEY_PATH",
    "IDGOV_SIGNING_KEY_PASSWORD",
    "HSC_MONGODB_URI",
    "HSC_SESSION_ENCRYPTION_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_USERS",
)

#: What is off-limits inside a committed file. A YAML that even mentions one of
#: these is a mistake worth stopping for, not a value to quietly ignore.
FORBIDDEN_CONFIG_KEYS: frozenset[str] = frozenset(
    name.lower() for name in SECRET_ENV_VARS
) | frozenset(
    {
        "key_path",
        "key_password",
        "mongodb_uri",
        "session_encryption_key",
        "telegram_bot_token",
        "telegram_users",
        "password",
        "token",
        "secret",
        "uri",
    }
)

REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True)
class SecretSettings:
    """The six sensitive values, and the only place they may come from.

    Two of them — the MasterKey path and its password — belong to the *local*
    runtime alone: they are what signs a person into ID.GOV.UA, and no CI
    runner is ever given them. The other four are shared with the headless
    monitor, which needs a database, a key to read it with and a bot to speak
    through.

    The path is treated as sensitive too: it names a home directory, and a home
    directory names a person. Only its basename is ever printed.
    """

    key_path: Path | None = None
    key_password: str = ""
    mongodb_uri: str = ""
    session_encryption_key: str = ""
    telegram_bot_token: str = ""
    telegram_users: tuple[int, ...] = ()

    def __repr__(self) -> str:
        """Never the values. This object ends up in tracebacks."""
        present = [name for name, value in self._by_name() if value]
        return f"SecretSettings(set={present or 'none'})"

    def _by_name(self) -> tuple[tuple[str, object], ...]:
        return (
            ("IDGOV_SIGNING_KEY_PATH", self.key_path),
            ("IDGOV_SIGNING_KEY_PASSWORD", self.key_password),
            ("HSC_MONGODB_URI", self.mongodb_uri),
            ("HSC_SESSION_ENCRYPTION_KEY", self.session_encryption_key),
            ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
            ("TELEGRAM_USERS", self.telegram_users),
        )

    def as_redacted_dict(self) -> dict[str, str]:
        """A diagnostic view: which secrets are set, never what they are."""
        return {name: (REDACTED if value else "") for name, value in self._by_name()}

    def redactable(self) -> list[str]:
        """Every value the log filter must scrub, including each recipient id.

        The recipient list is in here because a Telegram id identifies a person.
        Individual delivery lines still log a masked id — that is the whole
        point of masking — but the raw list never appears anywhere.
        """
        values = [
            self.key_password,
            self.mongodb_uri,
            self.session_encryption_key,
            self.telegram_bot_token,
        ]
        values += [str(user) for user in self.telegram_users]
        if self.key_path is not None:
            values.append(str(self.key_path))
        return [value for value in values if value]

    # ------------------------------------------------- per-command needs ----
    #
    # Deliberately not validated all at once at startup: a scheduled
    # `monitor-once` has no business failing because a MasterKey it will never
    # touch is not configured on the runner.

    @property
    def persistence_configured(self) -> bool:
        return bool(self.mongodb_uri and self.session_encryption_key)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_users)

    def require_key_path(self) -> Path:
        """Validate the electronic-signature signing key file. Local commands only."""
        if self.key_path is None:
            raise ConfigError(
                "IDGOV_SIGNING_KEY_PATH is not set. Copy .env.example to .env and point it at "
                "your file-based ID.GOV.UA signing key (keep the file outside this repository).\n"
                "This is a local-only secret: GitHub Actions never receives it."
            )
        if not self.key_path.exists():
            raise ConfigError(f"IDGOV_SIGNING_KEY_PATH does not exist: {self.key_path.name}")
        if not self.key_path.is_file():
            raise ConfigError(f"IDGOV_SIGNING_KEY_PATH is not a file: {self.key_path.name}")
        return self.key_path

    def require_key_password(self) -> str:
        if not self.key_password:
            raise ConfigError("IDGOV_SIGNING_KEY_PASSWORD is not set in .env")
        return self.key_password

    def require_persistence(self) -> tuple[str, str]:
        """The database and the key that opens it."""
        if not self.mongodb_uri:
            raise ConfigError(
                "HSC_MONGODB_URI is not set, so there is nowhere to read or write "
                "the HSC session.\nPut it in .env locally, and in the `production` "
                "GitHub Environment as a secret."
            )
        if not self.session_encryption_key:
            raise ConfigError(
                "HSC_MONGODB_URI is set but HSC_SESSION_ENCRYPTION_KEY is not.\n"
                "The stored session holds live authentication cookies and is only "
                "ever written encrypted. Generate a key with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"\n'
                "and put it in .env — never in the repository."
            )
        return self.mongodb_uri, self.session_encryption_key

    def require_telegram(self) -> None:
        """Refuse to run half-configured, in either direction."""
        if self.telegram_bot_token and not self.telegram_users:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is set but TELEGRAM_USERS is empty, so there "
                "is nobody to notify.\nSet TELEGRAM_USERS to the numeric Telegram "
                "ids that should receive alerts, comma separated. It is a secret: "
                "keep it in .env locally and in the `production` GitHub "
                "Environment.\nEach recipient must open the bot and press Start "
                "once first — a bot cannot start a conversation."
            )
        if self.telegram_users and not self.telegram_bot_token:
            raise ConfigError(
                "TELEGRAM_USERS is set but TELEGRAM_BOT_TOKEN is empty, so there "
                "is no bot to send with.\nCreate one with @BotFather and put the "
                "token in TELEGRAM_BOT_TOKEN — never in the repository."
            )


@dataclass(frozen=True, slots=True)
class Paths:
    """Where this run keeps its files. Not configuration anybody edits."""

    data_dir: Path = DEFAULT_DATA_DIR
    config_dir: Path = DEFAULT_CONFIG_DIR

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    @property
    def error_dir(self) -> Path:
        return self.debug_dir / "errors"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def events_path(self) -> Path:
        return self.debug_dir / "events.jsonl"

    @property
    def service_centers_path(self) -> Path:
        return self.config_dir / "service_centers.yaml"


def parse_telegram_users(raw: str, *, source: str = "TELEGRAM_USERS") -> tuple[int, ...]:
    """``"123, 456 ,789"`` -> ``(123, 456, 789)``.

    Whitespace is forgiven because a settings page will produce it. A value that
    is not a whole number is not: silently dropping one would mean silently not
    telling somebody. Duplicates are removed and first-seen order is kept, so
    the same list always fans out in the same order.

    Read from the environment only. These are personal identifiers, which is
    why they are a secret rather than a repository variable.
    """
    entries = [part.strip() for part in (raw or "").split(",")]
    seen: dict[int, None] = {}
    for entry in entries:
        if not entry:
            continue
        try:
            # `int()` accepts Unicode digits; a Telegram id is ASCII, and a
            # value that is not is a typo rather than a recipient.
            if not entry.isascii():
                raise ValueError(entry)
            seen.setdefault(int(entry), None)
        except ValueError:
            raise ConfigError(
                f"{source} must be numeric Telegram ids separated by commas, and "
                f"{entry!r} is not one.\nFor example:\n\n"
                "  TELEGRAM_USERS=123456789,987654321"
            ) from None
    return tuple(seen)


def load_secrets(env_file: Path | None = None) -> SecretSettings:
    """The six, from the environment. Nothing else is read from there."""
    load_dotenv(dotenv_path=env_file or (PROJECT_ROOT / ".env"), override=False)

    raw_key_path = os.getenv("IDGOV_SIGNING_KEY_PATH", "").strip()
    return SecretSettings(
        key_path=Path(raw_key_path).expanduser() if raw_key_path else None,
        key_password=os.getenv("IDGOV_SIGNING_KEY_PASSWORD", ""),
        mongodb_uri=os.getenv("HSC_MONGODB_URI", "").strip(),
        session_encryption_key=os.getenv("HSC_SESSION_ENCRYPTION_KEY", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_users=parse_telegram_users(os.getenv("TELEGRAM_USERS", "")),
    )


# --------------------------------------------------------------------------- #
# YAML helpers
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing configuration file: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


class SelectorRegistry:
    """All selectors, addressed by dotted path (``login.password``)."""

    def __init__(self, specs: Mapping[str, LocatorSpec]) -> None:
        self._specs = dict(specs)

    # ------------------------------------------------------------- loading --

    @classmethod
    def from_file(cls, path: Path) -> SelectorRegistry:
        return cls.from_dict(_read_yaml(path))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SelectorRegistry:
        specs: dict[str, LocatorSpec] = {}
        for section, entries in raw.items():
            if not isinstance(entries, Mapping):
                raise ConfigError(
                    f"selectors.yaml: section {section!r} must be a mapping of "
                    "selector name -> selector definition"
                )
            for name, definition in entries.items():
                key = f"{section}.{name}"
                specs[key] = LocatorSpec.from_dict(key, definition)
        return cls(specs)

    # ------------------------------------------------------------- lookup ----

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._specs))

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, key: str) -> LocatorSpec:
        """Return a spec, whatever its state. Raises only if the key is unknown."""
        try:
            return self._specs[key]
        except KeyError:
            raise ConfigError(
                f"Unknown selector {key!r}. Known selectors:\n  "
                + "\n  ".join(self)
            ) from None

    def require(self, key: str) -> LocatorSpec:
        """Return a usable spec, or raise :class:`SelectorNotConfigured`."""
        spec = self.get(key)
        if spec.is_todo:
            raise SelectorNotConfigured(key)
        return spec

    def optional(self, key: str) -> LocatorSpec | None:
        """Return the spec, or ``None`` when it is absent or still a TODO."""
        spec = self._specs.get(key)
        if spec is None or spec.is_todo:
            return None
        return spec

    def todo_keys(self) -> list[str]:
        return [key for key, spec in sorted(self._specs.items()) if spec.is_todo]

    def configured_keys(self) -> list[str]:
        return [key for key, spec in sorted(self._specs.items()) if not spec.is_todo]


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DebugConfig:
    pause_after_step: bool = False
    screenshots: bool = True
    dump_elements: bool = False


@dataclass(frozen=True, slots=True)
class Timeouts:
    default_locator: int = 15_000
    navigation: int = 30_000
    manual_challenge: int = 600_000
    #: How long the ID.GOV.UA callback may take to bring the browser back to
    #: HSC. Signing is slow, and the wait is a condition poll, not a sleep.
    authentication: int = 120_000


@dataclass(frozen=True, slots=True)
class AuthenticationConfig:
    """Values the ID.GOV.UA journey needs that are configuration, not secrets.

    ``key_provider`` is the electronic trust service provider (КНЕДП) that
    issued the MasterKey .dat file. ID.GOV.UA requires it to be chosen *before*
    the key is uploaded, because it decides how the key is interpreted. It is
    public information about the key, so it lives in flow.yaml rather than in
    ``.env`` — and it is a value, not a selector, so it never belongs in
    selectors.yaml either.
    """

    key_provider: str = ""
    #: How the .dat reaches ID.GOV.UA. ``native`` drives the operating
    #: system's own Open dialog, which is the only mechanism the site has been
    #: observed to accept; ``chooser`` uses Playwright's intercepted file
    #: chooser and is kept for A/B comparison only.
    file_selection: str = "native"
    #: The OS process whose file dialog is driven in ``native`` mode.
    browser_process: str = "Chromium"

    def require_key_provider(self) -> str:
        """The configured КНЕДП, or a ConfigError naming where to set it."""
        provider = self.key_provider.strip()
        if not provider:
            raise ConfigError(
                "authentication.key_provider is not set in config/flow.yaml.\n"
                "Automatic MasterKey authentication needs the electronic trust "
                "service provider (КНЕДП) that issued your key, because "
                "ID.GOV.UA requires it to be selected before the .dat file is "
                "uploaded. Set it like:\n\n"
                "  authentication:\n"
                '    key_provider: \'КНЕДП "MASTERKEY" ТОВ "АРТ-МАСТЕР"\'\n\n'
                "The value must match the option text shown in the provider "
                "dropdown exactly."
            )
        return provider

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AuthenticationConfig:
        if not isinstance(raw, Mapping):
            raise ConfigError("flow.yaml: top-level `authentication:` must be a mapping")

        unknown = set(raw) - {"key_provider", "file_selection", "browser_process"}
        if unknown:
            raise ConfigError(
                f"flow.yaml: authentication: unknown option(s) {sorted(unknown)}"
            )

        provider = raw.get("key_provider", "")
        if not isinstance(provider, str):
            raise ConfigError(
                "flow.yaml: authentication.key_provider must be a string "
                f"(the exact КНЕДП name), got {provider!r}"
            )

        selection = raw.get("file_selection", "native")
        if selection not in FILE_SELECTION_MODES:
            raise ConfigError(
                "flow.yaml: authentication.file_selection must be one of "
                f"{sorted(FILE_SELECTION_MODES)}, got {selection!r}"
            )

        process = raw.get("browser_process", "Chromium")
        if not isinstance(process, str) or not process.strip():
            raise ConfigError(
                "flow.yaml: authentication.browser_process must be the name of "
                f"the browser process, got {process!r}"
            )

        return cls(
            key_provider=provider,
            file_selection=selection,
            browser_process=process.strip(),
        )


#: Between two ``/slots`` requests for *different* dates. Measured: HSC answered
#: 429 to two such requests one second apart. This is pacing, not backoff — each
#: date is still requested exactly once, and a refusal is never retried.
DEFAULT_SLOT_INTERVAL_SECONDS = 2.0
#: Anything slower than this is a mistake, not a policy: a scan of a dozen dates
#: would take longer than the session it is running in.
MAX_SLOT_INTERVAL_SECONDS = 60.0


def _finite_seconds(value: Any, source: str) -> float:
    """The shared parsing half of every seconds-valued setting."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ConfigError(f"{source} must be a number of seconds, got {value!r}")
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ConfigError(f"{source} must be a number of seconds, got {value!r}") from exc
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        raise ConfigError(f"{source} must be a finite number of seconds, got {value!r}")
    return seconds


def validate_slot_interval(value: Any, source: str) -> float:
    """A usable seconds-between-slot-requests value, or a :class:`ConfigError`."""
    seconds = _finite_seconds(value, source)
    if seconds < 0:
        raise ConfigError(f"{source} cannot be negative, got {seconds}")
    if seconds > MAX_SLOT_INTERVAL_SECONDS:
        raise ConfigError(
            f"{source} is {seconds}s, and {MAX_SLOT_INTERVAL_SECONDS}s is the most "
            "that makes sense between two reads. Lower it, or scan fewer dates "
            "with --max-dates."
        )
    return seconds


#: Between the *start* of one ``api-monitor`` scan and the start of the next.
#: 300s against a measured 900s queue-session lifetime: frequent enough that the
#: scans themselves keep the session alive, so no keepalive request is needed.
DEFAULT_MONITOR_INTERVAL_SECONDS = 300.0
#: Below this, HSC's rate limiting is a near certainty — warned about, not
#: forbidden, because the operator can see the 429s as well as we can.
ADVISED_MIN_MONITOR_INTERVAL_SECONDS = 30.0
MAX_MONITOR_INTERVAL_SECONDS = 3600.0


def validate_monitor_interval(value: Any, source: str) -> float:
    """A usable seconds-between-scans value, or a :class:`ConfigError`."""
    seconds = _finite_seconds(value, source)
    if seconds <= 0:
        raise ConfigError(f"{source} must be greater than zero, got {seconds}")
    if seconds > MAX_MONITOR_INTERVAL_SECONDS:
        raise ConfigError(
            f"{source} is {seconds}s; {MAX_MONITOR_INTERVAL_SECONDS}s (one hour) is "
            "the most this monitor will schedule."
        )
    return seconds


#: The longest a single API read may take before it is given up on. There is no
#: retry behind this number: it is the whole budget for one request, so it is
#: raised when the server is measured to be slower than it, never doubled by a
#: second attempt.
MAX_TIMEOUT_SECONDS = 300.0


def validate_timeout(value: Any, source: str) -> float:
    """A usable HTTP timeout in seconds, or a :class:`ConfigError`."""
    seconds = _finite_seconds(value, source)
    if seconds <= 0:
        raise ConfigError(
            f"{source} must be greater than zero, got {seconds} — a request has "
            "to be allowed to take *some* time."
        )
    if seconds > MAX_TIMEOUT_SECONDS:
        raise ConfigError(
            f"{source} is {seconds}s; {MAX_TIMEOUT_SECONDS}s is the most this "
            "client will wait for one read."
        )
    return seconds


#: Bounds on the retry policy. Every one of them exists so a misconfiguration
#: cannot turn a scheduled read into a burst at a server that is already unwell.
MAX_RETRY_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 120.0
MAX_MULTIPLIER = 10.0


def _retry_from_dict(raw: Mapping[str, Any]) -> RetryConfig:
    """``api.retry:`` — validated on every axis, because none of them is free."""
    if not isinstance(raw, Mapping):
        raise ConfigError("app.yaml: `api.retry:` must be a mapping")

    known = {
        "max_attempts",
        "initial_backoff_seconds",
        "max_backoff_seconds",
        "multiplier",
        "max_retry_after_seconds",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"app.yaml: api.retry: unknown option(s) {sorted(unknown)}")

    default = RetryConfig()
    attempts = raw.get("max_attempts", default.max_attempts)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ConfigError(
            f"app.yaml: api.retry.max_attempts must be a whole number, got {attempts!r}"
        )
    if not 1 <= attempts <= MAX_RETRY_ATTEMPTS:
        raise ConfigError(
            f"app.yaml: api.retry.max_attempts must be between 1 and "
            f"{MAX_RETRY_ATTEMPTS}, got {attempts}. One means "
            "\"try once, never again\"; more than that starts to look like a "
            "burst rather than a retry."
        )

    initial = _positive_seconds(
        raw.get("initial_backoff_seconds", default.initial_backoff_seconds),
        "app.yaml: api.retry.initial_backoff_seconds",
        maximum=MAX_BACKOFF_SECONDS,
    )
    ceiling = _positive_seconds(
        raw.get("max_backoff_seconds", default.max_backoff_seconds),
        "app.yaml: api.retry.max_backoff_seconds",
        maximum=MAX_BACKOFF_SECONDS,
    )
    if ceiling < initial:
        raise ConfigError(
            "app.yaml: api.retry.max_backoff_seconds is below "
            "initial_backoff_seconds, so the first wait would already exceed the cap."
        )

    multiplier = _finite_seconds(
        raw.get("multiplier", default.multiplier), "app.yaml: api.retry.multiplier"
    )
    if not 1.0 <= multiplier <= MAX_MULTIPLIER:
        raise ConfigError(
            f"app.yaml: api.retry.multiplier must be between 1 and "
            f"{MAX_MULTIPLIER}, got {multiplier}. Below 1 the waits would shrink."
        )

    return RetryConfig(
        max_attempts=attempts,
        initial_backoff_seconds=initial,
        max_backoff_seconds=ceiling,
        multiplier=multiplier,
        max_retry_after_seconds=_positive_seconds(
            raw.get("max_retry_after_seconds", default.max_retry_after_seconds),
            "app.yaml: api.retry.max_retry_after_seconds",
            maximum=MAX_TIMEOUT_SECONDS,
        ),
    )


def _positive_seconds(value: Any, source: str, *, maximum: float) -> float:
    seconds = _finite_seconds(value, source)
    if seconds <= 0:
        raise ConfigError(f"{source} must be greater than zero, got {seconds}")
    if seconds > maximum:
        raise ConfigError(f"{source} is {seconds}s; {maximum}s is the most allowed.")
    return seconds


@dataclass(frozen=True, slots=True)
class MongoConfig:
    """Where the three documents live. Names, never credentials."""

    database: str = "hsc_queue_monitor"
    session_collection: str = "sessions"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> MongoConfig:
        _reject_secrets(raw, "app.yaml: mongodb")
        unknown = set(raw) - {"database", "session_collection"}
        if unknown:
            raise ConfigError(f"app.yaml: mongodb: unknown option(s) {sorted(unknown)}")
        return cls(
            database=_name(raw.get("database", "hsc_queue_monitor"), "app.yaml: mongodb.database"),
            session_collection=_name(
                raw.get("session_collection", "sessions"),
                "app.yaml: mongodb.session_collection",
            ),
        )


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Whether to notify at all. *Who* to notify is a secret and is not here."""

    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TelegramConfig:
        _reject_secrets(raw, "app.yaml: telegram")
        unknown = set(raw) - {"enabled"}
        if unknown:
            raise ConfigError(f"app.yaml: telegram: unknown option(s) {sorted(unknown)}")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError("app.yaml: telegram.enabled must be true or false")
        return cls(enabled=enabled)


@dataclass(frozen=True, slots=True)
class BrowserMonitorConfig:
    """The old browser `monitor` command's pacing. Local/debug only."""

    poll_interval_seconds: int = 60
    poll_jitter_seconds: int = 10
    notify_cooldown_seconds: int = 6 * 3600

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BrowserMonitorConfig:
        _reject_secrets(raw, "app.yaml: browser_monitor")
        known = {"poll_interval_seconds", "poll_jitter_seconds", "notify_cooldown_seconds"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                f"app.yaml: browser_monitor: unknown option(s) {sorted(unknown)}"
            )
        default = cls()
        return cls(
            poll_interval_seconds=max(
                _whole(raw.get("poll_interval_seconds", default.poll_interval_seconds),
                       "app.yaml: browser_monitor.poll_interval_seconds"),
                MIN_POLL_INTERVAL_SECONDS,
            ),
            poll_jitter_seconds=_whole(
                raw.get("poll_jitter_seconds", default.poll_jitter_seconds),
                "app.yaml: browser_monitor.poll_jitter_seconds",
            ),
            notify_cooldown_seconds=_whole(
                raw.get("notify_cooldown_seconds", default.notify_cooldown_seconds),
                "app.yaml: browser_monitor.notify_cooldown_seconds",
            ),
        )


def _name(value: Any, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source} must be a non-empty name, got {value!r}")
    return value.strip()


def _whole(value: Any, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{source} must be a whole number of seconds, got {value!r}")
    return value


def _reject_secrets(raw: Mapping[str, Any], source: str) -> None:
    """A committed file may not carry a credential, even by accident.

    Failing here rather than ignoring the key is the point: a secret written
    into version control is not a setting that does nothing, it is a secret in
    version control, and the person who put it there needs to know.
    """
    if not isinstance(raw, Mapping):
        return
    found = sorted(key for key in raw if str(key).strip().lower() in FORBIDDEN_CONFIG_KEYS)
    if found:
        raise ConfigError(
            f"{source}: {found} must never appear in a version-controlled file.\n"
            "Secrets come from the environment (.env locally, GitHub Environment "
            "secrets in CI) and from nowhere else."
        )


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """The read-only API path: pacing, timeouts and the one retry policy."""

    slot_request_interval_seconds: float = DEFAULT_SLOT_INTERVAL_SECONDS
    monitor_interval_seconds: float = DEFAULT_MONITOR_INTERVAL_SECONDS
    connect_timeout_seconds: float = CONNECT_TIMEOUT
    read_timeout_seconds: float = READ_TIMEOUT
    #: The one retry policy. Everything transient goes through it, and nothing
    #: else in the project retries at all.
    retry: RetryConfig = field(default_factory=RetryConfig)

    @property
    def timeout(self) -> tuple[float, float]:
        """``(connect, read)``, the pair ``requests`` takes."""
        return (self.connect_timeout_seconds, self.read_timeout_seconds)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ApiConfig:
        if not isinstance(raw, Mapping):
            raise ConfigError("app.yaml: top-level `api:` must be a mapping")
        _reject_secrets(raw, "app.yaml: api")

        unknown = set(raw) - {
            "slot_request_interval_seconds",
            "monitor_interval_seconds",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "retry",
        }
        if unknown:
            raise ConfigError(f"app.yaml: api: unknown option(s) {sorted(unknown)}")

        return cls(
            slot_request_interval_seconds=validate_slot_interval(
                raw.get("slot_request_interval_seconds", DEFAULT_SLOT_INTERVAL_SECONDS),
                "app.yaml: api.slot_request_interval_seconds",
            ),
            monitor_interval_seconds=validate_monitor_interval(
                raw.get("monitor_interval_seconds", DEFAULT_MONITOR_INTERVAL_SECONDS),
                "app.yaml: api.monitor_interval_seconds",
            ),
            connect_timeout_seconds=validate_timeout(
                raw.get("connect_timeout_seconds", CONNECT_TIMEOUT),
                "app.yaml: api.connect_timeout_seconds",
            ),
            read_timeout_seconds=validate_timeout(
                raw.get("read_timeout_seconds", READ_TIMEOUT),
                "app.yaml: api.read_timeout_seconds",
            ),
            retry=_retry_from_dict(raw.get("retry") or {}),
        )


@dataclass(frozen=True, slots=True)
class StepPlan:
    """How to reach the screen that holds a given selector.

    ``prerequisites`` is executed **verbatim and in order** — it is not expanded
    transitively, so each entry must list its full chain of ancestors.
    """

    key: str
    start_url: str | None = None
    prerequisites: tuple[str, ...] = ()

    @property
    def has_prerequisites(self) -> bool:
        return bool(self.prerequisites)

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any] | None) -> StepPlan:
        entry = raw or {}
        if not isinstance(entry, Mapping):
            raise ConfigError(f"flow.yaml: steps.{key} must be a mapping")

        unknown = set(entry) - {"start_url", "prerequisites"}
        if unknown:
            raise ConfigError(f"flow.yaml: steps.{key}: unknown option(s) {sorted(unknown)}")

        prerequisites = entry.get("prerequisites") or []
        if not isinstance(prerequisites, list) or not all(
            isinstance(p, str) for p in prerequisites
        ):
            raise ConfigError(
                f"flow.yaml: steps.{key}.prerequisites must be a list of selector keys"
            )
        if key in prerequisites:
            raise ConfigError(
                f"flow.yaml: steps.{key} lists itself as a prerequisite, which would "
                "click the very element being validated"
            )

        start_url = entry.get("start_url")
        return cls(
            key=key,
            start_url=None if start_url is None else str(start_url),
            prerequisites=tuple(prerequisites),
        )


@dataclass(frozen=True, slots=True)
class FlowConfig:
    base_url: str = "https://eqn.hsc.gov.ua"
    #: Informational only — nothing navigates here. The registration screen is
    #: reached by clicking ``queue.start_registration`` from the cabinet.
    queue_url: str = "https://eqn.hsc.gov.ua/cabinet/queue"
    #: Where every journey starts, and what prerequisite chains default to.
    cabinet_url: str = "https://eqn.hsc.gov.ua/cabinet"
    login_enabled: bool = True
    queue_steps: tuple[str, ...] = ()
    check_departments: bool = True
    debug: DebugConfig = field(default_factory=DebugConfig)
    timeouts: Timeouts = field(default_factory=Timeouts)
    #: Non-secret inputs to the ID.GOV.UA journey (the КНЕДП of the key).
    authentication: AuthenticationConfig = field(default_factory=AuthenticationConfig)
    #: selector key -> how to reach it. Keyed by selector key, not step name.
    steps: dict[str, StepPlan] = field(default_factory=dict)

    def plan_for(self, selector_key: str) -> StepPlan:
        """The plan for a selector, or an empty one when none is configured."""
        return self.steps.get(selector_key) or StepPlan(key=selector_key)

    def start_url_for(self, selector_key: str) -> str:
        """Explicit per-step URL, else the cabinet entry point.

        Never falls back to :attr:`queue_url`: that screen is reached by
        clicking ``queue.start_registration``, not by navigation.
        """
        return self.plan_for(selector_key).start_url or self.cabinet_url

    @classmethod
    def from_file(cls, path: Path) -> FlowConfig:
        return cls.from_dict(_read_yaml(path))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FlowConfig:
        site = raw.get("site") or {}
        flow = raw.get("flow") or {}
        queue = flow.get("queue") or {}
        login = flow.get("login") or {}
        monitor = flow.get("monitor") or {}
        debug = raw.get("debug") or {}
        timeouts = raw.get("timeouts") or {}
        authentication = raw.get("authentication") or {}

        steps = queue.get("steps") or []
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            raise ConfigError("flow.yaml: flow.queue.steps must be a list of step names")

        plans_raw = raw.get("steps") or {}
        if not isinstance(plans_raw, Mapping):
            raise ConfigError(
                "flow.yaml: top-level `steps:` must map a selector key to its "
                "start_url / prerequisites"
            )
        plans = {str(key): StepPlan.from_dict(str(key), entry)
                 for key, entry in plans_raw.items()}

        return cls(
            base_url=str(site.get("base_url", "https://eqn.hsc.gov.ua")).rstrip("/"),
            queue_url=str(site.get("queue_url", "https://eqn.hsc.gov.ua/cabinet/queue")),
            cabinet_url=str(site.get("cabinet_url", "https://eqn.hsc.gov.ua/cabinet")),
            login_enabled=bool(login.get("enabled", True)),
            queue_steps=tuple(steps),
            check_departments=bool(monitor.get("check_departments", True)),
            debug=DebugConfig(
                pause_after_step=bool(debug.get("pause_after_step", False)),
                screenshots=bool(debug.get("screenshots", True)),
                dump_elements=bool(debug.get("dump_elements", False)),
            ),
            timeouts=Timeouts(
                default_locator=int(timeouts.get("default_locator", 15_000)),
                navigation=int(timeouts.get("navigation", 30_000)),
                manual_challenge=int(timeouts.get("manual_challenge", 600_000)),
                authentication=int(timeouts.get("authentication", 120_000)),
            ),
            authentication=AuthenticationConfig.from_dict(authentication),
            steps=plans,
        )


# --------------------------------------------------------------------------- #
# app.yaml — every non-sensitive operational setting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AppSettings:
    """What ``config/app.yaml`` says. Version-controlled, and safe to read.

    Nothing here is a credential, which is what makes it reviewable in a diff:
    a change to a timeout should be as visible as a change to a retry count,
    and neither should require anybody to look at a settings page.
    """

    mongodb: MongoConfig = field(default_factory=MongoConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    browser_monitor: BrowserMonitorConfig = field(default_factory=BrowserMonitorConfig)
    #: A visible browser is the default: the ID.GOV.UA journey needs one, and a
    #: challenge nobody can see is a run nobody can finish.
    headless: bool = False

    @classmethod
    def from_file(cls, path: Path) -> AppSettings:
        return cls.from_dict(_read_yaml(path) if path.exists() else {})

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AppSettings:
        _reject_secrets(raw, "app.yaml")
        known = {"mongodb", "api", "telegram", "browser_monitor", "browser"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"app.yaml: unknown section(s) {sorted(unknown)}")

        browser = raw.get("browser") or {}
        _reject_secrets(browser, "app.yaml: browser")
        headless = browser.get("headless", False)
        if not isinstance(headless, bool):
            raise ConfigError("app.yaml: browser.headless must be true or false")

        return cls(
            mongodb=MongoConfig.from_dict(raw.get("mongodb") or {}),
            api=ApiConfig.from_dict(raw.get("api") or {}),
            telegram=TelegramConfig.from_dict(raw.get("telegram") or {}),
            browser_monitor=BrowserMonitorConfig.from_dict(raw.get("browser_monitor") or {}),
            headless=headless,
        )


# --------------------------------------------------------------------------- #
# Service centres
# --------------------------------------------------------------------------- #


def load_service_centers(path: Path) -> list[ServiceCenter]:
    raw = _read_yaml(path)
    entries = raw.get("service_centers")
    if not isinstance(entries, list):
        raise ConfigError("service_centers.yaml: expected a `service_centers:` list")

    centers: list[ServiceCenter] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or "name" not in entry:
            raise ConfigError(
                f"service_centers.yaml: entry #{index + 1} must be a mapping with a `name:`"
            )
        center_id = str(entry.get("id", "")).strip()
        if not center_id:
            raise ConfigError(
                f"service_centers.yaml: entry #{index + 1} ({entry['name']}) needs an `id:` — "
                "the numeric service centre ID is the stable identity, the address is not."
            )
        if center_id in seen:
            raise ConfigError(
                f"service_centers.yaml: duplicate service centre id {center_id!r}"
            )
        seen.add(center_id)
        centers.append(
            ServiceCenter(
                name=str(entry["name"]),
                enabled=bool(entry.get("enabled", True)),
                id=center_id,
                full_name=str(entry.get("full_name", "")),
            )
        )
    return centers


def find_service_center(centers: list[ServiceCenter], wanted: str) -> ServiceCenter:
    """Look a centre up by ID (preferred) or by its exact configured name."""
    key = wanted.strip()
    for center in centers:
        if center.id == key or center.name == key:
            return center
    known = ", ".join(f"{c.id} ({c.name})" for c in centers) or "(none configured)"
    raise ConfigError(
        f"Unknown service centre {wanted!r}.\nConfigured centres: {known}\n"
        "Add it to config/service_centers.yaml."
    )


def enabled_service_centers(centers: list[ServiceCenter]) -> list[ServiceCenter]:
    """Enabled centres whose name is still a TODO placeholder are rejected."""
    usable = [c for c in centers if c.enabled]
    todo = [c.name for c in usable if c.name.strip().upper().startswith("TODO")]
    if todo:
        raise SelectorNotConfigured(
            "service_centers",
            f"These entries are still placeholders: {todo}. Replace them with the "
            "exact names shown on the service centre cards.",
        )
    return usable


#: One scan opens the wizard once per centre and walks every free date inside
#: it, so the run time grows with this number. Five is the point past which a
#: "check what is free" turns into sustained traffic, which this project is not.
MAX_SCANNED_CENTERS = 5


def centers_to_scan(
    centers: list[ServiceCenter], overrides: Sequence[str] = ()
) -> list[ServiceCenter]:
    """The 1–5 centres one availability scan covers.

    Without ``overrides`` this is the enabled list from
    ``config/service_centers.yaml`` — no ID is ever written in code. Passing
    ``--center`` values *replaces* that list for the run (they are looked up in
    the same file, so a typo is still a configuration error, and a disabled
    entry is honoured because it was asked for by ID).

    Both bounds are refused rather than clamped: nothing to scan is a mistake
    worth saying out loud, and too much to scan is a decision the operator
    should make deliberately.
    """
    if overrides:
        chosen: list[ServiceCenter] = []
        for wanted in overrides:
            center = find_service_center(centers, wanted)
            if center not in chosen:  # --center 3242 --center 3242 is one centre
                chosen.append(center)
    else:
        chosen = enabled_service_centers(centers)

    if not chosen:
        raise ConfigError(
            "No service centre to scan.\n"
            "Enable at least one entry in config/service_centers.yaml "
            "(`enabled: true`), or name one with --center 3242."
        )
    if len(chosen) > MAX_SCANNED_CENTERS:
        listing = ", ".join(c.id or c.name for c in chosen)
        raise ConfigError(
            f"{len(chosen)} service centres were requested, and a scan covers at "
            f"most {MAX_SCANNED_CENTERS}.\nRequested: {listing}\n"
            "Disable some entries in config/service_centers.yaml, or name up to "
            f"{MAX_SCANNED_CENTERS} with --center."
        )
    return chosen


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Everything one command needs, with the sources kept apart on purpose.

    ``secrets`` comes from the environment and only from there. ``app``,
    ``flow``, ``selectors`` and ``service_centers`` come from files that are
    committed, reviewed and diffed. Nothing crosses: a credential in YAML is
    refused, and a timeout in an environment variable is not read.

    Note: ``selectors`` and ``flow`` are loaded lazily on first access.
    Headless commands (monitor-once) do not load them.
    """

    secrets: SecretSettings
    app: AppSettings
    paths: Paths
    service_centers: list[ServiceCenter]
    # Fields can be None when first created, but properties return non-None
    _selectors: SelectorRegistry | None = field(default=None, init=True, repr=False)
    _flow: FlowConfig | None = field(default=None, init=True, repr=False)

    @property
    def selectors(self) -> SelectorRegistry:
        """Load selectors lazily only when needed (browser commands)."""
        if self._selectors is None:
            sel = SelectorRegistry.from_file(self.paths.config_dir / "selectors.yaml")
            # For frozen dataclass, we must use object.__setattr__
            object.__setattr__(self, "_selectors", sel)
        assert self._selectors is not None
        return self._selectors

    @property
    def flow(self) -> FlowConfig:
        """Load flow lazily only when needed (browser commands)."""
        if self._flow is None:
            cfg = FlowConfig.from_file(self.paths.config_dir / "flow.yaml")
            # For frozen dataclass, we must use object.__setattr__
            object.__setattr__(self, "_flow", cfg)
        assert self._flow is not None
        return self._flow

    @classmethod
    def load(
        cls,
        *,
        config_dir: Path | None = None,
        data_dir: Path | None = None,
        env_file: Path | None = None,
        headless: bool | None = None,
    ) -> AppConfig:
        paths = Paths(
            data_dir=data_dir or DEFAULT_DATA_DIR,
            config_dir=config_dir or DEFAULT_CONFIG_DIR,
        )
        app = AppSettings.from_file(paths.config_dir / "app.yaml")
        if headless is not None:
            # The one CLI override, and it wins: --headed/--headless is a
            # decision about this run, not about the project.
            app = replace(app, headless=headless)

        return cls(
            secrets=load_secrets(env_file=env_file),
            app=app,
            paths=paths,
            service_centers=load_service_centers(paths.service_centers_path),
            _selectors=None,
            _flow=None,
        )
