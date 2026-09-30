from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.errors import InventoryConflictError
from another_trade.bybit.models import Instrument
from another_trade.io import atomic_write_bytes

ALLOWED_CRYPTO_SYMBOL_TYPES = {"", "innovation"}


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    instruments: tuple[Instrument, ...]
    sha256: str

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.status for item in self.instruments).items()))

    @property
    def symbol_type_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.symbolType for item in self.instruments).items()))

    @property
    def trading_count(self) -> int:
        return self.status_counts.get("Trading", 0)

    @property
    def closed_count(self) -> int:
        return self.status_counts.get("Closed", 0)


def currently_eligible_crypto_perpetual(item: Instrument) -> bool:
    return (
        item.contractType == "LinearPerpetual"
        and item.quoteCoin == "USDT"
        and item.settleCoin == "USDT"
        and item.status in {"Trading", "Closed"}
        and item.isPreListing is not True
        and item.marketRegion == ""
        and item.symbolType.casefold() in ALLOWED_CRYPTO_SYMBOL_TYPES
    )


def should_sample_for_audit(item: Instrument) -> bool:
    return (
        currently_eligible_crypto_perpetual(item)
        or "OLD" in item.symbol.upper()
        or item.status not in {"Trading", "Closed"}
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
            "audit_sample_candidate": should_sample_for_audit(item),
        }
        for item in instruments
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return InventorySnapshot(instruments=instruments, sha256=hashlib.sha256(raw).hexdigest())


def write_inventory(snapshot: InventorySnapshot, path: Path) -> None:
    doc = {
        "sha256": snapshot.sha256,
        "total_count": len(snapshot.instruments),
        "status_counts": snapshot.status_counts,
        "symbol_type_counts": snapshot.symbol_type_counts,
        "eligible_count": sum(
            currently_eligible_crypto_perpetual(item) for item in snapshot.instruments
        ),
        "audit_sample_candidate_count": sum(
            should_sample_for_audit(item) for item in snapshot.instruments
        ),
        "instruments": [
            {
                **item.model_dump(mode="json"),
                "current_crypto_perpetual_eligible": currently_eligible_crypto_perpetual(item),
                "audit_sample_candidate": should_sample_for_audit(item),
            }
            for item in snapshot.instruments
        ],
    }
    atomic_write_bytes(
        path,
        json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )
