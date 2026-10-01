from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from another_trade.audit.bulk_pilot import (
    DAY_MS,
    MINUTE_MS,
    PageWindow,
    PartitionStatus,
    RawPageStore,
    aggregate_candles,
    compare_pilot_runs,
    download_symbol_month,
    logical_content_sha256,
    missing_minute_starts,
    page_windows,
    parse_month,
    partition_status,
    payload_sha256,
    write_partition_parquet,
)
from another_trade.bybit.client import BybitPublicClient
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




def test_decimal128_max_precision_value_hashes_and_writes(tmp_path: Path) -> None:
    value = "12345678901234567890.123456789012345678"
    candles = [
        candle(
            0,
            price="1.000000000000000000",
            volume=value,
            turnover=value,
        )
    ]
    path = tmp_path / "max-precision.parquet"

    logical_hash, file_hash = write_partition_parquet(
        symbol="TESTUSDT",
        candles=candles,
        path=path,
    )

    assert len(logical_hash) == 64
    assert len(file_hash) == 64
    assert path.exists()
    table = pq.read_table(path)
    assert table["volume"][0].as_py() == Decimal(value)
    assert table["turnover"][0].as_py() == Decimal(value)

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



def test_aux_raw_store_uses_endpoint_params_identity(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT" / "2024-06.sqlite3"
    params_a = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "interval": "60",
        "start": 0,
        "end": 3_600_000,
        "limit": 1000,
    }
    params_b = {
        **params_a,
        "interval": "1",
        "end": 0,
        "limit": 1,
    }
    raw_a = b'{"retCode":0,"retMsg":"OK","result":{"list":[["0","1","1","1","1"]]}}'
    raw_b = b'{"retCode":0,"retMsg":"OK","result":{"list":[["0","2","2","2","2"]]}}'

    with RawPageStore(path, symbol="BTCUSDT", month="2024-06") as store:
        store.put_aux(
            stream="mark:60",
            range_start_ms=0,
            range_end_ms=3_600_000,
            endpoint="/v5/market/mark-price-kline",
            params=params_a,
            raw=raw_a,
            raw_rows=1,
            captured_at_ms=10,
        )
        first_hash = store.aux_index_sha256()
        store.put_aux(
            stream="mark:1:funding",
            range_start_ms=0,
            range_end_ms=0,
            endpoint="/v5/market/mark-price-kline",
            params=params_b,
            raw=raw_b,
            raw_rows=1,
            captured_at_ms=11,
        )
        assert store.aux_count() == 2
        assert store.read_aux(
            endpoint="/v5/market/mark-price-kline",
            params=params_a,
        ) == raw_a
        assert store.read_aux(
            endpoint="/v5/market/mark-price-kline",
            params=params_b,
        ) == raw_b
        assert store.aux_index_sha256() != first_hash


