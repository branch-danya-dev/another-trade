from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from pathlib import Path

from another_trade.audit.inventory import (
    InventorySnapshot,
    currently_eligible_crypto_perpetual,
    should_sample_for_audit,
)
from another_trade.bybit.client import BybitPublicClient, KlineSeries
from another_trade.bybit.models import Instrument
from another_trade.io import atomic_write_bytes
from another_trade.time import align_down_ms

MINUTE_MS = 60_000
DAY_MS = 86_400_000


class ProbeClassification(StrEnum):
    DATA_PRESENT = "DATA_PRESENT"
    PARTIAL_DATA = "PARTIAL_DATA"
    BEFORE_LAUNCH = "BEFORE_LAUNCH"
    BEFORE_FIRST_TRADE = "BEFORE_FIRST_TRADE"
    AFTER_DELIST = "AFTER_DELIST"
    EMPTY_DURING_LIFETIME = "EMPTY_DURING_LIFETIME"
    LIFETIME_METADATA_CONFLICT = "LIFETIME_METADATA_CONFLICT"
    FIRST_TRADE_NOT_FOUND = "FIRST_TRADE_NOT_FOUND"
    API_ERROR = "API_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    symbol: str
    status: str
    checkpoint: str
    checkpoint_ms: int
    window_start_ms: int
    window_end_ms: int
    classification: ProbeClassification
    expected_count: int
    candle_count: int
    coverage_pct: str
    missing_expected_count: int
    missing_at_start: bool
    missing_at_end: bool
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
    symbol_type: str
    eligible_now: bool
    launch_ms: int
    first_trade_ms: int | None
    launch_to_first_trade_ms: int | None
    delivery_ms: int
    lifetime_metadata_conflict: bool
    kline_probes: tuple[ProbeResult, ...]
    funding_probe: FundingProbeResult


def _funding_is_applicable(instrument: Instrument) -> bool:
    return instrument.contractType == "LinearPerpetual"


def _has_lifetime_metadata_conflict(
    *,
    first_trade_ms: int | None,
    launch_ms: int,
    prelaunch_classification: ProbeClassification,
) -> bool:
    launch_minute = align_down_ms(max(0, launch_ms), "1")
    return (
        first_trade_ms is not None and first_trade_ms < launch_minute
    ) or prelaunch_classification is ProbeClassification.LIFETIME_METADATA_CONFLICT


def _coverage_pct(actual: int, expected: int) -> str:
    if expected <= 0:
        return "0"
    value = (Decimal(actual) * Decimal(100) / Decimal(expected)).quantize(
        Decimal("0.001"),
        rounding=ROUND_HALF_EVEN,
    )
    return format(value, "f")


def classify_empty(
    instrument: Instrument,
    checkpoint_ms: int,
    *,
    first_trade_ms: int | None,
) -> ProbeClassification:
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    if checkpoint_ms < launch:
        return ProbeClassification.BEFORE_LAUNCH
    if first_trade_ms is not None and checkpoint_ms < first_trade_ms:
        return ProbeClassification.BEFORE_FIRST_TRADE
    if delivery > 0 and checkpoint_ms >= delivery:
        return ProbeClassification.AFTER_DELIST
    return ProbeClassification.EMPTY_DURING_LIFETIME


def _probe_points(
    instrument: Instrument,
    now_ms: int,
    *,
    first_trade_ms: int | None,
) -> tuple[tuple[str, int], ...]:
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    origin = first_trade_ms if first_trade_ms is not None else launch
    effective_end = delivery if delivery > origin else now_ms
    near_origin = min(effective_end - 2 * MINUTE_MS, origin + 60 * MINUTE_MS)
    midpoint = origin + max(0, effective_end - origin) // 2
    near_end = max(origin + 2 * MINUTE_MS, effective_end - 60 * MINUTE_MS)
    return (
        ("near_first_trade" if first_trade_ms is not None else "near_launch", near_origin),
        ("mid_life", midpoint),
        ("near_end", near_end),
    )


def _probe_window(point_ms: int, *, count: int = 30) -> tuple[int, int]:
    if count < 1:
        raise ValueError("count must be >= 1")
    center = align_down_ms(point_ms, "1")
    left = count // 2
    start = center - left * MINUTE_MS
    end = start + (count - 1) * MINUTE_MS
    return start, end


