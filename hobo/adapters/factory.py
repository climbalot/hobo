"""Adapter factory: maps an exchange name to its ExchangeAdapter. Register a new
exchange by adding it to `_REGISTRY`; nothing else changes (the core depends only
on the ExchangeAdapter interface).
"""

from __future__ import annotations

from hobo.adapters.base import ExchangeAdapter
from hobo.adapters.okx.adapter import OkxAdapter
from hobo.config import ExchangeConfig

_REGISTRY: dict[str, type[ExchangeAdapter]] = {
    "okx": OkxAdapter,
}


def build_adapter(config: ExchangeConfig) -> ExchangeAdapter:
    try:
        adapter_cls = _REGISTRY[config.name]
    except KeyError:
        raise ValueError(f"unknown exchange {config.name!r}; known: {sorted(_REGISTRY)}") from None
    return adapter_cls(config)
