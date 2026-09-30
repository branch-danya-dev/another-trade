from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")


class PriceFilter(StrictModel):
    tickSize: str
    minPrice: str | None = None
    maxPrice: str | None = None


class LotSizeFilter(StrictModel):
    qtyStep: str
    minOrderQty: str | None = None
    minNotionalValue: str | None = None
    maxOrderQty: str | None = None
    maxMktOrderQty: str | None = None


class Instrument(StrictModel):
    symbol: str
    contractType: str
    status: str
    baseCoin: str
    quoteCoin: str
    settleCoin: str
    launchTime: str
    deliveryTime: str
    priceFilter: PriceFilter
    lotSizeFilter: LotSizeFilter
    fundingInterval: int | None = None
    symbolType: str = ""
    marketRegion: str = ""
    isPreListing: bool | None = None
    preListingInfo: dict[str, Any] | None = None


class InstrumentsResult(StrictModel):
    category: str
    list: list[Instrument]
    nextPageCursor: str = ""


class InstrumentsEnvelope(StrictModel):
    retCode: int
    retMsg: str
    result: InstrumentsResult
    retExtInfo: dict[str, Any]
    time: int


class KlineResult(StrictModel):
    category: str
    symbol: str
    list: list[list[str]]


class KlineEnvelope(StrictModel):
    retCode: int
    retMsg: str
    result: KlineResult
    retExtInfo: dict[str, Any]
    time: int


class FundingItem(StrictModel):
    symbol: str
    fundingRate: str
    fundingRateTimestamp: str


class FundingResult(StrictModel):
    category: str
    list: list[FundingItem]


class FundingEnvelope(StrictModel):
    retCode: int
    retMsg: str
    result: FundingResult
    retExtInfo: dict[str, Any]
    time: int


class Kline:
    __slots__ = ("start_ms", "open", "high", "low", "close", "volume", "turnover")

    def __init__(self, row: list[str]) -> None:
        if len(row) < 7:
            raise ValueError(f"invalid kline row length: {len(row)}")
        if any(isinstance(item, float) for item in row):
            raise TypeError("float is forbidden in kline payload")
        self.start_ms = int(row[0])
        self.open = Decimal(row[1])
        self.high = Decimal(row[2])
        self.low = Decimal(row[3])
        self.close = Decimal(row[4])
        self.volume = Decimal(row[5])
        self.turnover = Decimal(row[6])

    def as_dict(self) -> dict[str, str | int]:
        return {
            "start_ms": self.start_ms,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "turnover": str(self.turnover),
        }
