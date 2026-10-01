from __future__ import annotations

import json
from pathlib import Path

import httpx

from another_trade.audit.bulk_pilot import RawPageStore, parse_month
from another_trade.audit.full_download import (
    FULL_DATA_START_MS,
    FullPartitionTask,
    LifetimeRecord,
    build_delisting_announcement_candidates,
    build_partition_plan,
    download_funding_month,
    process_full_partition,
)
from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.models import Instrument


def _instrument(
    symbol: str,
    *,
    base_coin: str,
    launch_ms: int,
    delivery_ms: int = 0,
) -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": symbol,
            "contractType": "LinearPerpetual",
            "status": "Closed" if delivery_ms else "Trading",
            "baseCoin": base_coin,
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": str(launch_ms),
            "deliveryTime": str(delivery_ms),
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "1"},
            "fundingInterval": 480,
            "symbolType": "",
            "marketRegion": "",
            "isPreListing": False,
        }
    )


def test_partition_plan_is_oldest_first_and_respects_lifetime() -> None:
    jan = parse_month("2022-01")
    feb = parse_month("2022-02")
    lifetimes = {
        "AAAUSDT": LifetimeRecord(
            symbol="AAAUSDT",
            status="Trading",
            first_trade_ms=jan.start_ms + 60_000,
            launch_ms=jan.start_ms,
            delivery_ms=0,
            eligible=True,
        ),
        "BBBUSDT": LifetimeRecord(
            symbol="BBBUSDT",
            status="Closed",
            first_trade_ms=FULL_DATA_START_MS,
            launch_ms=FULL_DATA_START_MS,
            delivery_ms=feb.start_ms + 120_000,
            eligible=True,
        ),
    }

    tasks = build_partition_plan(lifetimes)

    keys = [(task.month_start_ms, task.symbol) for task in tasks]
    assert keys == sorted(keys)

    aaa_jan = next(
        task for task in tasks if task.symbol == "AAAUSDT" and task.month == "2022-01"
    )
    assert aaa_jan.data_start_ms == jan.start_ms + 60_000

    bbb_feb = next(
        task for task in tasks if task.symbol == "BBBUSDT" and task.month == "2022-02"
    )
    assert bbb_feb.data_end_ms == feb.start_ms + 120_000


def test_funding_month_splits_only_when_limit_is_hit(tmp_path: Path) -> None:
    bounds = parse_month("2024-06")
    initial_start = bounds.start_ms
    initial_end = bounds.end_ms - 1
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        calls.append((start, end))
        if start == initial_start and end == initial_end:
            events = [
                {
                    "symbol": "TESTUSDT",
                    "fundingRate": "0.0001",
                    "fundingRateTimestamp": str(initial_start + index * 3_600_000),
                }
                for index in range(200)
            ]
        else:
            events = [
                {
                    "symbol": "TESTUSDT",
                    "fundingRate": "0.0001",
                    "fundingRateTimestamp": str(start),
                }
            ]
        payload = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"category": "linear", "list": events},
            "retExtInfo": {},
            "time": 123,
        }
        return httpx.Response(200, json=payload, request=request)

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    raw_path = tmp_path / "TESTUSDT" / "2024-06.sqlite3"
    try:
        with RawPageStore(raw_path, symbol="TESTUSDT", month="2024-06") as store:
            result = download_funding_month(
                client,
                store,
                symbol="TESTUSDT",
                start_ms=bounds.start_ms,
                end_ms=bounds.end_ms,
                now_ms=bounds.end_ms + 86_400_000,
            )
            assert result.stats.new_requests == 3
            assert result.stats.split_windows == 1
            assert store.aux_count() == 3
    finally:
        client.close()

    assert calls[0] == (initial_start, initial_end)
    assert len(calls) == 3


def test_delisting_candidate_uses_publish_time_without_inventing_knowledge() -> None:
    instrument = _instrument(
        "MATICUSDT",
        base_coin="MATIC",
        launch_ms=1,
        delivery_ms=2,
    )
    announcements = [
        {
            "title": "Delisting of MATICUSDT Perpetual Contract",
            "description": "MATIC migration",
            "url": "https://example.invalid/matic",
            "publishTime": 123456,
            "dateTimestamp": 123000,
        }
    ]

    rows = build_delisting_announcement_candidates((instrument,), announcements)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "MATICUSDT"
    assert rows[0]["publish_time_ms"] == 123456
    assert rows[0]["match_kind"] == "EXACT_SYMBOL"


def test_full_partition_never_computes_strategy_metrics(tmp_path: Path) -> None:
    june = parse_month("2024-06")
    start = june.start_ms
    end = start + 2 * 60_000

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v5/market/kline"):
            left = int(request.url.params["start"])
            right = int(request.url.params["end"])
            rows = [
                [str(ts), "10", "10", "10", "10", "0", "0"]
                for ts in range(right, left - 1, -60_000)
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

        if path.endswith("/v5/market/funding/history"):
            payload = {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"category": "linear", "list": []},
                "retExtInfo": {},
                "time": 2,
            }
            return httpx.Response(200, json=payload, request=request)

        raise AssertionError(f"unexpected endpoint: {path}")

    client = BybitPublicClient(
        transport=httpx.MockTransport(handler),
        requests_per_second=None,
    )
    task = FullPartitionTask(
        symbol="TESTUSDT",
        month="2024-06",
        month_start_ms=june.start_ms,
        month_end_ms=june.end_ms,
        data_start_ms=start,
        data_end_ms=end,
        delivery_ms=0,
    )
    try:
        result = process_full_partition(
            client,
            task=task,
            run_dir=tmp_path / "run",
            now_ms=june.end_ms + 86_400_000,
        )
    finally:
        client.close()

    assert result["strategy_metrics_computed"] is False
    assert result["structural_metrics_only"] is False
    assert result["missing_minutes"] == 0
    assert result["funding_event_count"] == 0

    manifest = json.loads(
        (
            tmp_path
            / "run"
            / "partitions"
            / "TESTUSDT"
            / "2024-06.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["strategy_metrics_computed"] is False
