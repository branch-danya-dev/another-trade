from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from another_trade.audit.bulk_pilot import (
    HOUR_MS,
    MINUTE_MS,
    MonthBounds,
    RawPageStore,
    download_symbol_month,
    parse_month,
)
from another_trade.audit.coverage import discover_first_trade_ms
from another_trade.audit.inventory import currently_eligible_crypto_perpetual
from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.models import FundingItem, Instrument, MarkPriceKline
from another_trade.io import atomic_write_bytes
from another_trade.time import align_down_ms, utc_ms

FULL_DATA_START_MS = utc_ms(datetime(2021, 12, 20, tzinfo=UTC))
FULL_DATA_END_MS = utc_ms(datetime(2026, 10, 1, tzinfo=UTC))
STRATEGY_DEVELOPMENT_START_MS = utc_ms(datetime(2022, 1, 1, tzinfo=UTC))
STRATEGY_VALIDATION_START_MS = utc_ms(datetime(2025, 1, 1, tzinfo=UTC))
STRATEGY_HOLDOUT_START_MS = utc_ms(datetime(2026, 1, 1, tzinfo=UTC))
STRATEGY_HOLDOUT_END_MS = utc_ms(datetime(2026, 9, 30, tzinfo=UTC))
FUNDING_LIMIT = 200
MARK_SAMPLE_DENOMINATOR = 100
DELIVERY_INDEX_LOOKBACK_MS = 120 * MINUTE_MS


@dataclass(frozen=True, slots=True)
class LifetimeRecord:
    symbol: str
    status: str
    first_trade_ms: int | None
    launch_ms: int
    delivery_ms: int
    eligible: bool


@dataclass(frozen=True, slots=True)
class FullPartitionTask:
    symbol: str
    month: str
    month_start_ms: int
    month_end_ms: int
    data_start_ms: int
    data_end_ms: int
    delivery_ms: int


@dataclass(frozen=True, slots=True)
class FundingFetchStats:
    new_requests: int
    reused_requests: int
    split_windows: int


@dataclass(frozen=True, slots=True)
class FundingMonthResult:
    events: tuple[FundingItem, ...]
    stats: FundingFetchStats


@dataclass(frozen=True, slots=True)
class MarkFundingSummary:
    funding_event_count: int
    hour_aligned_count: int
    mark_60m_rows: int
    exact_1m_checks: int
    exact_1m_matches: int
    exact_1m_mismatches: int
    missing_mark_count: int
    canonical_60m_count: int
    canonical_1m_count: int
    approximate_1m_count: int


