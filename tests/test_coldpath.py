from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from prometheus_client import CollectorRegistry

from hobo.obs.metrics import ColdPathMetrics
from hobo.obs.server import ColdPathServer

from conftest import make_state


@pytest.fixture
def state(instrument):
    return make_state(instrument)


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def test_metrics_records_gate_decisions():
    metrics = ColdPathMetrics(registry=CollectorRegistry())
    metrics.on_gate_decision("A", True, "NONE", latency_ns=50_000)
    metrics.on_gate_decision("A", False, "BOOK_POSITION_LIMIT", latency_ns=75_000)

    approved = metrics.gate_decisions_total.labels(book="A", decision="approved", reason="NONE")._value.get()
    rejected = metrics.gate_decisions_total.labels(
        book="A", decision="rejected", reason="BOOK_POSITION_LIMIT"
    )._value.get()
    assert approved == 1
    assert rejected == 1


def test_state_metrics_expose_book_pnl(state):
    metrics = ColdPathMetrics(registry=CollectorRegistry())
    metrics.register_state_metrics(lambda: state)
    from prometheus_client import generate_latest

    body = generate_latest(metrics.registry)
    assert b"book_pnl_usdt" in body
    assert b"desk_pnl_usdt" in body


def test_server_metrics_status_and_healthz_endpoints(state):
    metrics = ColdPathMetrics(registry=CollectorRegistry())
    metrics.on_gate_decision("A", True, "NONE", latency_ns=42_000)

    with ColdPathServer(port=0, registry=metrics.registry, state_provider=lambda: state) as server:
        base = f"http://127.0.0.1:{server.port}"

        status, body = _get(f"{base}/metrics")
        assert status == 200
        assert b"gate_decisions_total" in body

        status, body = _get(f"{base}/status")
        assert status == 200
        payload = json.loads(body)
        assert "BTC-USDT-SWAP" in payload["instruments"]
        assert set(payload["desk"]["books"].keys()) == {"A", "B"}
        assert "summary" in payload

        status, body = _get(f"{base}/healthz")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}


def test_server_unknown_path_returns_404(state):
    metrics = ColdPathMetrics(registry=CollectorRegistry())
    with ColdPathServer(port=0, registry=metrics.registry, state_provider=lambda: state) as server:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"http://127.0.0.1:{server.port}/not-a-real-path")
        assert exc_info.value.code == 404
