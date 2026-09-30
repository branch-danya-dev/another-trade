from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.errors import BybitApiError

FIXTURES = Path(__file__).parent / "fixtures" / "bybit"


def fixture(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"real fixture not captured yet: {path}")
    return path.read_bytes()


def test_real_closed_inventory_fixture_is_strictly_parsed() -> None:
    raw = fixture("instruments_closed.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    try:
        items = client.instruments("Closed")
    finally:
        client.close()

    assert items
    assert any(item.status == "Closed" for item in items)
    assert any(
        item.contractType == "LinearPerpetual" and item.quoteCoin == "USDT" for item in items
    )


def test_real_nonzero_retcode_is_not_hidden_by_http_200() -> None:
    raw = fixture("retcode_error.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
        max_retries=0,
    )
    try:
        with pytest.raises(BybitApiError):
            client.kline_page(
                symbol="THIS_SYMBOL_MUST_NOT_EXIST_USDT",
                start_ms=1,
                end_ms=2,
                now_ms=10_000_000,
            )
    finally:
        client.close()


def test_retcode_10006_retries_even_with_http_200() -> None:
    rate_limited = json.dumps(
        {
            "retCode": 10006,
            "retMsg": "Too many visits!",
            "result": {},
            "retExtInfo": {},
            "time": 1,
        }
    ).encode()
    success = fixture("kline_trading_symbol.json")
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = rate_limited if calls == 1 else success
        return httpx.Response(200, content=body, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
        max_retries=2,
        sleep=sleeps.append,
    )
    try:
        series = client.kline_page(
            symbol="BTCUSDT",
            start_ms=0,
            end_ms=9_999_999_999_999,
            now_ms=10_000_000_000_000,
        )
    finally:
        client.close()

    assert calls == 2
    assert sleeps
    assert series.candles


def test_json_numeric_float_is_decoded_as_decimal() -> None:
    payload = BybitPublicClient._decode(b'{"retCode":0,"retMsg":"OK","x":0.1}')  # noqa: SLF001
    assert payload["x"] == Decimal("0.1")
    assert not isinstance(payload["x"], float)