def _series_result(
    instrument: Instrument,
    label: str,
    point_ms: int,
    start_ms: int,
    end_ms: int,
    series: KlineSeries,
    *,
    first_trade_ms: int | None,
) -> ProbeResult:
    expected = ((end_ms - start_ms) // MINUTE_MS) + 1
    starts = {candle.start_ms for candle in series.candles}
    expected_starts = range(start_ms, end_ms + MINUTE_MS, MINUTE_MS)
    missing = sum(start not in starts for start in expected_starts)

    if not series.candles:
        classification = classify_empty(
            instrument,
            point_ms,
            first_trade_ms=first_trade_ms,
        )
    elif missing == 0 and not series.gaps:
        classification = ProbeClassification.DATA_PRESENT
    else:
        classification = ProbeClassification.PARTIAL_DATA

    return ProbeResult(
        symbol=instrument.symbol,
        status=instrument.status,
        checkpoint=label,
        checkpoint_ms=point_ms,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        classification=classification,
        expected_count=expected,
        candle_count=len(series.candles),
        coverage_pct=_coverage_pct(len(series.candles), expected),
        missing_expected_count=missing,
        missing_at_start=start_ms not in starts,
        missing_at_end=end_ms not in starts,
        gap_count=len(series.gaps),
        duplicate_start_times=series.duplicate_start_times,
        unfinished_filtered=series.unfinished_filtered,
    )


def _error_probe(
    instrument: Instrument,
    *,
    label: str,
    point_ms: int,
    start_ms: int,
    end_ms: int,
    classification: ProbeClassification,
    exc: Exception,
) -> ProbeResult:
    expected = ((end_ms - start_ms) // MINUTE_MS) + 1
    return ProbeResult(
        symbol=instrument.symbol,
        status=instrument.status,
        checkpoint=label,
        checkpoint_ms=point_ms,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        classification=classification,
        expected_count=expected,
        candle_count=0,
        coverage_pct="0.000",
        missing_expected_count=expected,
        missing_at_start=True,
        missing_at_end=True,
        gap_count=0,
        duplicate_start_times=0,
        unfinished_filtered=0,
        error=f"{type(exc).__name__}: {exc}",
    )


def discover_first_trade_ms(
    client: BybitPublicClient,
    instrument: Instrument,
    *,
    now_ms: int,
) -> int | None:
    """Find the first observed trade minute without trusting launchTime.

    Daily history is scanned forward in aligned chunks of at most 1000 days until
    the first non-empty chunk is found or the instrument lifetime ends. Then at
    most two 12-hour 1m windows locate the exact first observed minute.
    """
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)
    effective_end = delivery if delivery > launch else now_ms
    if effective_end <= 0:
        return None

    final_day = align_down_ms(max(0, effective_end - 1), "D")
    chunk_start = align_down_ms(max(0, launch), "D")
    first_day: int | None = None

    while chunk_start <= final_day:
        chunk_end = min(final_day, chunk_start + 999 * DAY_MS)
        daily = client.kline_page(
            symbol=instrument.symbol,
            start_ms=chunk_start,
            end_ms=chunk_end,
            interval="D",
            limit=1000,
            now_ms=now_ms,
        )
        if daily.candles:
            first_day = daily.candles[0].start_ms
            break
        chunk_start = chunk_end + DAY_MS

    if first_day is None:
        return None

    last_allowed = align_down_ms(max(first_day, effective_end - 1), "1")
    halves = (
        (first_day, min(first_day + 719 * MINUTE_MS, last_allowed)),
        (first_day + 720 * MINUTE_MS, min(first_day + 1439 * MINUTE_MS, last_allowed)),
    )
    for start_ms, end_ms in halves:
        if end_ms < start_ms:
            continue
        page = client.kline_page(
            symbol=instrument.symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval="1",
            limit=1000,
            now_ms=now_ms,
        )
        if page.candles:
            return page.candles[0].start_ms
    return None


def _prelaunch_history_probe(
    client: BybitPublicClient,
    instrument: Instrument,
    *,
    now_ms: int,
) -> ProbeResult:
    """Search up to 1000 complete UTC days before the launch day for hidden history."""
    launch = int(instrument.launchTime)
    launch_day = align_down_ms(max(0, launch), "D")
    end_ms = launch_day - DAY_MS
    if end_ms < 0:
        start_ms = 0
        end_ms = 0
        candles: tuple[object, ...] = ()
    else:
        start_ms = max(0, end_ms - 999 * DAY_MS)
        page = client.kline_page(
            symbol=instrument.symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval="D",
            limit=1000,
            now_ms=now_ms,
        )
        candles = page.candles

    classification = (
        ProbeClassification.LIFETIME_METADATA_CONFLICT
        if candles
        else ProbeClassification.BEFORE_LAUNCH
    )
    return ProbeResult(
        symbol=instrument.symbol,
        status=instrument.status,
        checkpoint="pre_launch_1000d",
        checkpoint_ms=launch_day,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        classification=classification,
        expected_count=0,
        candle_count=len(candles),
        coverage_pct="0.000",
        missing_expected_count=0,
        missing_at_start=False,
        missing_at_end=False,
        gap_count=0,
        duplicate_start_times=0,
        unfinished_filtered=0,
    )


def _single_probe(
    client: BybitPublicClient,
    instrument: Instrument,
    *,
    label: str,
    point_ms: int,
    now_ms: int,
    first_trade_ms: int | None,
) -> ProbeResult:
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
        return _series_result(
            instrument,
            label,
            point_ms,
            start_ms,
            end_ms,
            series,
            first_trade_ms=first_trade_ms,
        )
    except Exception as exc:
        return _error_probe(
            instrument,
            label=label,
            point_ms=point_ms,
            start_ms=start_ms,
            end_ms=end_ms,
            classification=ProbeClassification.API_ERROR,
            exc=exc,
        )


def probe_symbol(
    client: BybitPublicClient,
    instrument: Instrument,
    *,
    now_ms: int,
) -> SymbolCoverage:
    launch = int(instrument.launchTime)
    delivery = int(instrument.deliveryTime or 0)

    first_trade_ms: int | None = None
    discovery_error: Exception | None = None
    try:
        first_trade_ms = discover_first_trade_ms(client, instrument, now_ms=now_ms)
    except Exception as exc:
        discovery_error = exc

    probes: list[ProbeResult] = []

    try:
        prelaunch = _prelaunch_history_probe(client, instrument, now_ms=now_ms)
    except Exception as exc:
        prelaunch = _error_probe(
            instrument,
            label="pre_launch_1000d",
            point_ms=align_down_ms(max(0, launch), "D"),
            start_ms=max(0, align_down_ms(max(0, launch), "D") - 1000 * DAY_MS),
            end_ms=max(0, align_down_ms(max(0, launch), "D") - DAY_MS),
            classification=ProbeClassification.API_ERROR,
            exc=exc,
        )
    probes.append(prelaunch)

    if discovery_error is not None:
        point = align_down_ms(launch, "1")
        start_ms, end_ms = _probe_window(point)
        probes.append(
            _error_probe(
                instrument,
                label="first_trade_discovery",
                point_ms=point,
                start_ms=start_ms,
                end_ms=end_ms,
                classification=ProbeClassification.API_ERROR,
                exc=discovery_error,
            )
        )
    elif first_trade_ms is None:
        point = align_down_ms(launch, "1")
        start_ms, end_ms = _probe_window(point)
        probes.append(
            ProbeResult(
                symbol=instrument.symbol,
                status=instrument.status,
                checkpoint="first_trade_discovery",
                checkpoint_ms=point,
                window_start_ms=start_ms,
                window_end_ms=end_ms,
                classification=ProbeClassification.FIRST_TRADE_NOT_FOUND,
                expected_count=0,
                candle_count=0,
                coverage_pct="0.000",
                missing_expected_count=0,
                missing_at_start=False,
                missing_at_end=False,
                gap_count=0,
                duplicate_start_times=0,
                unfinished_filtered=0,
            )
        )

    for label, point_ms in _probe_points(
        instrument,
        now_ms,
        first_trade_ms=first_trade_ms,
    ):
        probes.append(
            _single_probe(
                client,
                instrument,
                label=label,
                point_ms=point_ms,
                now_ms=now_ms,
                first_trade_ms=first_trade_ms,
            )
        )

    origin = first_trade_ms if first_trade_ms is not None else launch
    effective_end = delivery if delivery > origin else now_ms
    funding_point = align_down_ms(origin + max(0, effective_end - origin) // 2, "1")
    funding_start = max(origin, funding_point - 4 * DAY_MS)
    funding_end = min(effective_end - 1, funding_point + 4 * DAY_MS)

    if not _funding_is_applicable(instrument):
        funding_probe = FundingProbeResult(
            symbol=instrument.symbol,
            checkpoint_ms=funding_point,
            classification=ProbeClassification.NOT_APPLICABLE,
            event_count=0,
        )
    else:
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
                else classify_empty(
                    instrument,
                    funding_point,
                    first_trade_ms=first_trade_ms,
                )
            )
            funding_probe = FundingProbeResult(
                symbol=instrument.symbol,
                checkpoint_ms=funding_point,
                classification=funding_class,
                event_count=len(events),
            )
        except Exception as exc:
            funding_probe = FundingProbeResult(
                symbol=instrument.symbol,
                checkpoint_ms=funding_point,
                classification=ProbeClassification.API_ERROR,
                event_count=0,
                error=f"{type(exc).__name__}: {exc}",
            )

    metadata_conflict = _has_lifetime_metadata_conflict(
        first_trade_ms=first_trade_ms,
        launch_ms=launch,
        prelaunch_classification=prelaunch.classification,
    )

    return SymbolCoverage(
        symbol=instrument.symbol,
        status=instrument.status,
        symbol_type=instrument.symbolType,
        eligible_now=currently_eligible_crypto_perpetual(instrument),
        launch_ms=launch,
        first_trade_ms=first_trade_ms,
        launch_to_first_trade_ms=(
            first_trade_ms - launch if first_trade_ms is not None else None
        ),
        delivery_ms=delivery,
        lifetime_metadata_conflict=metadata_conflict,
        kline_probes=tuple(probes),
        funding_probe=funding_probe,
    )


def audit_candidates(inventory: InventorySnapshot) -> list[Instrument]:
    return [item for item in inventory.instruments if should_sample_for_audit(item)]


def append_jsonl_fsync(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def load_jsonl_results(path: Path) -> list[SymbolCoverage]:
    if not path.exists():
        return []
    results: list[SymbolCoverage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        probes = tuple(
            ProbeResult(
                **{
                    **probe,
                    "classification": ProbeClassification(probe["classification"]),
                }
            )
            for probe in row["kline_probes"]
        )
        funding = FundingProbeResult(
            **{
                **row["funding_probe"],
                "classification": ProbeClassification(
                    row["funding_probe"]["classification"]
                ),
            }
        )
        results.append(
            SymbolCoverage(
                **{
                    **row,
                    "kline_probes": probes,
                    "funding_probe": funding,
                }
            )
        )
    return results


def run_sample_coverage(
    client: BybitPublicClient,
    inventory: InventorySnapshot,
    *,
    run_dir: Path,
    now_ms: int,
    max_symbols: int | None = None,
) -> tuple[SymbolCoverage, ...]:
    jsonl_path = run_dir / "coverage-sample.jsonl"
    existing = load_jsonl_results(jsonl_path)
    completed = {result.symbol for result in existing}
    results = list(existing)

    instruments = audit_candidates(inventory)
    if max_symbols is not None:
        instruments = instruments[:max_symbols]

    for instrument in instruments:
        if instrument.symbol in completed:
            continue
        try:
            result = probe_symbol(client, instrument, now_ms=now_ms)
        except Exception as exc:
            result = SymbolCoverage(
                symbol=instrument.symbol,
                status=instrument.status,
                symbol_type=instrument.symbolType,
                eligible_now=currently_eligible_crypto_perpetual(instrument),
                launch_ms=int(instrument.launchTime),
                first_trade_ms=None,
                launch_to_first_trade_ms=None,
                delivery_ms=int(instrument.deliveryTime or 0),
                lifetime_metadata_conflict=False,
                kline_probes=(),
                funding_probe=FundingProbeResult(
                    symbol=instrument.symbol,
                    checkpoint_ms=0,
                    classification=ProbeClassification.INTERNAL_ERROR,
                    event_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
        append_jsonl_fsync(run_dir / "coverage-sample.jsonl", asdict(result))
        results.append(result)

    return tuple(results)


def write_coverage(results: tuple[SymbolCoverage, ...], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    json_doc = [asdict(result) for result in results]
    atomic_write_bytes(
        directory / "coverage-sample.json",
        json.dumps(json_doc, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode(
            "utf-8"
        ),
    )

    csv_path = directory / "coverage-sample.csv"
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "symbol",
                "status",
                "symbol_type",
                "first_trade_ms",
                "launch_to_first_trade_ms",
                "checkpoint",
                "classification",
                "expected_count",
                "candle_count",
                "coverage_pct",
                "missing_expected_count",
                "missing_at_start",
                "missing_at_end",
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
                        result.symbol_type,
                        result.first_trade_ms or "",
                        result.launch_to_first_trade_ms or "",
                        probe.checkpoint,
                        probe.classification,
                        probe.expected_count,
                        probe.candle_count,
                        probe.coverage_pct,
                        probe.missing_expected_count,
                        probe.missing_at_start,
                        probe.missing_at_end,
                        probe.gap_count,
                        probe.duplicate_start_times,
                        probe.unfinished_filtered,
                        probe.error or "",
                    ]
                )
    os.replace(tmp, csv_path)

    delays = [
        result.launch_to_first_trade_ms
        for result in results
        if result.launch_to_first_trade_ms is not None
    ]
    summary = {
        "symbols": len(results),
        "status_counts": dict(
            sorted(
                {
                    status: sum(item.status == status for item in results)
                    for status in {item.status for item in results}
                }.items()
            )
        ),
        "eligible_now": sum(item.eligible_now for item in results),
        "lifetime_metadata_conflicts": sum(
            item.lifetime_metadata_conflict for item in results
        ),
        "first_trade_found": sum(item.first_trade_ms is not None for item in results),
        "launch_delay_ms": {
            "count": len(delays),
            "min": min(delays) if delays else None,
            "max": max(delays) if delays else None,
            "median": sorted(delays)[len(delays) // 2] if delays else None,
        },
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
