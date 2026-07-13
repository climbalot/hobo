from __future__ import annotations

import httpx
import pytest

from hobo.adapters.okx.market_data import MarketDataError, OkxMarketData, parse_mark_price
from hobo.adapters.okx.reference import OkxReferenceData, ReferenceDataError, parse_contract_spec
from hobo.adapters.okx.rest import OkxRestClient


@pytest.fixture
def instruments_response() -> dict:
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "uly": "BTC-USDT",
                "settleCcy": "USDT",
                "ctVal": "0.01",
                "ctMult": "1",
                "ctValCcy": "BTC",
                "lever": "125",
                "tickSz": "0.1",
                "lotSz": "1",
                "minSz": "1",
                "ctType": "linear",
                "state": "live",
            }
        ],
    }


@pytest.fixture
def mark_price_response() -> dict:
    return {
        "code": "0",
        "msg": "",
        "data": [{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "markPx": "50123.4", "ts": "1597026383085"}],
    }


# --- pure parsing ---


def test_parse_contract_spec(instruments_response):
    spec = parse_contract_spec(instruments_response, "BTC-USDT-SWAP")
    assert spec.instrument_id == "BTC-USDT-SWAP"
    assert spec.ct_val == 0.01
    assert spec.ct_mult == 1.0
    assert spec.max_leverage == 125.0
    assert spec.settle_ccy == "USDT"
    assert spec.contract_type == "linear"


def test_parse_contract_spec_missing_instrument_raises(instruments_response):
    with pytest.raises(ReferenceDataError, match="ETH-USDT-SWAP"):
        parse_contract_spec(instruments_response, "ETH-USDT-SWAP")


def test_parse_contract_spec_malformed_field_raises():
    bad = {"data": [{"instId": "BTC-USDT-SWAP", "ctVal": "not-a-number"}]}
    with pytest.raises(ReferenceDataError, match="malformed"):
        parse_contract_spec(bad, "BTC-USDT-SWAP")


def test_parse_mark_price(mark_price_response):
    price, ts_ns = parse_mark_price(mark_price_response, "BTC-USDT-SWAP")
    assert price == pytest.approx(50123.4)
    assert ts_ns == 1597026383085 * 1_000_000


def test_parse_mark_price_missing_instrument_raises(mark_price_response):
    with pytest.raises(MarketDataError):
        parse_mark_price(mark_price_response, "ETH-USDT-SWAP")


# --- capabilities over a fake OKX REST client (no live network) ---


def _mock_rest(instruments_response, mark_price_response) -> OkxRestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/public/instruments":
            return httpx.Response(200, json=instruments_response)
        if request.url.path == "/api/v5/public/mark-price":
            return httpx.Response(200, json=mark_price_response)
        return httpx.Response(404, json={"error": "not found"})

    return OkxRestClient("https://www.okx.com", transport=httpx.MockTransport(handler))


def test_reference_data_fetch_contract_spec(instruments_response, mark_price_response):
    rest = _mock_rest(instruments_response, mark_price_response)
    spec = OkxReferenceData(rest).fetch_contract_spec("BTC-USDT-SWAP")
    assert spec.instrument_id == "BTC-USDT-SWAP"
    assert spec.max_leverage == 125.0
    rest.close()


def test_market_data_fetch_mark(instruments_response, mark_price_response):
    rest = _mock_rest(instruments_response, mark_price_response)
    market = OkxMarketData(rest, ws_url="wss://example.invalid", register_ws=lambda client: None)
    price, ts_ns = market.fetch_mark("BTC-USDT-SWAP")
    assert price == pytest.approx(50123.4)
    assert ts_ns == 1597026383085 * 1_000_000
    rest.close()
