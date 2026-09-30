from __future__ import annotations

import json

import httpx

from another_trade.bybit.client import BybitPublicClient


def envelope(rows: list[list[str]]) -> bytes:
    return json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"category": "linear", "symbol": "BTCUSDT", "list": rows},
            "retExtInfo": {},
            "time": 1,
        }
    ).encode()


def row(start: int) -> list[str]:
    return [str(start), "1", "1", "1", "1", "1", "1"]


def test_full_raw_page_with_unfinished_row_does_not_stop_pagination() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Two raw rows == full page. Newest is unfinished and will be filtered.
            body = envelope([row(180_000), row(120_000)])
        else:
            body = envelope([row(60_000), row(0)])
        return httpx.Response(200, content=body, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    try:
        series = client.klines(
            symbol="BTCUSDT",
            start_ms=0,
            end_ms=180_000,
            interval="1",
            limit=2,
            now_ms=200_000,
        )
    finally:
        client.close()

    assert calls == 2
    assert [c.start_ms for c in series.candles] == [0, 60_000, 120_000]
    assert series.unfinished_filtered == 1
