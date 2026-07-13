"""Per-environment settings from config/<env>.toml (selected by RISK_ENV), with
secrets overlaid from env vars only - never the TOML, so config files stay safe to
commit. Relative paths resolve against APP_HOME (inputs) or DATA_DIR (runtime data).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# hobo/config.py -> hobo/ -> repo root: the default base for relative paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = "config"
DEFAULT_ENV = "local"
VALID_BACKENDS = ("paper", "exchange")


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


# --- path + env helpers ---


def _app_home(environ: dict[str, str]) -> Path:
    """Base for app inputs (config, seeds). APP_HOME overrides the repo root, so
    the app runs from any CWD and a deployment can relocate it."""
    return Path(environ.get("APP_HOME") or PROJECT_ROOT)


def _data_dir(environ: dict[str, str]) -> Path:
    """Root for writable runtime data (event log, snapshots). DATA_DIR overrides
    it; defaults to <APP_HOME>/data."""
    return Path(environ.get("DATA_DIR") or (_app_home(environ) / "data"))


def _resolve(path: str, base: Path) -> str:
    """Join a relative config path onto `base`; pass absolute paths through unchanged."""
    p = Path(path)
    return str(p if p.is_absolute() else base / p)


def _env_bool(environ: dict[str, str], key: str, default: bool) -> bool:
    raw = environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- typed config sections (each parses its own TOML section + env overlay) ---


@dataclass(frozen=True)
class ExchangeConfig:
    name: str  # selects the adapter, e.g. "okx"
    ws_url: str
    ws_private_url: str
    rest_url: str
    instrument_ids: list[str]
    demo_trading: bool
    api_key: str
    api_secret: str
    api_passphrase: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    @classmethod
    def from_raw(cls, raw: dict, environ: dict[str, str]) -> "ExchangeConfig":
        return cls(
            name=raw["name"],
            ws_url=raw["ws_url"],
            ws_private_url=raw["ws_private_url"],
            rest_url=raw["rest_url"],
            instrument_ids=raw["instruments"],
            demo_trading=raw.get("demo_trading", False),
            api_key=environ.get("EXCHANGE_API_KEY", ""),  # secrets from env only, never the TOML
            api_secret=environ.get("EXCHANGE_API_SECRET", ""),
            api_passphrase=environ.get("EXCHANGE_API_PASSPHRASE", ""),
        )


@dataclass(frozen=True)
class RiskConfig:
    maintenance_margin_rate: float

    @classmethod
    def from_raw(cls, raw: dict) -> "RiskConfig":
        return cls(maintenance_margin_rate=raw["maintenance_margin_rate"])


@dataclass(frozen=True)
class ExecutionConfig:
    backend: str  # "paper" | "exchange"

    @classmethod
    def from_raw(cls, raw: dict) -> "ExecutionConfig":
        backend = raw["backend"]
        if backend not in VALID_BACKENDS:
            raise ConfigError(f"execution.backend must be one of {VALID_BACKENDS}, got {backend!r}")
        return cls(backend=backend)


@dataclass(frozen=True)
class DurabilityConfig:
    event_log_path: str
    snapshot_path: str
    desk_seed_path: str
    fsync_interval_ms: int

    @classmethod
    def from_raw(cls, raw: dict, app_home: Path, data_dir: Path) -> "DurabilityConfig":
        return cls(
            event_log_path=_resolve(raw["event_log_path"], data_dir),
            snapshot_path=_resolve(raw["snapshot_path"], data_dir),
            desk_seed_path=_resolve(raw["desk_seed_path"], app_home),
            fsync_interval_ms=raw["fsync_interval_ms"],
        )


@dataclass(frozen=True)
class ColdPathConfig:
    metrics_port: int

    @classmethod
    def from_raw(cls, raw: dict, environ: dict[str, str]) -> "ColdPathConfig":
        return cls(metrics_port=int(environ.get("METRICS_PORT") or raw["metrics_port"]))


@dataclass(frozen=True)
class Config:
    exchange: ExchangeConfig
    risk: RiskConfig
    execution: ExecutionConfig
    durability: DurabilityConfig
    coldpath: ColdPathConfig
    replica_mode: bool

    @classmethod
    def load(
        cls,
        env: str | None = None,
        config_dir: str = DEFAULT_CONFIG_DIR,
        environ: dict[str, str] | None = None,
    ) -> "Config":
        if environ is None:
            load_dotenv()  # local-dev convenience; production sets real env vars
            environ = dict(os.environ)
        env = env or environ.get("RISK_ENV", DEFAULT_ENV)
        path = Path(_resolve(config_dir, _app_home(environ))) / f"{env}.toml"
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls.from_toml(raw, environ)

    @classmethod
    def from_toml(cls, raw: dict, environ: dict[str, str]) -> "Config":
        app_home, data_dir = _app_home(environ), _data_dir(environ)
        try:
            config = cls(
                exchange=ExchangeConfig.from_raw(raw["exchange"], environ),
                risk=RiskConfig.from_raw(raw["risk"]),
                execution=ExecutionConfig.from_raw(raw["execution"]),
                durability=DurabilityConfig.from_raw(raw["durability"], app_home, data_dir),
                coldpath=ColdPathConfig.from_raw(raw["coldpath"], environ),
                replica_mode=_env_bool(environ, "REPLICA_MODE", raw.get("replica", {}).get("mode", False)),
            )
        except KeyError as exc:
            raise ConfigError(f"missing config key: {exc}") from exc

        if config.execution.backend == "exchange" and not config.exchange.has_credentials:
            raise ConfigError(
                "execution.backend=exchange requires EXCHANGE_API_KEY, EXCHANGE_API_SECRET, EXCHANGE_API_PASSPHRASE"
            )
        return config
