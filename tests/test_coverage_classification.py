from __future__ import annotations

from another_trade.audit.coverage import ProbeClassification, classify_empty
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
            "launchTime": "1000",
            "deliveryTime": "5000",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "1"},
            "fundingInterval": 480,
            "symbolType": "",
            "marketRegion": "",
            "isPreListing": False,
            "preListingInfo": None,
        }
    )


def test_empty_response_is_classified_not_treated_as_zero_volume() -> None:
    item = instrument()
    assert classify_empty(item, 999, first_trade_ms=None) is ProbeClassification.BEFORE_LAUNCH
    assert (
        classify_empty(item, 3000, first_trade_ms=None)
        is ProbeClassification.EMPTY_DURING_LIFETIME
    )
    assert classify_empty(item, 5000, first_trade_ms=None) is ProbeClassification.AFTER_DELIST


def test_empty_after_launch_but_before_first_trade_is_not_missing_data() -> None:
    item = instrument()
    assert (
        classify_empty(item, 3000, first_trade_ms=4000)
        is ProbeClassification.BEFORE_FIRST_TRADE
    )
