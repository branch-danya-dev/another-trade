from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from another_trade.audit.inventory import InventorySnapshot, currently_eligible_crypto_perpetual
from another_trade.bybit.client import BybitPublicClient
from another_trade.bybit.models import FundingItem, Instrument, Kline
from another_trade.io import atomic_write_bytes
from another_trade.time import interval_ms, utc_ms

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RAW_SCHEMA_VERSION = 1
PARQUET_SCHEMA_VERSION = 1
DECIMAL_TYPE = pa.decimal128(38, 18)
PARQUET_COMPRESSION = "zstd"
PARQUET_COMPRESSION_LEVEL = 9
PARQUET_USE_DICTIONARY = False
PARQUET_WRITE_STATISTICS = True
PARQUET_ROW_GROUP_SIZE = 65_536
PARQUET_DATA_PAGE_VERSION = "2.0"
PARQUET_VERSION = "2.6"
PARQUET_WRITER_CONFIG: dict[str, object] = {
    "compression": PARQUET_COMPRESSION,
    "compression_level": PARQUET_COMPRESSION_LEVEL,
    "use_dictionary": PARQUET_USE_DICTIONARY,
    "write_statistics": PARQUET_WRITE_STATISTICS,
    "row_group_size": PARQUET_ROW_GROUP_SIZE,
    "data_page_version": PARQUET_DATA_PAGE_VERSION,
    "version": PARQUET_VERSION,
}


class PartitionStatus(StrEnum):
    OPEN = "OPEN"
    SEALED = "SEALED"


