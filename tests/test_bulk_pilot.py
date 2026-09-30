from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from another_trade.audit.bulk_pilot import (
    DAY_MS,
    MINUTE_MS,
    PageWindow,
    PartitionStatus,
    RawPageStore,
    aggregate_candles,
    logical_content_sha256,
    missing_minute_starts,
    page_windows,
    parse_month,
    partition_status,
    write_partition_parquet,
)
from another_trade.bybit.models import Kline


def candle(
    start_ms: int,
    *,
    price: str = "100",
    volume: str = "1",
    turnover: str = "100",
) -> Kline:
    return Kline(
        [
            str(start_ms),
            price,
            price,
            price,
            price,
            volume,
            turnover,
        ]
    )


def test_june_2024_page_windows_cover_every_minute_once() -> None:
    bounds = parse_month("2024-06")
    windows = list(page_windows(bounds))
    assert len(windows) == 44
    assert windows[0].start_ms == bounds.start_ms
    assert windows[-1].end_ms == bounds.end_ms - MINUTE_MS

    starts: list[int] = []
    for window in windows:
        starts.extend(range(window.start_ms, window.end_ms + MINUTE_MS, MINUTE_MS))
    assert len(starts) == 30 * 24 * 60
    assert len(set(starts)) == len(starts)


def test_partition_requires_24h_after_month_end_to_seal() -> None:
    bounds = parse_month("2024-06")
    assert partition_status(bounds, bounds.end_ms + DAY_MS - 1) is PartitionStatus.OPEN
    assert partition_status(bounds, bounds.end_ms + DAY_MS) is PartitionStatus.SEALED


def test_raw_page_store_round_trip_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT" / "2024-06.sqlite3"
    window = PageWindow(start_ms=0, end_ms=60_000)
    params = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "interval": "1",
        "start": 0,
        "end": 60_000,
        "limit": 1000,
    }
    raw = b'{"retCode":0,"retMsg":"OK","result":{"list":[]}}'

    with RawPageStore(path, symbol="BTCUSDT", month="2024-06") as store:
        store.put_page(
            window=window,
            endpoint="/v5/market/kline",
            params=params,
            raw=raw,
            raw_rows=0,
            captured_at_ms=1,
        )
        first_hash = store.logical_index_sha256()
        assert store.has_page(0)
        assert store.read_raw(0) == raw

    with RawPageStore(path, symbol="BTCUSDT", month="2024-06") as resumed:
        assert resumed.page_count() == 1
        assert resumed.read_raw(0) == raw
        assert resumed.logical_index_sha256() == first_hash
        resumed.put_page(
            window=window,
            endpoint="/v5/market/kline",
            params=params,
            raw=raw,
            raw_rows=0,
            captured_at_ms=999,
        )
        assert resumed.page_count() == 1
        assert resumed.logical_index_sha256() == first_hash


def test_present_zero_volume_flat_candle_is_not_gap() -> None:
    bounds = parse_month("2024-06")
    minute = candle(
        bounds.start_ms,
        price="10",
        volume="0",
        turnover="0",
    )
    missing = missing_minute_starts(
        type(bounds)(
            label=bounds.label,
            start_ms=bounds.start_ms,
            end_ms=bounds.start_ms + MINUTE_MS,
        ),
        [minute],
    )
    assert missing == []


def test_15m_aggregation_keeps_zero_volume_minutes() -> None:
    candles = []
    for index in range(15):
        candles.append(
            candle(
                index * MINUTE_MS,
                price=str(100 + index),
                volume="0" if index == 7 else "1",
                turnover="0" if index == 7 else str(100 + index),
            )
        )

    bars = aggregate_candles(candles, interval="15")
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == Decimal("100")
    assert bar.close == Decimal("114")
    assert bar.volume == Decimal("14")


def test_parquet_rewrite_has_same_logical_and_file_hash(tmp_path: Path) -> None:
    candles = [
        candle(0, price="100.1", volume="2.5", turnover="250.25"),
        candle(MINUTE_MS, price="100.2", volume="0", turnover="0"),
    ]
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"

    logical_a, file_a = write_partition_parquet(
        symbol="BTCUSDT",
        candles=candles,
        path=a,
    )
    logical_b, file_b = write_partition_parquet(
        symbol="BTCUSDT",
        candles=candles,
        path=b,
    )

    assert logical_a == logical_b == logical_content_sha256("BTCUSDT", candles)
    assert file_a == file_b
