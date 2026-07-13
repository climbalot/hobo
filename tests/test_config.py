from __future__ import annotations

import copy

import pytest

from hobo.config import Config, ConfigError


def raw_config() -> dict:
    return {
        "exchange": {
            "name": "okx",
            "ws_url": "wss://ws.okx.com:8443/ws/v5/public",
            "ws_private_url": "wss://wspap.okx.com:8443/ws/v5/private",
            "rest_url": "https://www.okx.com",
            "instruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            "demo_trading": False,
        },
        "risk": {"maintenance_margin_rate": 0.005},
        "execution": {"backend": "paper"},
        "durability": {
            "event_log_path": "local/eventlog.bin",
            "snapshot_path": "local/snapshots",
            "desk_seed_path": "./seeds/desk_seed.json",
            "fsync_interval_ms": 100,
        },
        "coldpath": {"metrics_port": 9090},
        "replica": {"mode": False},
    }


def test_from_toml_loads_all_sections():
    cfg = Config.from_toml(raw_config(), environ={})
    assert cfg.exchange.name == "okx"
    assert cfg.exchange.instrument_ids == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert cfg.risk.maintenance_margin_rate == 0.005
    assert cfg.execution.backend == "paper"
    assert cfg.coldpath.metrics_port == 9090
    assert cfg.replica_mode is False
    assert cfg.durability.event_log_path.endswith("local/eventlog.bin")


def test_secrets_come_from_environ_not_toml():
    environ = {
        "EXCHANGE_API_KEY": "key",
        "EXCHANGE_API_SECRET": "secret",
        "EXCHANGE_API_PASSPHRASE": "pass",
    }
    cfg = Config.from_toml(raw_config(), environ)
    assert cfg.exchange.api_key == "key"
    assert cfg.exchange.api_secret == "secret"
    assert cfg.exchange.has_credentials is True


def test_no_credentials_means_has_credentials_false():
    cfg = Config.from_toml(raw_config(), environ={})
    assert cfg.exchange.has_credentials is False


def test_exchange_backend_requires_credentials():
    raw = raw_config()
    raw["execution"]["backend"] = "exchange"
    with pytest.raises(ConfigError, match="EXCHANGE_API_KEY"):
        Config.from_toml(raw, environ={})


def test_exchange_backend_with_credentials_is_valid():
    raw = raw_config()
    raw["execution"]["backend"] = "exchange"
    environ = {
        "EXCHANGE_API_KEY": "k",
        "EXCHANGE_API_SECRET": "s",
        "EXCHANGE_API_PASSPHRASE": "p",
    }
    cfg = Config.from_toml(raw, environ)
    assert cfg.execution.backend == "exchange"


def test_invalid_backend_raises():
    raw = raw_config()
    raw["execution"]["backend"] = "bogus"
    with pytest.raises(ConfigError, match="backend"):
        Config.from_toml(raw, environ={})


def test_missing_section_raises_config_error():
    raw = raw_config()
    del raw["risk"]
    with pytest.raises(ConfigError, match="missing config key"):
        Config.from_toml(raw, environ={})


@pytest.mark.parametrize("raw_val,expected", [("true", True), ("1", True), ("false", False), ("", False)])
def test_replica_mode_env_override(raw_val, expected):
    cfg = Config.from_toml(raw_config(), environ={"REPLICA_MODE": raw_val})
    assert cfg.replica_mode is expected


def test_replica_mode_defaults_from_toml_when_env_absent():
    raw = raw_config()
    raw["replica"]["mode"] = True
    cfg = Config.from_toml(raw, environ={})
    assert cfg.replica_mode is True


def test_metrics_port_env_override():
    cfg = Config.from_toml(raw_config(), environ={"METRICS_PORT": "9999"})
    assert cfg.coldpath.metrics_port == 9999


def test_load_reads_local_toml_from_disk():
    # RISK_ENV=local against the committed config/local.toml, resolved from the repo root.
    cfg = Config.load(env="local", environ={})
    assert cfg.exchange.name == "okx"
    assert cfg.execution.backend == "paper"
    assert "BTC-USDT-SWAP" in cfg.exchange.instrument_ids


def test_load_missing_env_file_raises():
    with pytest.raises(ConfigError, match="config file not found"):
        Config.load(env="does-not-exist", environ={})


def test_from_toml_does_not_mutate_input():
    raw = raw_config()
    before = copy.deepcopy(raw)
    Config.from_toml(raw, environ={})
    assert raw == before
