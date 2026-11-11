import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILE = "config.toml"
CONFIG_FILE_ENV = "THREADSIFT_CONFIG"
DATABASE_URL_ENV = "THREADSIFT_DATABASE_URL"

Range = tuple[float, float]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = "postgresql://threadsift@localhost/threadsift"


@dataclass(frozen=True)
class BrowserConfig:
    session_dir: str = ".session"
    headless: bool = False
    channel: str = "chrome"
    backend: str = "patchright"


@dataclass(frozen=True)
class PacingConfig:
    action_pause_seconds: Range = (4.0, 12.0)
    url_pause_seconds: Range = (30.0, 90.0)
    dwell_seconds_per_char: float = 0.01
    dwell_jitter: float = 0.25
    dwell_max_chars: int = 2000
    urls_per_break: Range = (10, 15)
    break_minutes: Range = (3.0, 8.0)
    warmup_minutes: Range = (1.0, 3.0)


@dataclass(frozen=True)
class LimitsConfig:
    urls_per_run: int = 60
    url_action_budget: int = 150
    run_action_budget: int = 1200
    max_replies: int = 1000
    max_depth: int = 5


@dataclass(frozen=True)
class CooldownConfig:
    blocked_hours: float = 24.0
    early_warning_hours: float = 6.0


@dataclass(frozen=True)
class EarlyWarningConfig:
    empty_reveals: int = 3
    failure_window: int = 20
    failure_ratio: float = 0.3
    slowdown_pages: int = 5
    slowdown_factor: float = 3.0


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    early_warning: EarlyWarningConfig = field(default_factory=EarlyWarningConfig)


BACKENDS = ("patchright", "playwright")


def _coerce(path: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected a whole number")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: expected a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected text")
        return value
    if isinstance(default, tuple):
        ok = (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        )
        if not ok:
            raise ConfigError(f"{path}: expected a pair of numbers like [low, high]")
        return (value[0], value[1])
    raise AssertionError(f"unsupported config type at {path}")


def _build_section(name: str, section: Any, raw: Any) -> Any:
    if raw is None:
        return section
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}]: expected a table")
    known = {f.name for f in fields(section)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"[{name}]: unknown key(s): {', '.join(unknown)}")
    values = {
        key: _coerce(f"{name}.{key}", value, getattr(section, key))
        for key, value in raw.items()
    }
    return replace(section, **values)


def _validate(config: Config) -> None:
    pacing = config.pacing
    for key in (
        "action_pause_seconds",
        "url_pause_seconds",
        "urls_per_break",
        "break_minutes",
        "warmup_minutes",
    ):
        low, high = getattr(pacing, key)
        if low < 0 or low > high:
            raise ConfigError(f"pacing.{key}: need 0 <= low <= high, got [{low}, {high}]")
    if pacing.urls_per_break[0] < 1:
        raise ConfigError("pacing.urls_per_break: low must be at least 1")
    if pacing.dwell_seconds_per_char < 0:
        raise ConfigError("pacing.dwell_seconds_per_char: must not be negative")
    if pacing.dwell_max_chars < 0:
        raise ConfigError("pacing.dwell_max_chars: must not be negative")
    if not 0 <= pacing.dwell_jitter < 1:
        raise ConfigError("pacing.dwell_jitter: must be at least 0 and below 1")

    limits = config.limits
    for key in ("urls_per_run", "url_action_budget", "run_action_budget", "max_replies", "max_depth"):
        if getattr(limits, key) < 1:
            raise ConfigError(f"limits.{key}: must be at least 1")
    if limits.run_action_budget < limits.url_action_budget:
        raise ConfigError(
            "limits.run_action_budget: must cover at least one full url_action_budget"
        )

    if config.cooldown.blocked_hours < 0 or config.cooldown.early_warning_hours < 0:
        raise ConfigError("cooldown: hours must not be negative")

    warning = config.early_warning
    if warning.empty_reveals < 1 or warning.failure_window < 1 or warning.slowdown_pages < 1:
        raise ConfigError("early_warning: counts and windows must be at least 1")
    if not 0 < warning.failure_ratio <= 1:
        raise ConfigError("early_warning.failure_ratio: must be above 0 and at most 1")
    if warning.slowdown_factor <= 1:
        raise ConfigError("early_warning.slowdown_factor: must be above 1")

    if config.browser.backend not in BACKENDS:
        raise ConfigError(f"browser.backend: must be one of {', '.join(BACKENDS)}")
    if not config.browser.channel:
        raise ConfigError("browser.channel: must not be empty")
    if not config.database.url:
        raise ConfigError("database.url: must not be empty")


def load_config(path: Path | str | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    explicit = path is not None or CONFIG_FILE_ENV in env
    if path is None:
        path = env.get(CONFIG_FILE_ENV, DEFAULT_CONFIG_FILE)
    path = Path(path)

    raw: dict[str, Any] = {}
    if explicit and not path.exists():
        raise ConfigError(f"{path}: config file not found")
    if path.exists():
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path}: {error}") from error

    defaults = Config()
    unknown = sorted(set(raw) - {f.name for f in fields(defaults)})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")

    config = Config(
        **{
            f.name: _build_section(f.name, getattr(defaults, f.name), raw.get(f.name))
            for f in fields(defaults)
        }
    )

    database_url = env.get(DATABASE_URL_ENV)
    if database_url:
        config = replace(config, database=replace(config.database, url=database_url))

    _validate(config)
    return config
