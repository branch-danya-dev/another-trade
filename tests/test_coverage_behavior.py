from __future__ import annotations

from pathlib import Path

from another_trade.audit.coverage import (
    ProbeClassification,
    _probe_points,
    _probe_window,
    append_jsonl_fsync,
    load_jsonl_results,
)
from another_trade.bybit.models import Instrument


def instrument() -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": "XUSDT",
            "contractType": "LinearPerpetual",
            "status": "Closed",
            "baseCoin": "X",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": "600000",
            "deliveryTime": "7200000",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "1"},
            "fundingInterval": 480,
            "symbolType": "",
            "marketRegion": "",
            "isPreListing": False,
            "preListingInfo": None,
        }
    )


def test_probe_window_is_aligned_and_has_exactly_30_expected_minutes() -> None:
    start, end = _probe_window(1_234_567)
    assert start % 60_000 == 0
    assert end % 60_000 == 0
    assert ((end - start) // 60_000) + 1 == 30


def test_probe_points_use_first_trade_not_launch_time() -> None:
    points = _probe_points(instrument(), 9_000_000, first_trade_ms=1_800_000)
    labels = [label for label, _ in points]
    assert labels[0] == "near_first_trade"
    assert points[0][1] == 1_800_000 + 60 * 60_000


def test_jsonl_checkpoint_round_trip(tmp_path: Path) -> None:
    row = {
        "symbol": "XUSDT",
        "status": "Trading",
        "symbol_type": "",
        "eligible_now": True,
        "launch_ms": 1,
        "launch_time_quality": "OBSERVED_METADATA",
        "first_trade_ms": 2,
        "launch_to_first_trade_ms": 1,
        "delivery_ms": 0,
        "lifetime_metadata_conflict": False,
        "kline_probes": [],
        "funding_probe": {
            "symbol": "XUSDT",
            "checkpoint_ms": 3,
            "classification": ProbeClassification.API_ERROR,
            "event_count": 0,
            "error": "x",
        },
    }
    path = tmp_path / "coverage.jsonl"
    append_jsonl_fsync(path, row)
    loaded = load_jsonl_results(path)
    assert len(loaded) == 1
    assert loaded[0].symbol == "XUSDT"
    assert loaded[0].funding_probe.classification is ProbeClassification.API_ERROR
