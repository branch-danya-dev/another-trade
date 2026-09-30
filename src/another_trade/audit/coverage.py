from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from another_trade.audit.inventory import InventorySnapshot, currently_eligible_crypto_perpetual
from another_trade.bybit.client import BybitPublicClient, KlineSeries
from another_trade.bybit.errors import BybitError
from another_trade.bybit.models import Instrument
from another_trade.io import atomic_write_bytes


class ProbeClassification(StrEnum):
    DATA_PRESENT = "DATA_PRESENT"
    BEFORE_LAUNCH = "BEFORE_LAUNCH"
    AFTER_DELIST = "AFTER_DELIST"
    EMPTY_DURING_LIFETIME = "EMPTY_DURING_LIFETIME"
    API_ERROR = "API_ERROR"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    symbol: str
    status: str
    checkpoint: str
    checkpoint_ms: int
    window_start_ms: int
    window_end_ms: int
    classification: ProbeClassification
    candle_count: int
    gap_count: int
    duplicate_start_times: int
    unfinished_filtered: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FundingProbeResult:
    symbol: str
    checkpoint_ms: int
    classification: ProbeClassification
    event_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolCoverage:
    symbol: str
    status: str
    launch_ms: int
    delivery_ms: int
    kline_probes: tuple[ProbeResult, ...]
    funding_probe: FundingProbeResult


def classify_empty(instrument: Instrument, checkpoint_ms: int) -> ProbeClassification:
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    if checkpoint_ms < launch:
        return ProbeClassification.BEFORE_LAUNCH
    if delivery > 0 and checkpoint_ms >= delivery:
        return ProbeClassification.AFTER_DELIST
    return ProbeClassification.EMPTY_DURING_LIFETIME


def _probe_points(instrument: Instrument, now_ms: int) -> tuple[tuple[str, int], ...]:
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    effective_end = delivery if delivery > launch else now_ms
    near_launch = min(effective_end - 120_000, launch + 60 * 60_000)
    midpoint = launch + max(0, effective_end - launch) // 2
    near_end = max(launch + 120_000, effective_end - 60 * 60_000)
    return (
        ("near_launch", near_launch),
        ("mid_life", midpoint),
        ("near_end", near_end),
    )


def _probe_window(point_ms: int) -> tuple[int, int]:
    half = 15 * 60_000
    return point_ms - half, point_ms + half


def _series_result(
    instrument: Instrument,
    label: str,
    point_ms: int,
    start_ms: int,
    end_ms: int,
    series: KlineSeries,
) -> ProbeResult:
    classification = (
        ProbeClassification.DATA_PRESENT
        if series.candles
        else classify_empty(instrument, point_ms)
    )
    return ProbeResult(
        symbol=instrument.symbol,
        status=instrument.status,
        checkpoint=label,
        checkpoint_ms=point_ms,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        classification=classification,
        candle_count=len(series.candles),
        gap_count=len(series.gaps),
        duplicate_start_times=series.duplicate_start_times,
        unfinished_filtered=series.unfinished_filtered,
    )


def probe_symbol(
    client: BybitPublicClient,
    instrument: Instrument,
    *,
    now_ms: int,
) -> SymbolCoverage:
    probes: list[ProbeResult] = []
    for label, point_ms in _probe_points(instrument, now_ms):
        start_ms, end_ms = _probe_window(point_ms)
        try:
            series = client.kline_page(
                symbol=instrument.symbol,
                start_ms=start_ms,
                end_ms=end_ms,
                interval="1",
                limit=100,
                now_ms=now_ms,
            )
            probes.append(
                _series_result(instrument, label, point_ms, start_ms, end_ms, series)
            )
        except BybitError as exc:
            probes.append(
                ProbeResult(
                    symbol=instrument.symbol,
                    status=instrument.status,
                    checkpoint=label,
                    checkpoint_ms=point_ms,
                    window_start_ms=start_ms,
                    window_end_ms=end_ms,
                    classification=ProbeClassification.API_ERROR,
                    candle_count=0,
                    gap_count=0,
                    duplicate_start_times=0,
                    unfinished_filtered=0,
                    error=str(exc),
                )
            )

    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    effective_end = delivery if delivery > launch else now_ms
    funding_point = launch + max(0, effective_end - launch) // 2
    funding_start = max(launch, funding_point - 4 * 24 * 60 * 60_000)
    funding_end = min(effective_end - 1, funding_point + 4 * 24 * 60 * 60_000)
    try:
        events = client.funding_page(
            symbol=instrument.symbol,
            start_ms=funding_start,
            end_ms=funding_end,
            limit=200,
            now_ms=now_ms,
        )
        funding_class = (
            ProbeClassification.DATA_PRESENT
            if events
            else classify_empty(instrument, funding_point)
        )
        funding_probe = FundingProbeResult(
            symbol=instrument.symbol,
            checkpoint_ms=funding_point,
            classification=funding_class,
            event_count=len(events),
        )
    except BybitError as exc:
        funding_probe = FundingProbeResult(
            symbol=instrument.symbol,
            checkpoint_ms=funding_point,
            classification=ProbeClassification.API_ERROR,
            event_count=0,
            error=str(exc),
        )

    return SymbolCoverage(
        symbol=instrument.symbol,
        status=instrument.status,
        launch_ms=launch,
        delivery_ms=delivery,
        kline_probes=tuple(probes),
        funding_probe=funding_probe,
    )


def sample_coverage(
    client: BybitPublicClient,
    inventory: InventorySnapshot,
    *,
    now_ms: int | None = None,
    max_symbols: int | None = None,
) -> tuple[SymbolCoverage, ...]:
    now_value = int(time.time() * 1000) if now_ms is None else now_ms
    instruments = [
        item for item in inventory.instruments if currently_eligible_crypto_perpetual(item)
    ]
    if max_symbols is not None:
        instruments = instruments[:max_symbols]
    return tuple(probe_symbol(client, item, now_ms=now_value) for item in instruments)


def write_coverage(results: tuple[SymbolCoverage, ...], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    json_doc = []
    for result in results:
        row = asdict(result)
        row["kline_probes"] = [asdict(probe) for probe in result.kline_probes]
        row["funding_probe"] = asdict(result.funding_probe)
        json_doc.append(row)
    atomic_write_bytes(
        directory / "coverage-sample.json",
        json.dumps(json_doc, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode(
            "utf-8"
        ),
    )

    csv_path = directory / "coverage-sample.csv"
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "status",
                "checkpoint",
                "classification",
                "candle_count",
                "gap_count",
                "duplicates",
                "unfinished_filtered",
                "error",
            ]
        )
        for result in results:
            for probe in result.kline_probes:
                writer.writerow(
                    [
                        result.symbol,
                        result.status,
                        probe.checkpoint,
                        probe.classification,
                        probe.candle_count,
                        probe.gap_count,
                        probe.duplicate_start_times,
                        probe.unfinished_filtered,
                        probe.error or "",
                    ]
                )
    tmp.replace(csv_path)

    summary = {
        "symbols": len(results),
        "trading": sum(item.status == "Trading" for item in results),
        "closed": sum(item.status == "Closed" for item in results),
        "kline_probe_counts": {
            classification.value: sum(
                probe.classification == classification
                for item in results
                for probe in item.kline_probes
            )
            for classification in ProbeClassification
        },
        "funding_probe_counts": {
            classification.value: sum(
                item.funding_probe.classification == classification for item in results
            )
            for classification in ProbeClassification
        },
    }
    atomic_write_bytes(
        directory / "coverage-summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )
