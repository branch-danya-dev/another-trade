from __future__ import annotations

from pathlib import Path

import pytest

from another_trade.cache import ImmutableResponseCache


def test_cache_key_includes_every_request_parameter(tmp_path: Path) -> None:
    cache = ImmutableResponseCache(tmp_path)
    a = cache.key(
        base_url="https://api.bybit.com",
        method="GET",
        path="/v5/market/kline",
        params={"symbol": "BTCUSDT", "interval": 1, "end": 100},
    )
    b = cache.key(
        base_url="https://api.bybit.com",
        method="GET",
        path="/v5/market/kline",
        params={"symbol": "BTCUSDT", "interval": 1, "end": 101},
    )
    assert a != b


def test_cache_verifies_raw_body_hash(tmp_path: Path) -> None:
    cache = ImmutableResponseCache(tmp_path)
    key = cache.key(base_url="u", method="GET", path="/x", params={"a": 1})
    cached = cache.put(key=key, raw=b'{"ok":true}', request_metadata={"a": 1})
    assert cache.get(key) == cached

    body, _ = cache._paths(key)  # noqa: SLF001
    body.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="cache corruption"):
        cache.get(key)
