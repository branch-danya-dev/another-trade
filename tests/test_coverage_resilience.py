from __future__ import annotations

import json
from pathlib import Path

from another_trade.audit import coverage
from another_trade.audit.coverage import ProbeClassification, run_sample_coverage
from another_trade.audit.inventory import InventorySnapshot
from another_trade.bybit.models import Instrument


class DummyClient:
    pass


def instrument() -> Instrument:
    return Instrument.model_validate(
        {
            "symbol": "XUSDT",
            "contractType": "LinearPerpetual",
            "status": "Trading",
            "baseCoin": "X",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "launchTime": "600000",
            "deliveryTime": "0",
            "priceFilter": {"tickSize": "0.1"},
            "lotSizeFilter": {"qtyStep": "1"},
            "fundingInterval": 480,
            "symbolType": "",
            "marketRegion": "",
            "isPreListing": False,
            "preListingInfo": None,
        }
    )


def test_unexpected_symbol_failure_is_checkpointed_not_raised(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    snapshot = InventorySnapshot(instruments=(instrument(),), sha256="abc")

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(coverage, "probe_symbol", explode)  # type: ignore[attr-defined]

    results = run_sample_coverage(
        DummyClient(),  # type: ignore[arg-type]
        snapshot,
        run_dir=tmp_path,
        now_ms=10_000_000,
    )

    assert len(results) == 1
    assert results[0].funding_probe.classification is ProbeClassification.INTERNAL_ERROR
    assert "unexpected parser failure" in (results[0].funding_probe.error or "")

    lines = (tmp_path / "coverage-sample.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["symbol"] == "XUSDT"
    assert row["funding_probe"]["classification"] == "INTERNAL_ERROR"
