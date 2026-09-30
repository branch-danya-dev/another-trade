from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from another_trade.audit.identity import load_identity_config
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
    def identity_relationships(self) -> list[dict[str, object]]:
        config = load_identity_config()
        by_symbol = {item.symbol: item for item in self.instruments}
        out: list[dict[str, object]] = []
        for relationship in config.relationships:
            left = str(relationship["left_symbol"])
            right = str(relationship["right_symbol"])
            relation = str(relationship["relation"])
            left_item = by_symbol.get(left)
            right_item = by_symbol.get(right)
            if left_item is None and right_item is None:
                continue
            left_launch = int(left_item.launchTime) if left_item is not None else None
            left_delivery = (
                int(left_item.deliveryTime or 0) if left_item is not None else None
            )
            right_launch = int(right_item.launchTime) if right_item is not None else None
            right_delivery = (
                int(right_item.deliveryTime or 0) if right_item is not None else None
            )
            overlap_start = (
                max(left_launch, right_launch)
                if left_launch is not None and right_launch is not None
                else None
            )
            left_end = left_delivery if left_delivery and left_delivery > 0 else None
            right_end = right_delivery if right_delivery and right_delivery > 0 else None
            overlap_end_candidates = [value for value in (left_end, right_end) if value]
            overlap_end = min(overlap_end_candidates) if overlap_end_candidates else None
            overlap_ms = (
                max(0, overlap_end - overlap_start)
                if overlap_start is not None and overlap_end is not None
                else None
            )
            out.append(
                {
                    "left_symbol": left,
                    "right_symbol": right,
                    "relation": relation,
                    "left_present": left_item is not None,
                    "right_present": right_item is not None,
                    "left_launch_ms": left_launch,
                    "left_delivery_ms": left_delivery,
                    "right_launch_ms": right_launch,
                    "right_delivery_ms": right_delivery,
                    "metadata_overlap_ms": overlap_ms,
                    "automatic_stitching": False,
                    "confidence": relationship["confidence"],
                    "source_urls": relationship["source_urls"],
                    "evidence_notes": relationship["evidence_notes"],
                    "identity_config_version": config.version,
                    "identity_config_sha256": config.sha256,
                }
            )
        return out

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
        "identity_relationship_config": {
            "version": load_identity_config().version,
            "sha256": load_identity_config().sha256,
        },
        "identity_relationships": snapshot.identity_relationships,
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
