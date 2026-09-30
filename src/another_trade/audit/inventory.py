from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.errors import InventoryConflictError
from another_trade.bybit.models import Instrument
from another_trade.io import atomic_write_bytes


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    instruments: tuple[Instrument, ...]
    sha256: str

    @property
    def trading_count(self) -> int:
        return sum(item.status == "Trading" for item in self.instruments)

    @property
    def closed_count(self) -> int:
        return sum(item.status == "Closed" for item in self.instruments)


def currently_eligible_crypto_perpetual(item: Instrument) -> bool:
    return (
        item.contractType == "LinearPerpetual"
        and item.quoteCoin == "USDT"
        and item.settleCoin == "USDT"
        and item.status in {"Trading", "Closed"}
        and item.isPreListing is not True
        and item.marketRegion == ""
    )


def collect_inventory(client: BybitPublicClient) -> InventorySnapshot:
    trading = client.instruments("Trading")
    closed = client.instruments("Closed")

    by_symbol: dict[str, Instrument] = {}
    for item in [*trading, *closed]:
        if item.symbol in by_symbol:
            other = by_symbol[item.symbol]
            raise InventoryConflictError(
                f"duplicate symbol across inventory pages/statuses: "
                f"{item.symbol} ({other.status}, {item.status})"
            )
        by_symbol[item.symbol] = item

    instruments = tuple(by_symbol[key] for key in sorted(by_symbol))
    payload = [
        {
            **item.model_dump(mode="json"),
            "current_crypto_perpetual_eligible": currently_eligible_crypto_perpetual(item),
        }
        for item in instruments
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return InventorySnapshot(instruments=instruments, sha256=hashlib.sha256(raw).hexdigest())


def write_inventory(snapshot: InventorySnapshot, path: Path) -> None:
    doc = {
        "sha256": snapshot.sha256,
        "trading_count": snapshot.trading_count,
        "closed_count": snapshot.closed_count,
        "instruments": [
            {
                **item.model_dump(mode="json"),
                "current_crypto_perpetual_eligible": currently_eligible_crypto_perpetual(item),
            }
            for item in snapshot.instruments
        ],
    }
    atomic_write_bytes(
        path,
        json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )
