from __future__ import annotations

from pathlib import Path

from another_trade.audit.coverage import (
    DAY_MS,
    MINUTE_MS,
    ProbeClassification,
    _funding_is_applicable,
    _has_lifetime_metadata_conflict,
    _prelaunch_history_probe,
    append_jsonl_fsync,
    discover_first_trade_ms,
)
from another_trade.bybit.client import KlineSeries
from another_trade.bybit.models import Instrument, Kline


def make_instrument(
    *,
    contract_type: str = "LinearPerpetual",
    launch_ms: int = 0,
    delivery_ms: int = 0,
) -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": "XUSDT",
            "contractType": contract_type,
            "status": "Closed" if delivery_ms else "Trading",
            "baseCoin": "X",
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
            "preListingInfo": None,
        }
    )


def series(*starts: int) -> KlineSeries:
    candles = tuple(
        Kline([str(start), "1", "1", "1", "1", "1", "1"]) for start in starts
    )
    return KlineSeries(
        candles=candles,
        duplicate_start_times=0,
        unfinished_filtered=0,
        gaps=(),
        raw_row_count=len(candles),
        oldest_raw_start_ms=min(starts) if starts else None,
        newest_raw_start_ms=max(starts) if starts else None,
    )


class FirstTradeClient:
    def __init__(self, first_day: int, first_minute: int) -> None:
        self.first_day = first_day
        self.first_minute = first_minute
        self.daily_calls: list[tuple[int, int]] = []

    def kline_page(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
        limit: int,
        now_ms: int,
    ) -> KlineSeries:
        del symbol, limit, now_ms
        if interval == "D":
            self.daily_calls.append((start_ms, end_ms))
            if start_ms <= self.first_day <= end_ms:
                return series(self.first_day)
            return series()
        if interval == "1":
            if start_ms <= self.first_minute <= end_ms:
                return series(self.first_minute)
            return series()
        raise AssertionError(interval)


def test_first_trade_search_advances_across_multiple_1000_day_chunks() -> None:
    first_day = 2_100 * DAY_MS
    first_minute = first_day + 5 * MINUTE_MS
    client = FirstTradeClient(first_day, first_minute)
    item = make_instrument(
        launch_ms=0,
        delivery_ms=2_500 * DAY_MS,
    )

    found = discover_first_trade_ms(
        client,  # type: ignore[arg-type]
        item,
        now_ms=3_000 * DAY_MS,
    )

    assert found == first_minute
    assert len(client.daily_calls) == 3
    assert client.daily_calls[0] == (0, 999 * DAY_MS)
    assert client.daily_calls[1] == (1_000 * DAY_MS, 1_999 * DAY_MS)


def test_same_launch_minute_is_not_lifetime_conflict() -> None:
    launch = 44_000
    assert not _has_lifetime_metadata_conflict(
        first_trade_ms=0,
        launch_ms=launch,
        prelaunch_classification=ProbeClassification.BEFORE_LAUNCH,
    )


def test_prior_full_minute_is_lifetime_conflict() -> None:
    launch = 120_044
    assert _has_lifetime_metadata_conflict(
        first_trade_ms=0,
        launch_ms=launch,
        prelaunch_classification=ProbeClassification.BEFORE_LAUNCH,
    )


def test_funding_not_applicable_to_delivery_futures() -> None:
    assert _funding_is_applicable(make_instrument(contract_type="LinearPerpetual"))
    assert not _funding_is_applicable(make_instrument(contract_type="LinearFutures"))


class PrelaunchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []

    def kline_page(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
        limit: int,
        now_ms: int,
    ) -> KlineSeries:
        del symbol, limit, now_ms
        self.calls.append((start_ms, end_ms, interval))
        return series()


def test_prelaunch_probe_scans_1000_complete_days() -> None:
    launch_day = 2_000 * DAY_MS
    item = make_instrument(launch_ms=launch_day + 12 * 60 * 60 * 1000)
    client = PrelaunchClient()

    result = _prelaunch_history_probe(
        client,  # type: ignore[arg-type]
        item,
        now_ms=3_000 * DAY_MS,
    )

    assert result.classification is ProbeClassification.BEFORE_LAUNCH
    assert client.calls == [
        (
            launch_day - 1_000 * DAY_MS,
            launch_day - DAY_MS,
            "D",
        )
    ]


def test_jsonl_bytes_are_lf_only(tmp_path: Path) -> None:
    path = tmp_path / "sample.jsonl"
    append_jsonl_fsync(path, {"a": 1})
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