def test_parquet_schema_does_not_repeat_symbol_column(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT" / "2024-06.parquet"
    write_partition_parquet(
        symbol="BTCUSDT",
        candles=[candle(0), candle(MINUTE_MS)],
        path=path,
    )
    table = pq.read_table(path)
    assert "symbol" not in table.column_names
    assert table.column_names == [
        "start_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    ]


def _write_fake_pilot_run(
    root: Path,
    *,
    logical_suffix: str = "",
) -> None:
    manifest = {
        "month": "2024-06",
        "selected_symbols": ["BTCUSDT", "ETHUSDT"],
    }
    (root / "partitions" / "BTCUSDT").mkdir(parents=True)
    (root / "partitions" / "ETHUSDT").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for symbol in manifest["selected_symbols"]:
        row = {
            "logical_content_sha256": f"logical-{symbol}{logical_suffix}",
            "parquet_file_sha256": f"parquet-{symbol}",
        }
        (root / "partitions" / symbol / "2024-06.json").write_text(
            json.dumps(row),
            encoding="utf-8",
        )


def test_clean_run_comparison_detects_logical_drift(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    clean = tmp_path / "clean"
    _write_fake_pilot_run(reference)
    _write_fake_pilot_run(clean)

    same = compare_pilot_runs(reference, clean)
    assert same["all_logical_equal"] is True
    assert same["all_parquet_equal"] is True

    row_path = clean / "partitions" / "ETHUSDT" / "2024-06.json"
    row = json.loads(row_path.read_text(encoding="utf-8"))
    row["logical_content_sha256"] = "different"
    row_path.write_text(json.dumps(row), encoding="utf-8")

    changed = compare_pilot_runs(reference, clean)
    assert changed["all_logical_equal"] is False


def test_partial_month_download_uses_only_requested_lifetime_range(tmp_path: Path) -> None:
    bounds = parse_month("2024-09")
    data_start = bounds.start_ms + DAY_MS
    data_end = data_start + 2 * MINUTE_MS

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params
        start = int(query["start"])
        end = int(query["end"])
        rows = []
        cursor = end
        while cursor >= start:
            rows.append(
                [
                    str(cursor),
                    "10",
                    "10",
                    "10",
                    "10",
                    "0",
                    "0",
                ]
            )
            cursor -= MINUTE_MS
        payload = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "symbol": "TESTUSDT",
                "list": rows,
            },
            "retExtInfo": {},
            "time": 1,
        }
        return httpx.Response(200, json=payload, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    try:
        artifact = download_symbol_month(
            client,
            symbol="TESTUSDT",
            bounds=bounds,
            run_dir=tmp_path / "run",
            now_ms=bounds.end_ms + 2 * DAY_MS,
            data_start_ms=data_start,
            data_end_ms=data_end,
        )
    finally:
        client.close()

    assert artifact.data_start_ms == data_start
    assert artifact.data_end_ms == data_end
    assert artifact.expected_minutes == 2
    assert artifact.actual_minutes == 2
    assert artifact.missing_minutes == 0
    assert artifact.first_start_ms == data_start
    assert artifact.last_start_ms == data_start + MINUTE_MS

    raw_path = tmp_path / "run" / "raw" / "TESTUSDT" / "2024-09.sqlite3"
    conn = sqlite3.connect(raw_path)
    try:
        captured = conn.execute(
            "SELECT captured_at_ms FROM responses"
        ).fetchone()[0]
    finally:
        conn.close()
    assert captured != bounds.end_ms + 2 * DAY_MS



def test_payload_hash_ignores_top_level_server_time() -> None:
    first = (
        b'{"retCode":0,"retMsg":"OK","result":{"list":[["1","2"]]},'
        b'"retExtInfo":{},"time":111}'
    )
    second = (
        b'{"time":999,"result":{"list":[["1","2"]]},'
        b'"retMsg":"OK","retCode":0,"retExtInfo":{}}'
    )

    assert hashlib.sha256(first).hexdigest() != hashlib.sha256(second).hexdigest()
    assert payload_sha256(first) == payload_sha256(second)


def test_payload_hash_changes_when_result_changes() -> None:
    first = b'{"retCode":0,"retMsg":"OK","result":{"value":"1"},"time":1}'
    second = b'{"retCode":0,"retMsg":"OK","result":{"value":"2"},"time":1}'
    assert payload_sha256(first) != payload_sha256(second)


def test_raw_store_persists_raw_and_payload_hashes(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT" / "2024-06.sqlite3"
    raw = b'{"retCode":0,"retMsg":"OK","result":{"list":[]},"time":123}'
    params = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "interval": "1",
        "start": 0,
        "end": 0,
        "limit": 1000,
    }

    with RawPageStore(path, symbol="BTCUSDT", month="2024-06") as store:
        store.put_page(
            window=PageWindow(start_ms=0, end_ms=0),
            endpoint="/v5/market/kline",
            params=params,
            raw=raw,
            raw_rows=0,
            captured_at_ms=10,
        )
        assert len(store.payload_index_sha256()) == 64

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT raw_sha256, payload_sha256 FROM responses"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == hashlib.sha256(raw).hexdigest()
    assert row[1] == payload_sha256(raw)


def test_bulk_store_batches_commits_until_threshold(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT" / "2024-06.sqlite3"
    raw = b'{"retCode":0,"retMsg":"OK","result":{"list":[]},"time":1}'
    params = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "interval": "1",
        "start": 0,
        "end": 0,
        "limit": 1000,
    }

    with RawPageStore(
        path,
        symbol="BTCUSDT",
        month="2024-06",
        profile="bulk",
        commit_every=3,
    ) as store:
        for index in range(2):
            store.put_page(
                window=PageWindow(
                    start_ms=index * MINUTE_MS,
                    end_ms=index * MINUTE_MS,
                ),
                endpoint="/v5/market/kline",
                params={**params, "start": index * MINUTE_MS, "end": index * MINUTE_MS},
                raw=raw,
                raw_rows=0,
                captured_at_ms=index,
            )

        reader = sqlite3.connect(path)
        try:
            count_before = reader.execute(
                "SELECT COUNT(*) FROM responses"
            ).fetchone()[0]
        finally:
            reader.close()
        assert count_before == 0

        store.put_page(
            window=PageWindow(
                start_ms=2 * MINUTE_MS,
                end_ms=2 * MINUTE_MS,
            ),
            endpoint="/v5/market/kline",
            params={
                **params,
                "start": 2 * MINUTE_MS,
                "end": 2 * MINUTE_MS,
            },
            raw=raw,
            raw_rows=0,
            captured_at_ms=2,
        )

        reader = sqlite3.connect(path)
        try:
            count_after = reader.execute(
                "SELECT COUNT(*) FROM responses"
            ).fetchone()[0]
        finally:
            reader.close()
        assert count_after == 3


def test_parallel_kline_fetch_produces_complete_partition(tmp_path: Path) -> None:
    start = parse_month("2024-06").start_ms
    minute_count = 2_001
    bounds = type(parse_month("2024-06"))(
        label="2024-06",
        start_ms=start,
        end_ms=start + minute_count * MINUTE_MS,
    )
    lock = threading.Lock()
    active = 0
    max_active = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            left = int(request.url.params["start"])
            right = int(request.url.params["end"])
            rows = [
                [str(ts), "10", "10", "10", "10", "1", "10"]
                for ts in range(right, left - 1, -MINUTE_MS)
            ]
            payload = {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "category": "linear",
                    "symbol": "TESTUSDT",
                    "list": rows,
                },
                "retExtInfo": {},
                "time": 1,
            }
            return httpx.Response(200, json=payload, request=request)
        finally:
            with lock:
                active -= 1

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    try:
        artifact = download_symbol_month(
            client,
            symbol="TESTUSDT",
            bounds=bounds,
            run_dir=tmp_path / "run",
            now_ms=bounds.end_ms + DAY_MS,
            raw_store_profile="bulk",
            raw_commit_every=2,
            fetch_workers=3,
        )
    finally:
        client.close()

    assert artifact.expected_minutes == minute_count
    assert artifact.actual_minutes == minute_count
    assert artifact.missing_minutes == 0
    assert artifact.raw_page_count == 3
    assert max_active >= 2
