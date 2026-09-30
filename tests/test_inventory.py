from __future__ import annotations

from another_trade.audit.inventory import currently_eligible_crypto_perpetual
from another_trade.bybit.models import Instrument


def instrument(*, symbol_type: str = "", status: str = "Trading") -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": "XUSDT",
            "contractType": "LinearPerpetual",
            "status": status,
            "baseCoin": "X",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": "1000",
            "deliveryTime": "0",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "1"},
            "fundingInterval": 480,
            "symbolType": symbol_type,
            "marketRegion": "",
            "isPreListing": False,
            "preListingInfo": None,
        }
    )


def test_symbol_type_whitelist_excludes_tradfi() -> None:
    assert currently_eligible_crypto_perpetual(instrument(symbol_type=""))
    assert currently_eligible_crypto_perpetual(instrument(symbol_type="innovation"))
    assert not currently_eligible_crypto_perpetual(instrument(symbol_type="commodity"))
    assert not currently_eligible_crypto_perpetual(instrument(symbol_type="forex"))
    assert not currently_eligible_crypto_perpetual(instrument(symbol_type="future-new-type"))


def test_pending_open_is_not_eligible() -> None:
    assert not currently_eligible_crypto_perpetual(instrument(status="PendingOpen"))