def _append_jsonl_fsync(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def load_frozen_instruments(inventory_path: Path) -> tuple[Instrument, ...]:
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    rows = payload.get("instruments")
    if not isinstance(rows, list):
        raise ValueError("frozen inventory has no instruments list")
    return tuple(
        Instrument.model_validate(cast(dict[str, object], row))
        for row in rows
        if isinstance(row, dict)
    )


def load_lifetime_records(path: Path) -> dict[str, LifetimeRecord]:
    if not path.exists():
        return {}
    out: dict[str, LifetimeRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        record = LifetimeRecord(
            symbol=str(row["symbol"]),
            status=str(row["status"]),
            first_trade_ms=(
                int(row["first_trade_ms"])
                if row.get("first_trade_ms") is not None
                else None
            ),
            launch_ms=int(row["launch_ms"]),
            delivery_ms=int(row["delivery_ms"]),
            eligible=bool(row["eligible"]),
        )
        out[record.symbol] = record
    return out


def discover_lifetime_records(
    client: BybitPublicClient,
    instruments: tuple[Instrument, ...],
    *,
    path: Path,
    now_ms: int,
) -> dict[str, LifetimeRecord]:
    records = load_lifetime_records(path)
    for instrument in instruments:
        if not currently_eligible_crypto_perpetual(instrument):
            continue
        if instrument.symbol in records:
            continue
        first_trade_ms = discover_first_trade_ms(
            client,
            instrument,
            now_ms=now_ms,
        )
        record = LifetimeRecord(
            symbol=instrument.symbol,
            status=instrument.status,
            first_trade_ms=first_trade_ms,
            launch_ms=int(instrument.launchTime),
            delivery_ms=int(instrument.deliveryTime or 0),
            eligible=True,
        )
        records[record.symbol] = record
        _append_jsonl_fsync(path, asdict(record))
    return records


def _month_sequence(start_ms: int, end_ms: int) -> list[MonthBounds]:
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    cursor_year = start_dt.year
    cursor_month = start_dt.month
    out: list[MonthBounds] = []
    while True:
        label = f"{cursor_year:04d}-{cursor_month:02d}"
        bounds = parse_month(label)
        if bounds.start_ms >= end_ms:
            return out
        if bounds.end_ms > start_ms:
            out.append(bounds)
        if cursor_month == 12:
            cursor_year += 1
            cursor_month = 1
        else:
            cursor_month += 1


def _delivery_exclusive_ms(delivery_ms: int) -> int:
    if delivery_ms <= 0:
        return FULL_DATA_END_MS
    if delivery_ms % MINUTE_MS == 0:
        return delivery_ms
    return align_down_ms(delivery_ms, "1") + MINUTE_MS


def build_partition_plan(
    lifetimes: dict[str, LifetimeRecord],
) -> list[FullPartitionTask]:
    months = _month_sequence(FULL_DATA_START_MS, FULL_DATA_END_MS)
    tasks: list[FullPartitionTask] = []
    for record in lifetimes.values():
        if not record.eligible or record.first_trade_ms is None:
            continue
        lifetime_start = max(FULL_DATA_START_MS, record.first_trade_ms)
        lifetime_end = min(
            FULL_DATA_END_MS,
            _delivery_exclusive_ms(record.delivery_ms),
        )
        if lifetime_end <= lifetime_start:
            continue
        for bounds in months:
            data_start = max(bounds.start_ms, lifetime_start)
            data_end = min(bounds.end_ms, lifetime_end)
            if data_end <= data_start:
                continue
            tasks.append(
                FullPartitionTask(
                    symbol=record.symbol,
                    month=bounds.label,
                    month_start_ms=bounds.start_ms,
                    month_end_ms=bounds.end_ms,
                    data_start_ms=data_start,
                    data_end_ms=data_end,
                    delivery_ms=record.delivery_ms,
                )
            )
    tasks.sort(key=lambda task: (task.month_start_ms, task.symbol))
    return tasks


def write_partition_plan(path: Path, tasks: list[FullPartitionTask]) -> None:
    atomic_write_bytes(
        path,
        json.dumps(
            [asdict(task) for task in tasks],
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8"),
    )


def load_partition_plan(path: Path) -> list[FullPartitionTask]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("partition plan must be a list")
    return [
        FullPartitionTask(
            symbol=str(row["symbol"]),
            month=str(row["month"]),
            month_start_ms=int(row["month_start_ms"]),
            month_end_ms=int(row["month_end_ms"]),
            data_start_ms=int(row["data_start_ms"]),
            data_end_ms=int(row["data_end_ms"]),
            delivery_ms=int(row["delivery_ms"]),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _funding_params(
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, str | int]:
    return {
        "category": "linear",
        "symbol": symbol,
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": FUNDING_LIMIT,
    }


def download_funding_month(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    now_ms: int,
) -> FundingMonthResult:
    by_identity: dict[tuple[str, str, str], FundingItem] = {}
    new_requests = 0
    reused_requests = 0
    split_windows = 0

    def fetch(left: int, right: int) -> None:
        nonlocal new_requests, reused_requests, split_windows
        params = _funding_params(symbol, left, right)
        endpoint = "/v5/market/funding/history"
        if store.has_aux(endpoint=endpoint, params=params):
            raw = store.read_aux(endpoint=endpoint, params=params)
            items = client._parse_funding_raw(raw)
            reused_requests += 1
        else:
            page = client.funding_page_with_raw(
                symbol=symbol,
                start_ms=left,
                end_ms=right - 1,
                limit=FUNDING_LIMIT,
                now_ms=now_ms,
            )
            items = page.items
            store.put_aux(
                stream="funding",
                range_start_ms=left,
                range_end_ms=right,
                endpoint=page.endpoint,
                params=page.params,
                raw=page.raw,
                raw_rows=len(items),
                captured_at_ms=time.time_ns() // 1_000_000,
            )
            new_requests += 1

        if len(items) >= FUNDING_LIMIT:
            if right - left <= HOUR_MS:
                raise RuntimeError(
                    f"funding window saturated at minimum split for {symbol}: "
                    f"{left}..{right}"
                )
            midpoint = align_down_ms((left + right) // 2, "60")
            if midpoint <= left or midpoint >= right:
                midpoint = left + (right - left) // 2
            split_windows += 1
            fetch(left, midpoint)
            fetch(midpoint, right)
            return

        for item in items:
            timestamp = int(item.fundingRateTimestamp)
            if left <= timestamp < right:
                key = (item.symbol, item.fundingRateTimestamp, item.fundingRate)
                by_identity[key] = item

    fetch(start_ms, end_ms)
    events = tuple(
        sorted(
            by_identity.values(),
            key=lambda item: int(item.fundingRateTimestamp),
        )
    )
    return FundingMonthResult(
        events=events,
        stats=FundingFetchStats(
            new_requests=new_requests,
            reused_requests=reused_requests,
            split_windows=split_windows,
        ),
    )


def _mark_60m_params(
    symbol: str,
    bounds: MonthBounds,
) -> dict[str, str | int]:
    return {
        "category": "linear",
        "symbol": symbol,
        "interval": "60",
        "start": bounds.start_ms,
        "end": bounds.end_ms - HOUR_MS,
        "limit": 1000,
    }


def download_mark_60m(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    bounds: MonthBounds,
    now_ms: int,
) -> tuple[tuple[MarkPriceKline, ...], bool]:
    params = _mark_60m_params(symbol, bounds)
    endpoint = "/v5/market/mark-price-kline"
    if store.has_aux(endpoint=endpoint, params=params):
        raw = store.read_aux(endpoint=endpoint, params=params)
        series = client._parse_mark_price_raw(raw, interval="60", now_ms=now_ms)
        return series.candles, True

    page = client.mark_price_page_with_raw(
        symbol=symbol,
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms - HOUR_MS,
        interval="60",
        limit=1000,
        now_ms=now_ms,
    )
    store.put_aux(
        stream="mark:60",
        range_start_ms=bounds.start_ms,
        range_end_ms=bounds.end_ms,
        endpoint=page.endpoint,
        params=page.params,
        raw=page.raw,
        raw_rows=page.series.raw_row_count,
        captured_at_ms=time.time_ns() // 1_000_000,
    )
    return page.series.candles, False


def _deterministic_mark_sample(symbol: str, timestamp_ms: int) -> bool:
    digest = hashlib.sha256(f"{symbol}:{timestamp_ms}".encode("ascii")).digest()
    bucket = int.from_bytes(digest[:8], "big") % MARK_SAMPLE_DENOMINATOR
    return bucket == 0


def _exact_mark_1m(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    timestamp_ms: int,
    now_ms: int,
) -> Decimal | None:
    params: dict[str, str | int] = {
        "category": "linear",
        "symbol": symbol,
        "interval": "1",
        "start": timestamp_ms,
        "end": timestamp_ms,
        "limit": 1,
    }
    endpoint = "/v5/market/mark-price-kline"
    if store.has_aux(endpoint=endpoint, params=params):
        raw = store.read_aux(endpoint=endpoint, params=params)
        series = client._parse_mark_price_raw(raw, interval="1", now_ms=now_ms)
    else:
        page = client.mark_price_page_with_raw(
            symbol=symbol,
            start_ms=timestamp_ms,
            end_ms=timestamp_ms,
            interval="1",
            limit=1,
            now_ms=now_ms,
        )
        series = page.series
        store.put_aux(
            stream="mark:1:audit",
            range_start_ms=timestamp_ms,
            range_end_ms=timestamp_ms + MINUTE_MS,
            endpoint=page.endpoint,
            params=page.params,
            raw=page.raw,
            raw_rows=series.raw_row_count,
            captured_at_ms=time.time_ns() // 1_000_000,
        )
    return series.candles[0].open if series.candles else None


def _previous_mark_1m_close(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    timestamp_ms: int,
    now_ms: int,
) -> Decimal | None:
    previous = timestamp_ms - MINUTE_MS
    params: dict[str, str | int] = {
        "category": "linear",
        "symbol": symbol,
        "interval": "1",
        "start": previous,
        "end": previous,
        "limit": 1,
    }
    endpoint = "/v5/market/mark-price-kline"
    if store.has_aux(endpoint=endpoint, params=params):
        raw = store.read_aux(endpoint=endpoint, params=params)
        series = client._parse_mark_price_raw(raw, interval="1", now_ms=now_ms)
    else:
        page = client.mark_price_page_with_raw(
            symbol=symbol,
            start_ms=previous,
            end_ms=previous,
            interval="1",
            limit=1,
            now_ms=now_ms,
        )
        series = page.series
        store.put_aux(
            stream="mark:1:fallback",
            range_start_ms=previous,
            range_end_ms=timestamp_ms,
            endpoint=page.endpoint,
            params=page.params,
            raw=page.raw,
            raw_rows=series.raw_row_count,
            captured_at_ms=time.time_ns() // 1_000_000,
        )
    return series.candles[0].close if series.candles else None


def build_funding_values(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    bounds: MonthBounds,
    events: tuple[FundingItem, ...],
    now_ms: int,
) -> tuple[list[dict[str, object]], MarkFundingSummary]:
    if not events:
        return [], MarkFundingSummary(
            funding_event_count=0,
            hour_aligned_count=0,
            mark_60m_rows=0,
            exact_1m_checks=0,
            exact_1m_matches=0,
            exact_1m_mismatches=0,
            missing_mark_count=0,
            canonical_60m_count=0,
            canonical_1m_count=0,
            approximate_1m_count=0,
        )

    hourly, _ = download_mark_60m(
        client,
        store,
        symbol=symbol,
        bounds=bounds,
        now_ms=now_ms,
    )
    hourly_open = {candle.start_ms: candle.open for candle in hourly}

    rows: list[dict[str, object]] = []
    aligned_count = 0
    exact_checks = 0
    exact_matches = 0
    exact_mismatches = 0
    missing_count = 0
    use_60m = 0
    use_1m = 0
    use_approx = 0

    for item in events:
        timestamp = int(item.fundingRateTimestamp)
        aligned = timestamp % HOUR_MS == 0
        if aligned:
            aligned_count += 1
        hour_value = hourly_open.get(timestamp) if aligned else None
        audit_exact = (
            not aligned
            or hour_value is None
            or _deterministic_mark_sample(symbol, timestamp)
        )
        exact_value: Decimal | None = None
        if audit_exact:
            exact_checks += 1
            exact_value = _exact_mark_1m(
                client,
                store,
                symbol=symbol,
                timestamp_ms=timestamp,
                now_ms=now_ms,
            )
            if hour_value is not None and exact_value is not None:
                if hour_value == exact_value:
                    exact_matches += 1
                else:
                    exact_mismatches += 1

        source: str
        mark_value: Decimal | None
        approximate = False

        if hour_value is not None and (
            exact_value is None or hour_value == exact_value
        ):
            source = "MARK_60M_OPEN"
            mark_value = hour_value
            use_60m += 1
        elif exact_value is not None:
            source = "MARK_1M_OPEN"
            mark_value = exact_value
            use_1m += 1
        else:
            fallback = _previous_mark_1m_close(
                client,
                store,
                symbol=symbol,
                timestamp_ms=timestamp,
                now_ms=now_ms,
            )
            if fallback is not None:
                source = "MARK_PRICE_1M_APPROX"
                mark_value = fallback
                approximate = True
                use_approx += 1
            else:
                source = "MARK_PRICE_MISSING"
                mark_value = None
                missing_count += 1

        rows.append(
            {
                "symbol": symbol,
                "funding_rate_timestamp_ms": timestamp,
                "funding_rate": item.fundingRate,
                "mark_price": str(mark_value) if mark_value is not None else None,
                "mark_source": source,
                "mark_approximate": approximate,
                "hour_aligned": aligned,
                "sampled_exact_1m": audit_exact,
                "sampled_1m_open": (
                    str(exact_value) if exact_value is not None else None
                ),
                "mark_60m_open": (
                    str(hour_value) if hour_value is not None else None
                ),
                "sample_match": (
                    hour_value == exact_value
                    if hour_value is not None and exact_value is not None
                    else None
                ),
            }
        )

    return rows, MarkFundingSummary(
        funding_event_count=len(events),
        hour_aligned_count=aligned_count,
        mark_60m_rows=len(hourly),
        exact_1m_checks=exact_checks,
        exact_1m_matches=exact_matches,
        exact_1m_mismatches=exact_mismatches,
        missing_mark_count=missing_count,
        canonical_60m_count=use_60m,
        canonical_1m_count=use_1m,
        approximate_1m_count=use_approx,
    )


def capture_delivery_index_evidence(
    client: BybitPublicClient,
    store: RawPageStore,
    *,
    symbol: str,
    delivery_ms: int,
    now_ms: int,
) -> dict[str, object] | None:
    if delivery_ms <= 0:
        return None
    end_start = align_down_ms(delivery_ms, "1")
    if end_start >= delivery_ms:
        end_start -= MINUTE_MS
    start = max(0, end_start - DELIVERY_INDEX_LOOKBACK_MS + MINUTE_MS)
    if end_start < start:
        return None

    params: dict[str, str | int] = {
        "category": "linear",
        "symbol": symbol,
        "interval": "1",
        "start": start,
        "end": end_start,
        "limit": 1000,
    }
    endpoint = "/v5/market/index-price-kline"
    if store.has_aux(endpoint=endpoint, params=params):
        raw = store.read_aux(endpoint=endpoint, params=params)
        series = client._parse_mark_price_raw(raw, interval="1", now_ms=now_ms)
    else:
        page = client.index_price_page_with_raw(
            symbol=symbol,
            start_ms=start,
            end_ms=end_start,
            interval="1",
            limit=1000,
            now_ms=now_ms,
        )
        series = page.series
        store.put_aux(
            stream="index:1:delivery",
            range_start_ms=start,
            range_end_ms=end_start + MINUTE_MS,
            endpoint=page.endpoint,
            params=page.params,
            raw=page.raw,
            raw_rows=series.raw_row_count,
            captured_at_ms=time.time_ns() // 1_000_000,
        )

    return {
        "symbol": symbol,
        "delivery_ms": delivery_ms,
        "index_window_start_ms": start,
        "index_window_end_ms": end_start + MINUTE_MS,
        "row_count": len(series.candles),
        "candles": [candle.as_dict() for candle in series.candles],
        "settlement_formula_status": "REQUIRES_EVENT_SPECIFIC_ANNOUNCEMENT",
    }


def _funding_logical_hash(rows: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def process_full_partition(
    client: BybitPublicClient,
    *,
    task: FullPartitionTask,
    run_dir: Path,
    now_ms: int,
) -> dict[str, object]:
    bounds = MonthBounds(
        label=task.month,
        start_ms=task.month_start_ms,
        end_ms=task.month_end_ms,
    )
    price_artifact = download_symbol_month(
        client,
        symbol=task.symbol,
        bounds=bounds,
        run_dir=run_dir,
        now_ms=now_ms,
        data_start_ms=task.data_start_ms,
        data_end_ms=task.data_end_ms,
    )

    raw_path = run_dir / "raw" / task.symbol / f"{task.month}.sqlite3"
    with RawPageStore(raw_path, symbol=task.symbol, month=task.month) as store:
        funding = download_funding_month(
            client,
            store,
            symbol=task.symbol,
            start_ms=task.data_start_ms,
            end_ms=task.data_end_ms,
            now_ms=now_ms,
        )
        funding_rows, mark_summary = build_funding_values(
            client,
            store,
            symbol=task.symbol,
            bounds=bounds,
            events=funding.events,
            now_ms=now_ms,
        )
        delivery_evidence = None
        if (
            task.delivery_ms > 0
            and task.data_start_ms < task.delivery_ms <= task.data_end_ms
        ):
            delivery_evidence = capture_delivery_index_evidence(
                client,
                store,
                symbol=task.symbol,
                delivery_ms=task.delivery_ms,
                now_ms=now_ms,
            )

        aux_raw_hash = store.aux_index_sha256()
        aux_payload_hash = store.aux_payload_index_sha256()
        aux_count = store.aux_count()

    funding_doc = {
        "symbol": task.symbol,
        "month": task.month,
        "data_start_ms": task.data_start_ms,
        "data_end_ms": task.data_end_ms,
        "event_count": len(funding_rows),
        "logical_sha256": _funding_logical_hash(funding_rows),
        "events": funding_rows,
    }
    funding_path = run_dir / "funding" / task.symbol / f"{task.month}.json"
    atomic_write_bytes(
        funding_path,
        json.dumps(
            funding_doc,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8"),
    )

    if delivery_evidence is not None:
        delivery_path = (
            run_dir / "delivery-index" / task.symbol / f"{task.month}.json"
        )
        atomic_write_bytes(
            delivery_path,
            json.dumps(
                delivery_evidence,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8"),
        )

    full_manifest = {
        **asdict(price_artifact),
        "status": price_artifact.status.value,
        "data_contract_partition_kind": "FULL_HISTORICAL",
        "funding_logical_sha256": funding_doc["logical_sha256"],
        "funding_event_count": len(funding_rows),
        "funding_fetch_stats": asdict(funding.stats),
        "mark_funding_summary": asdict(mark_summary),
        "aux_raw_response_count": aux_count,
        "aux_raw_index_sha256": aux_raw_hash,
        "aux_payload_index_sha256": aux_payload_hash,
        "delivery_index_evidence": delivery_evidence is not None,
        "structural_metrics_only": task.data_start_ms >= STRATEGY_VALIDATION_START_MS,
        "strategy_metrics_computed": False,
    }
    manifest_path = run_dir / "partitions" / task.symbol / f"{task.month}.json"
    atomic_write_bytes(
        manifest_path,
        json.dumps(
            full_manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8"),
    )
    return full_manifest


def partition_manifest_is_complete(
    run_dir: Path,
    task: FullPartitionTask,
) -> bool:
    path = run_dir / "partitions" / task.symbol / f"{task.month}.json"
    if not path.exists():
        return False
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        row.get("data_contract_partition_kind") == "FULL_HISTORICAL"
        and row.get("symbol") == task.symbol
        and row.get("month") == task.month
        and row.get("data_start_ms") == task.data_start_ms
        and row.get("data_end_ms") == task.data_end_ms
    )


def structural_summary(
    run_dir: Path,
    tasks: list[FullPartitionTask],
) -> dict[str, object]:
    completed = 0
    missing_minutes = 0
    funding_events = 0
    mark_checks = 0
    mark_mismatches = 0
    missing_mark = 0
    for task in tasks:
        path = run_dir / "partitions" / task.symbol / f"{task.month}.json"
        if not path.exists():
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("data_contract_partition_kind") != "FULL_HISTORICAL":
            continue
        completed += 1
        missing_minutes += int(row.get("missing_minutes", 0))
        funding_events += int(row.get("funding_event_count", 0))
        mark = row.get("mark_funding_summary", {})
        if isinstance(mark, dict):
            mark_checks += int(mark.get("exact_1m_checks", 0))
            mark_mismatches += int(mark.get("exact_1m_mismatches", 0))
            missing_mark += int(mark.get("missing_mark_count", 0))
    return {
        "planned_partition_count": len(tasks),
        "completed_partition_count": completed,
        "total_missing_minutes": missing_minutes,
        "funding_event_count": funding_events,
        "mark_exact_1m_check_count": mark_checks,
        "mark_exact_1m_mismatch_count": mark_mismatches,
        "missing_mark_count": missing_mark,
        "strategy_metrics_computed": False,
        "contains_pnl": False,
        "contains_signals": False,
        "contains_setup_counts": False,
    }


def fetch_announcements_snapshot(
    client: BybitPublicClient,
    *,
    run_dir: Path,
    now_ms: int,
) -> list[dict[str, object]]:
    store_path = run_dir / "raw-global" / "announcements.sqlite3"
    announcements: list[dict[str, object]] = []
    page_number = 1
    limit = 20

    with RawPageStore(
        store_path,
        symbol="__GLOBAL__",
        month="announcements",
    ) as store:
        while True:
            params: dict[str, str | int] = {
                "locale": "en-US",
                "page": page_number,
                "limit": limit,
            }
            endpoint = "/v5/announcements/index"
            if store.has_aux(endpoint=endpoint, params=params):
                raw = store.read_aux(endpoint=endpoint, params=params)
                payload = client._decode(raw)
            else:
                raw, payload = client.announcement_page_raw(
                    page=page_number,
                    limit=limit,
                    locale="en-US",
                )
                result = payload.get("result")
                row_count = 0
                if isinstance(result, dict):
                    values = result.get("list")
                    if isinstance(values, list):
                        row_count = len(values)
                store.put_aux(
                    stream="announcements",
                    range_start_ms=page_number,
                    range_end_ms=page_number,
                    endpoint=endpoint,
                    params=params,
                    raw=raw,
                    raw_rows=row_count,
                    captured_at_ms=time.time_ns() // 1_000_000,
                )

            result = payload.get("result")
            if not isinstance(result, dict):
                break
            values = result.get("list")
            if not isinstance(values, list) or not values:
                break
            for value in values:
                if isinstance(value, dict):
                    announcements.append(cast(dict[str, object], value))

            total_value = result.get("total")
            total = int(total_value) if isinstance(total_value, int | str) else None
            if total is not None and page_number * limit >= total:
                break
            page_number += 1
            if page_number > 1000:
                raise RuntimeError("announcement pagination exceeded safety bound")

    atomic_write_bytes(
        run_dir / "announcements-snapshot.json",
        json.dumps(
            announcements,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8"),
    )
    return announcements


def build_delisting_announcement_candidates(
    instruments: tuple[Instrument, ...],
    announcements: list[dict[str, object]],
) -> list[dict[str, object]]:
    closed = [
        instrument
        for instrument in instruments
        if currently_eligible_crypto_perpetual(instrument)
        and int(instrument.deliveryTime or 0) > 0
    ]
    rows: list[dict[str, object]] = []
    keywords = ("delist", "migration", "migrate", "conversion", "upgrade")
    for announcement in announcements:
        title = str(announcement.get("title", ""))
        description = str(announcement.get("description", ""))
        haystack = f"{title}\n{description}".upper()
        if not any(keyword.upper() in haystack for keyword in keywords):
            continue
        for instrument in closed:
            exact_symbol = instrument.symbol.upper() in haystack
            base_match = instrument.baseCoin.upper() in haystack
            if not exact_symbol and not base_match:
                continue
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "base_coin": instrument.baseCoin,
                    "delivery_ms": int(instrument.deliveryTime or 0),
                    "match_kind": (
                        "EXACT_SYMBOL" if exact_symbol else "BASE_COIN_CANDIDATE"
                    ),
                    "title": title,
                    "description": description,
                    "url": announcement.get("url"),
                    "publish_time_ms": announcement.get("publishTime"),
                    "date_timestamp_ms": announcement.get("dateTimestamp"),
                }
            )
    rows.sort(
        key=lambda row: (
            str(row["symbol"]),
            int(row["publish_time_ms"] or 0),
            str(row["title"]),
        )
    )
    return rows
