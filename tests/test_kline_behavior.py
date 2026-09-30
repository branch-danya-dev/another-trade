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


def test_reverse_order_is_sorted_and_duplicates_removed() -> None:
    raw = envelope(
        [
            ["120000", "1", "1", "1", "1", "1", "1"],
            ["60000", "1", "1", "1", "1", "1", "1"],
            ["60000", "1", "1", "1", "1", "1", "1"],
            ["0", "1", "1", "1", "1", "1", "1"],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(transport=httpx.MockTransport(handler), requests_per_second=None)
    try:
        page = client.kline_page(
            symbol="BTCUSDT", start_ms=0, end_ms=120000, now_ms=500000
        )
    finally:
        client.close()

    assert [c.start_ms for c in page.candles] == [0, 60000, 120000]
    assert page.duplicate_start_times == 1
    assert page.gaps == ()


def test_unfinished_candle_is_filtered() -> None:
    raw = envelope(
        [
            ["120000", "1", "1", "1", "1", "1", "1"],
            ["60000", "1", "1", "1", "1", "1", "1"],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(transport=httpx.MockTransport(handler), requests_per_second=None)
    try:
        page = client.kline_page(
            symbol="BTCUSDT",
            start_ms=60000,
            end_ms=120000,
            now_ms=150000,
        )
    finally:
        client.close()

    assert [c.start_ms for c in page.candles] == [60000]
    assert page.unfinished_filtered == 1


def test_gap_is_reported_not_forward_filled() -> None:
    raw = envelope(
        [
            ["120000", "1", "1", "1", "1", "1", "1"],
            ["0", "1", "1", "1", "1", "1", "1"],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, request=request)

    client = BybitPublicClient(transport=httpx.MockTransport(handler), requests_per_second=None)
    try:
        page = client.kline_page(
            symbol="BTCUSDT", start_ms=0, end_ms=120000, now_ms=500000
        )
    finally:
        client.close()

    assert page.gaps == ((60000, 120000),)