class PilotAbort(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MonthBounds:
    label: str
    start_ms: int
    end_ms: int  # exclusive


@dataclass(frozen=True, slots=True)
class PageWindow:
    start_ms: int
    end_ms: int  # inclusive candle start


@dataclass(frozen=True, slots=True)
class AggregatedBar:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass(frozen=True, slots=True)
class PartitionArtifact:
    symbol: str
    month: str
    status: PartitionStatus
    expected_minutes: int
    actual_minutes: int
    missing_minutes: int
    first_start_ms: int | None
    last_start_ms: int | None
    logical_content_sha256: str
    parquet_file_sha256: str
    raw_index_sha256: str
    raw_page_count: int
    parquet_path: str
    raw_container_path: str


def parse_month(value: str) -> MonthBounds:
    try:
        year_text, month_text = value.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        start = datetime(year, month, 1, tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError("month must use YYYY-MM") from exc

    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return MonthBounds(label=value, start_ms=utc_ms(start), end_ms=utc_ms(end))


def partition_status(bounds: MonthBounds, now_ms: int) -> PartitionStatus:
    return (
        PartitionStatus.SEALED
        if now_ms >= bounds.end_ms + DAY_MS
        else PartitionStatus.OPEN
    )


def page_windows(bounds: MonthBounds, page_size: int = 1000) -> Iterator[PageWindow]:
    if page_size < 1 or page_size > 1000:
        raise ValueError("page_size must be 1..1000")
    cursor = bounds.start_ms
    final_start = bounds.end_ms - MINUTE_MS
    while cursor <= final_start:
        end = min(final_start, cursor + (page_size - 1) * MINUTE_MS)
        yield PageWindow(start_ms=cursor, end_ms=end)
        cursor = end + MINUTE_MS


def instrument_covers_month(item: Instrument, bounds: MonthBounds) -> bool:
    launch = int(item.launchTime)
    delivery = int(item.deliveryTime or 0)
    return launch <= bounds.start_ms and (delivery == 0 or delivery >= bounds.end_ms)


def select_default_pilot_symbols(
    inventory: InventorySnapshot,
    bounds: MonthBounds,
    *,
    target_count: int = 9,
) -> list[str]:
    if target_count < 6:
        raise ValueError("pilot target_count must be >= 6")

    eligible = [
        item
        for item in inventory.instruments
        if currently_eligible_crypto_perpetual(item) and instrument_covers_month(item, bounds)
    ]
    by_symbol = {item.symbol: item for item in eligible}
    selected: list[str] = []

    preferred_trading = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
    ]
    for symbol in preferred_trading:
        item = by_symbol.get(symbol)
        if item is not None and item.status == "Trading" and symbol not in selected:
            selected.append(symbol)

    closed = [item for item in eligible if item.status == "Closed"]
    closed.sort(key=lambda item: item.symbol)
    if "MATICUSDT" in by_symbol and by_symbol["MATICUSDT"].status == "Closed":
        selected.append("MATICUSDT")

    for item in closed:
        if item.symbol not in selected:
            selected.append(item.symbol)
        if sum(by_symbol[symbol].status == "Closed" for symbol in selected) >= 3:
            break

    trading = [item for item in eligible if item.status == "Trading"]
    trading.sort(key=lambda item: item.symbol)
    for item in trading:
        if item.symbol not in selected:
            selected.append(item.symbol)
        if len(selected) >= target_count:
            break

    selected = selected[:target_count]
    if "BTCUSDT" not in selected:
        raise ValueError(f"BTCUSDT does not cover pilot month {bounds.label}")
    if not any(by_symbol[symbol].status == "Closed" for symbol in selected):
        raise ValueError(f"no Closed perpetual covers pilot month {bounds.label}")
    return selected


def _canonical_params(params: dict[str, str | int]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


class RawPageStore:
    def __init__(self, path: Path, *, symbol: str, month: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                page_start_ms INTEGER PRIMARY KEY,
                page_end_ms INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                params_json TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_gzip BLOB NOT NULL,
                raw_bytes INTEGER NOT NULL,
                raw_rows INTEGER NOT NULL,
                captured_at_ms INTEGER NOT NULL
            )
            """
        )
        self._set_metadata("schema_version", str(RAW_SCHEMA_VERSION))
        self._set_metadata("symbol", symbol)
        self._set_metadata("month", month)
        self.conn.commit()

    def _set_metadata(self, key: str, value: str) -> None:
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (key, value),
            )
        elif row[0] != value:
            raise RuntimeError(
                f"raw container metadata mismatch for {key}: {row[0]!r} != {value!r}"
            )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> RawPageStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def has_page(self, start_ms: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM responses WHERE page_start_ms = ?",
                (start_ms,),
            ).fetchone()
            is not None
        )

    def put_page(
        self,
        *,
        window: PageWindow,
        endpoint: str,
        params: dict[str, str | int],
        raw: bytes,
        raw_rows: int,
        captured_at_ms: int,
    ) -> None:
        digest = hashlib.sha256(raw).hexdigest()
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        self.conn.execute(
            """
            INSERT INTO responses(
                page_start_ms, page_end_ms, endpoint, params_json, raw_sha256,
                raw_gzip, raw_bytes, raw_rows, captured_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_start_ms) DO NOTHING
            """,
            (
                window.start_ms,
                window.end_ms,
                endpoint,
                _canonical_params(params),
                digest,
                compressed,
                len(raw),
                raw_rows,
                captured_at_ms,
            ),
        )
        self.conn.commit()

    def read_raw(self, start_ms: int) -> bytes:
        row = self.conn.execute(
            "SELECT raw_sha256, raw_gzip FROM responses WHERE page_start_ms = ?",
            (start_ms,),
        ).fetchone()
        if row is None:
            raise KeyError(start_ms)
        raw = gzip.decompress(row[1])
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row[0]:
            raise RuntimeError(f"raw container corruption at page {start_ms}")
        return raw

    def page_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return int(row[0]) if row is not None else 0

    def logical_index_sha256(self) -> str:
        h = hashlib.sha256()
        rows = self.conn.execute(
            """
            SELECT page_start_ms, page_end_ms, endpoint, params_json, raw_sha256,
                   raw_bytes, raw_rows
            FROM responses
            ORDER BY page_start_ms
            """
        )
        for row in rows:
            line = json.dumps(
                {
                    "page_start_ms": row[0],
                    "page_end_ms": row[1],
                    "endpoint": row[2],
                    "params_json": row[3],
                    "raw_sha256": row[4],
                    "raw_bytes": row[5],
                    "raw_rows": row[6],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            h.update(line)
            h.update(b"\n")
        return h.hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    quantum = Decimal("0.000000000000000001")
    normalized = value.quantize(quantum)
    if normalized != value:
        raise ValueError(f"decimal {value} exceeds fixed 18-decimal storage scale")
    return format(normalized, "f")


def logical_content_sha256(symbol: str, candles: Sequence[Kline]) -> str:
    h = hashlib.sha256()
    for candle in sorted(candles, key=lambda item: item.start_ms):
        fields = [
            symbol,
            str(candle.start_ms),
            _canonical_decimal(candle.open),
            _canonical_decimal(candle.high),
            _canonical_decimal(candle.low),
            _canonical_decimal(candle.close),
            _canonical_decimal(candle.volume),
            _canonical_decimal(candle.turnover),
        ]
        h.update(("\t".join(fields) + "\n").encode("utf-8"))
    return h.hexdigest()


def _arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            ("symbol", pa.string()),
            ("start_ms", pa.int64()),
            ("open", DECIMAL_TYPE),
            ("high", DECIMAL_TYPE),
            ("low", DECIMAL_TYPE),
            ("close", DECIMAL_TYPE),
            ("volume", DECIMAL_TYPE),
            ("turnover", DECIMAL_TYPE),
        ]
    )


def write_partition_parquet(
    *,
    symbol: str,
    candles: Sequence[Kline],
    path: Path,
) -> tuple[str, str]:
    ordered = sorted(candles, key=lambda item: item.start_ms)
    schema = _arrow_schema()
    table = pa.Table.from_arrays(
        [
            pa.array([symbol] * len(ordered), type=pa.string()),
            pa.array([item.start_ms for item in ordered], type=pa.int64()),
            pa.array([item.open for item in ordered], type=DECIMAL_TYPE),
            pa.array([item.high for item in ordered], type=DECIMAL_TYPE),
            pa.array([item.low for item in ordered], type=DECIMAL_TYPE),
            pa.array([item.close for item in ordered], type=DECIMAL_TYPE),
            pa.array([item.volume for item in ordered], type=DECIMAL_TYPE),
            pa.array([item.turnover for item in ordered], type=DECIMAL_TYPE),
        ],
        schema=schema,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
        use_dictionary=PARQUET_USE_DICTIONARY,
        write_statistics=PARQUET_WRITE_STATISTICS,
        row_group_size=PARQUET_ROW_GROUP_SIZE,
        data_page_version=PARQUET_DATA_PAGE_VERSION,
        version=PARQUET_VERSION,
    )
    tmp.replace(path)
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return logical_content_sha256(symbol, ordered), file_hash


def _parse_store_candles(
    client: BybitPublicClient,
    store: RawPageStore,
    bounds: MonthBounds,
    *,
    now_ms: int,
) -> list[Kline]:
    by_start: dict[int, Kline] = {}
    for window in page_windows(bounds):
        raw = store.read_raw(window.start_ms)
        page = client._parse_kline_raw(raw, interval="1", now_ms=now_ms)
        for candle in page.candles:
            if bounds.start_ms <= candle.start_ms < bounds.end_ms:
                existing = by_start.get(candle.start_ms)
                if existing is not None and existing.as_dict() != candle.as_dict():
                    raise RuntimeError(
                        f"conflicting duplicate candle at {candle.start_ms}"
                    )
                by_start[candle.start_ms] = candle
    return [by_start[key] for key in sorted(by_start)]


def missing_minute_starts(bounds: MonthBounds, candles: Sequence[Kline]) -> list[int]:
    actual = {item.start_ms for item in candles}
    return [
        start
        for start in range(bounds.start_ms, bounds.end_ms, MINUTE_MS)
        if start not in actual
    ]


def aggregate_candles(
    candles: Sequence[Kline],
    *,
    interval: str,
) -> list[AggregatedBar]:
    step = interval_ms(interval)
    grouped: dict[int, list[Kline]] = {}
    for candle in candles:
        start = candle.start_ms - (candle.start_ms % step)
        grouped.setdefault(start, []).append(candle)

    bars: list[AggregatedBar] = []
    expected_count = step // MINUTE_MS
    for start in sorted(grouped):
        items = sorted(grouped[start], key=lambda item: item.start_ms)
        if len(items) != expected_count:
            continue
        if items[0].start_ms != start:
            continue
        if items[-1].start_ms != start + step - MINUTE_MS:
            continue
        bars.append(
            AggregatedBar(
                start_ms=start,
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                volume=sum((item.volume for item in items), Decimal(0)),
                turnover=sum((item.turnover for item in items), Decimal(0)),
            )
        )
    return bars


def _native_compare(
    client: BybitPublicClient,
    *,
    symbol: str,
    bounds: MonthBounds,
    candles: Sequence[Kline],
    interval: str,
    now_ms: int,
) -> dict[str, object]:
    local = {bar.start_ms: bar for bar in aggregate_candles(candles, interval=interval)}
    native = client.klines(
        symbol=symbol,
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms - interval_ms(interval),
        interval=interval,
        limit=1000,
        now_ms=now_ms,
    )
    native_map = {item.start_ms: item for item in native.candles}
    common = sorted(set(local) & set(native_map))
    mismatches: list[dict[str, object]] = []
    for start in common:
        left = local[start]
        right = native_map[start]
        diffs: list[str] = []
        if left.open != right.open:
            diffs.append("open")
        if left.high != right.high:
            diffs.append("high")
        if left.low != right.low:
            diffs.append("low")
        if left.close != right.close:
            diffs.append("close")
        if left.volume != right.volume:
            diffs.append("volume")
        if left.turnover != right.turnover:
            diffs.append("turnover")
        if diffs:
            mismatches.append({"start_ms": start, "fields": diffs})
    return {
        "interval": interval,
        "local_count": len(local),
        "native_count": len(native_map),
        "common_count": len(common),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
    }


def _sample_funding_timestamps(items: Sequence[int], limit: int = 5) -> list[int]:
    ordered = sorted(set(items))
    if len(ordered) <= limit:
        return ordered
    indexes = {0, len(ordered) - 1, len(ordered) // 2}
    if limit >= 4:
        indexes.add(len(ordered) // 4)
    if limit >= 5:
        indexes.add((3 * len(ordered)) // 4)
    return [ordered[index] for index in sorted(indexes)]


def _funding_events_for_month(
    client: BybitPublicClient,
    *,
    symbol: str,
    bounds: MonthBounds,
    now_ms: int,
) -> list[FundingItem]:
    events_by_identity: dict[tuple[str, str, str], FundingItem] = {}
    cursor = bounds.start_ms
    chunk_ms = 7 * DAY_MS
    while cursor < bounds.end_ms:
        chunk_end = min(bounds.end_ms - 1, cursor + chunk_ms - 1)
        items = client.funding_page(
            symbol=symbol,
            start_ms=cursor,
            end_ms=chunk_end,
            limit=200,
            now_ms=now_ms,
        )
        for item in items:
            key = (item.symbol, item.fundingRateTimestamp, item.fundingRate)
            events_by_identity[key] = item
        cursor = chunk_end + 1
    return list(events_by_identity.values())


def compare_mark_price_funding_opens(
    client: BybitPublicClient,
    *,
    symbol: str,
    bounds: MonthBounds,
    now_ms: int,
) -> dict[str, object]:
    funding = _funding_events_for_month(
        client,
        symbol=symbol,
        bounds=bounds,
        now_ms=now_ms,
    )
    timestamps = [
        int(item.fundingRateTimestamp)
        for item in funding
        if bounds.start_ms <= int(item.fundingRateTimestamp) < bounds.end_ms
    ]
    aligned = [timestamp for timestamp in timestamps if timestamp % HOUR_MS == 0]

    hourly = client.mark_price_page(
        symbol=symbol,
        start_ms=bounds.start_ms,
        end_ms=bounds.end_ms - HOUR_MS,
        interval="60",
        limit=1000,
        now_ms=now_ms,
    )
    hourly_open = {item.start_ms: item.open for item in hourly.candles}
    comparisons: list[dict[str, object]] = []
    for timestamp in _sample_funding_timestamps(aligned):
        minute = client.mark_price_page(
            symbol=symbol,
            start_ms=timestamp,
            end_ms=timestamp,
            interval="1",
            limit=1,
            now_ms=now_ms,
        )
        minute_open = minute.candles[0].open if minute.candles else None
        hour_open = hourly_open.get(timestamp)
        comparisons.append(
            {
                "timestamp_ms": timestamp,
                "minute_open": str(minute_open) if minute_open is not None else None,
                "hour_open": str(hour_open) if hour_open is not None else None,
                "equal": minute_open is not None and minute_open == hour_open,
            }
        )

    return {
        "funding_event_count": len(timestamps),
        "hour_aligned_count": len(aligned),
        "hour_alignment_rate": (
            len(aligned) / len(timestamps) if timestamps else None
        ),
        "sample_count": len(comparisons),
        "equal_count": sum(bool(item["equal"]) for item in comparisons),
        "all_equal": bool(comparisons) and all(bool(item["equal"]) for item in comparisons),
        "comparisons": comparisons,
    }


def download_symbol_month(
    client: BybitPublicClient,
    *,
    symbol: str,
    bounds: MonthBounds,
    run_dir: Path,
    now_ms: int,
    abort_counter: list[int] | None = None,
    abort_after_pages: int | None = None,
) -> PartitionArtifact:
    raw_path = run_dir / "raw" / symbol / f"{bounds.label}.sqlite3"
    parquet_path = run_dir / "parquet" / symbol / f"{bounds.label}.parquet"

    reused_pages = 0
    downloaded_pages = 0
    with RawPageStore(raw_path, symbol=symbol, month=bounds.label) as store:
        for window in page_windows(bounds):
            if store.has_page(window.start_ms):
                reused_pages += 1
                continue
            page = client.kline_page_with_raw(
                symbol=symbol,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                interval="1",
                limit=1000,
                now_ms=now_ms,
            )
            store.put_page(
                window=window,
                endpoint=page.endpoint,
                params=page.params,
                raw=page.raw,
                raw_rows=page.series.raw_row_count,
                captured_at_ms=time.time_ns() // 1_000_000,
            )
            downloaded_pages += 1
            if abort_counter is not None:
                abort_counter[0] += 1
                if abort_after_pages is not None and abort_counter[0] >= abort_after_pages:
                    raise PilotAbort(
                        f"intentional pilot abort after {abort_counter[0]} newly committed pages"
                    )

        candles = _parse_store_candles(client, store, bounds, now_ms=now_ms)
        raw_hash = store.logical_index_sha256()
        raw_count = store.page_count()

    missing = missing_minute_starts(bounds, candles)
    logical_hash, parquet_hash = write_partition_parquet(
        symbol=symbol,
        candles=candles,
        path=parquet_path,
    )
    artifact = PartitionArtifact(
        symbol=symbol,
        month=bounds.label,
        status=partition_status(bounds, now_ms),
        expected_minutes=(bounds.end_ms - bounds.start_ms) // MINUTE_MS,
        actual_minutes=len(candles),
        missing_minutes=len(missing),
        first_start_ms=candles[0].start_ms if candles else None,
        last_start_ms=candles[-1].start_ms if candles else None,
        logical_content_sha256=logical_hash,
        parquet_file_sha256=parquet_hash,
        raw_index_sha256=raw_hash,
        raw_page_count=raw_count,
        parquet_path=parquet_path.relative_to(run_dir).as_posix(),
        raw_container_path=raw_path.relative_to(run_dir).as_posix(),
    )
    partition_manifest = {
        **asdict(artifact),
        "status": artifact.status.value,
        "missing_minute_starts": missing[:10_000],
        "parquet_schema_version": PARQUET_SCHEMA_VERSION,
        "parquet_writer_config": PARQUET_WRITER_CONFIG,
        "resume_metrics": {
            "reused_pages": reused_pages,
            "downloaded_pages": downloaded_pages,
        },
    }
    target = run_dir / "partitions" / symbol / f"{bounds.label}.json"
    atomic_write_bytes(
        target,
        json.dumps(partition_manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    return artifact


def pilot_symbol_diagnostics(
    client: BybitPublicClient,
    *,
    symbol: str,
    bounds: MonthBounds,
    run_dir: Path,
    now_ms: int,
) -> dict[str, object]:
    raw_path = run_dir / "raw" / symbol / f"{bounds.label}.sqlite3"
    with RawPageStore(raw_path, symbol=symbol, month=bounds.label) as store:
        candles = _parse_store_candles(client, store, bounds, now_ms=now_ms)

    aggregation = [
        _native_compare(
            client,
            symbol=symbol,
            bounds=bounds,
            candles=candles,
            interval=interval,
            now_ms=now_ms,
        )
        for interval in ("15", "60", "D")
    ]
    mark = compare_mark_price_funding_opens(
        client,
        symbol=symbol,
        bounds=bounds,
        now_ms=now_ms,
    )
    return {
        "symbol": symbol,
        "aggregation": aggregation,
        "mark_price_funding_open_comparison": mark,
    }


def _require_int(value: object, *, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be int, got {type(value)!r}")
    return value


def _require_dict(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be dict, got {type(value)!r}")
    return cast(dict[str, object], value)


def _require_dict_list(value: object, *, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{name} must be list[dict]")
    return cast(list[dict[str, object]], value)


def write_pilot_summary(
    *,
    run_dir: Path,
    artifacts: Sequence[PartitionArtifact],
    diagnostics: Sequence[dict[str, object]],
    resumed: bool,
) -> None:
    summary = {
        "partition_count": len(artifacts),
        "all_partitions_complete": all(
            artifact.missing_minutes == 0 for artifact in artifacts
        ),
        "total_missing_minutes": sum(artifact.missing_minutes for artifact in artifacts),
        "resumed": resumed,
        "aggregation_mismatch_count": sum(
            _require_int(item["mismatch_count"], name="mismatch_count")
            for diagnostic in diagnostics
            for item in _require_dict_list(
                diagnostic["aggregation"],
                name="aggregation",
            )
        ),
        "mark_price_sample_count": sum(
            _require_int(
                _require_dict(
                    diagnostic["mark_price_funding_open_comparison"],
                    name="mark_price_funding_open_comparison",
                )["sample_count"],
                name="sample_count",
            )
            for diagnostic in diagnostics
        ),
        "mark_price_equal_count": sum(
            _require_int(
                _require_dict(
                    diagnostic["mark_price_funding_open_comparison"],
                    name="mark_price_funding_open_comparison",
                )["equal_count"],
                name="equal_count",
            )
            for diagnostic in diagnostics
        ),
        "partitions": [
            {**asdict(artifact), "status": artifact.status.value}
            for artifact in artifacts
        ],
    }
    atomic_write_bytes(
        run_dir / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True).encode("utf-8"),
    )
    atomic_write_bytes(
        run_dir / "diagnostics.json",
        json.dumps(list(diagnostics), indent=2, sort_keys=True).encode("utf-8"),
    )
