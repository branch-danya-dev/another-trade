from __future__ import annotations

import json
from pathlib import Path

import httpx

from another_trade.bybit.client import BybitPublicClient


def test_empty_historical_kline_response_is_not_cached(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"category": "linear", "symbol": "XUSDT", "list": []},
            "retExtInfo": {},
            "time": 1,
        }
    ).encode()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
        cache_dir=tmp_path,
    )
    try:
        for _ in range(2):
            client.kline_page(
                symbol="XUSDT",
                start_ms=0,
                end_ms=60_000,
                now_ms=10 * 24 * 60 * 60 * 1000,
            )
    finally:
        client.close()

    assert calls == 2
    assert not list(tmp_path.rglob("*.body"))
