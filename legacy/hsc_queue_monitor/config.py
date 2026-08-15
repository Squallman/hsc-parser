"""Configuration loading.

Settings come from environment variables (optionally via ``.env``) and can be
overridden per-invocation by the CLI. No secret is ever written back to disk by
this module; the Telegram token stays in memory only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://eqn.hsc.gov.ua"
DEFAULT_QUEUE_URL = "https://eqn.hsc.gov.ua/cabinet/queue"
DEFAULT_SERVICE_ID = 47

#: Every discovered/known queue endpoint lives under this prefix.
API_PREFIX = "/api/v2/equeue/"


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _get_optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get_optional_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %s", name, raw, default)
        return default


def _get_float(name: str, default: float) -> float:
    raw = _get_optional_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid number for %s=%r, using default %s", name, raw, default)
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get_optional_str(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_int_tuple(name: str) -> tuple[int, ...]:
    raw = _get_optional_str(name)
    if raw is None:
        return ()
    values: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(int(chunk))
        except ValueError:
            logger.warning("Ignoring non-numeric department id %r in %s", chunk, name)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime configuration."""

    base_url: str = DEFAULT_BASE_URL
    queue_url: str = DEFAULT_QUEUE_URL
    service_id: int = DEFAULT_SERVICE_ID
    department_ids: tuple[int, ...] = ()
    date_from: str | None = None
    date_to: str | None = None

    poll_interval_seconds: float = 60.0
    poll_jitter_seconds: float = 10.0
    min_poll_interval_seconds: float = 30.0
    request_delay_seconds: float = 1.5

    headless: bool = False
    data_dir: Path = field(default_factory=lambda: Path("data"))
    profile_dir: Path = field(default_factory=lambda: Path("data/browser-profile"))
    locale: str = "uk-UA"
    timezone: str = "Europe/Kyiv"
    auth_timeout_seconds: float = 600.0
    navigation_timeout_ms: float = 60_000.0

    inspect_network: bool = False
    log_level: str = "INFO"

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def network_events_file(self) -> Path:
        return self.data_dir / "network-events.jsonl"

    @property
    def api_base(self) -> str:
        return f"{self.base_url.rstrip('/')}{API_PREFIX}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def effective_poll_interval(self) -> float:
        """Polling interval clamped to the configured safe minimum."""
        return max(self.poll_interval_seconds, self.min_poll_interval_seconds)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def with_overrides(self, **overrides: object) -> Settings:
        """Return a copy with ``None`` overrides ignored."""
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, *, env_file: str | os.PathLike[str] | None = None) -> Settings:
        load_dotenv(dotenv_path=env_file, override=False)

        data_dir = Path(_get_str("HSC_DATA_DIR", "data")).expanduser()
        profile_raw = _get_optional_str("HSC_PROFILE_DIR")
        profile_dir = (
            Path(profile_raw).expanduser() if profile_raw else data_dir / "browser-profile"
        )

        settings = cls(
            base_url=_get_str("HSC_BASE_URL", DEFAULT_BASE_URL),
            queue_url=_get_str("HSC_QUEUE_URL", DEFAULT_QUEUE_URL),
            service_id=_get_int("HSC_SERVICE_ID", DEFAULT_SERVICE_ID),
            department_ids=_get_int_tuple("HSC_DEPARTMENT_IDS"),
            date_from=_get_optional_str("HSC_DATE_FROM"),
            date_to=_get_optional_str("HSC_DATE_TO"),
            poll_interval_seconds=_get_float("HSC_POLL_INTERVAL_SECONDS", 60.0),
            poll_jitter_seconds=abs(_get_float("HSC_POLL_JITTER_SECONDS", 10.0)),
            min_poll_interval_seconds=_get_float("HSC_MIN_POLL_INTERVAL_SECONDS", 30.0),
            request_delay_seconds=_get_float("HSC_REQUEST_DELAY_SECONDS", 1.5),
            headless=_get_bool("HSC_HEADLESS", False),
            data_dir=data_dir,
            profile_dir=profile_dir,
            locale=_get_str("HSC_LOCALE", "uk-UA"),
            timezone=_get_str("HSC_TIMEZONE", "Europe/Kyiv"),
            auth_timeout_seconds=_get_float("HSC_AUTH_TIMEOUT_SECONDS", 600.0),
            log_level=_get_str("HSC_LOG_LEVEL", "INFO").upper(),
            telegram_bot_token=_get_optional_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get_optional_str("TELEGRAM_CHAT_ID"),
        )

        if settings.poll_interval_seconds < settings.min_poll_interval_seconds:
            logger.warning(
                "HSC_POLL_INTERVAL_SECONDS=%.1fs is below the safe minimum %.1fs; using %.1fs",
                settings.poll_interval_seconds,
                settings.min_poll_interval_seconds,
                settings.effective_poll_interval,
            )
        return settings


def describe(settings: Settings) -> str:
    """Human-readable, secret-free summary of the active settings."""
    departments = (
        ",".join(str(i) for i in settings.department_ids) if settings.department_ids else "<all>"
    )
    return (
        f"service_id={settings.service_id} departments={departments} "
        f"interval={settings.effective_poll_interval:.0f}s(+/-{settings.poll_jitter_seconds:.0f}s) "
        f"headless={settings.headless} profile={settings.profile_dir} "
        f"telegram={'on' if settings.telegram_enabled else 'off'}"
    )
