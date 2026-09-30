from __future__ import annotations

import httpx
import pytest

from another_trade.bybit.client import BybitPublicClient


def test_unaligned_kline_boundaries_are_rejected_before_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    try:
        with pytest.raises(ValueError, match="aligned"):
            client.kline_page(
                symbol="BTCUSDT",
                start_ms=500,
                end_ms=60_000,
                interval="1",
                now_ms=1_000_000,
            )
    finally:
        client.close()

    assert calls == 0
