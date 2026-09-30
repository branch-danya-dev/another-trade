from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from another_trade.bybit.errors import BybitApiError, BybitHttpError, BybitRateLimitError
from another_trade.bybit.models import (
    FundingEnvelope,
    FundingItem,
    Instrument,
    InstrumentsEnvelope,
    Kline,
    KlineEnvelope,
)
from another_trade.cache import ImmutableResponseCache
from another_trade.time import close_time_ms, interval_ms

RETRYABLE_RET_CODES = {10000, 10006, 10016}
RATE_LIMIT_RET_CODE = 10006


@dataclass(frozen=True, slots=True)
class KlineSeries:
    candles: tuple[Kline, ...]
    duplicate_start_times: int
    unfinished_filtered: int
    gaps: tuple[tuple[int, int], ...]


class BybitPublicClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        cache_dir: Path | None = None,
        timeout_s: float = 20.0,
        requests_per_second: float | None = 10.0,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = ImmutableResponseCache(cache_dir) if cache_dir is not None else None
        self.max_retries = max_retries
        self.requests_per_second = requests_per_second
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: float | None = None
        self.http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": "another-trade-data-audit/0.1"},
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> BybitPublicClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=int)
        if not isinstance(value, dict):
            raise BybitApiError(-1, "top-level response is not an object")
        return value

    def _throttle(self) -> None:
        if not self.requests_per_second or self.requests_per_second <= 0:
            return
        minimum = 1.0 / self.requests_per_second
        now = self.monotonic()
        if self._last_request_at is not None:
            wait = minimum - (now - self._last_request_at)
            if wait > 0:
                self.sleep(wait)
        self._last_request_at = self.monotonic()

    @staticmethod
    def _params(params: Mapping[str, str | int | None]) -> dict[str, str | int]:
        return {key: value for key, value in params.items() if value is not None}

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        base = min(8.0, 0.5 * (2**attempt))
        if response is not None:
            reset = response.headers.get("X-Bapi-Limit-Reset-Timestamp")
            if reset and reset.isdigit():
                delta = int(reset) / 1000.0 - time.time()
                if 0 < delta < 60:
                    return max(base, delta)
        return base

    def _request_raw(
        self,
        path: str,
        params: Mapping[str, str | int | None],
        *,
        cacheable: bool,
        cache_guard: Callable[[bytes], bool] | None = None,
    ) -> bytes:
        clean = self._params(params)
        key = ImmutableResponseCache.key(
            base_url=self.base_url,
            method="GET",
            path=path,
            params=clean,
        )
        if cacheable and self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached.raw

        last_rate_error: BybitRateLimitError | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.http.get(path, params=clean)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                self.sleep(self._backoff(attempt))
                continue

            raw = response.content
            if response.status_code == 403:
                raise BybitHttpError(response.status_code, raw)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    raise BybitHttpError(response.status_code, raw)
                self.sleep(self._backoff(attempt, response))
                continue
            if not 200 <= response.status_code < 300:
                raise BybitHttpError(response.status_code, raw)

            payload = self._decode(raw)
            ret_code = payload.get("retCode")
            ret_msg = payload.get("retMsg")
            if type(ret_code) is not int or not isinstance(ret_msg, str):
                raise BybitApiError(-1, "missing/invalid retCode or retMsg")

            if ret_code != 0:
                if ret_code == RATE_LIMIT_RET_CODE:
                    last_rate_error = BybitRateLimitError(ret_code, ret_msg)
                    if attempt >= self.max_retries:
                        raise last_rate_error
                    self.sleep(self._backoff(attempt, response))
                    continue
                if ret_code in RETRYABLE_RET_CODES and attempt < self.max_retries:
                    self.sleep(self._backoff(attempt, response))
                    continue
                raise BybitApiError(ret_code, ret_msg)

            if cacheable and self.cache is not None:
                if cache_guard is not None and not cache_guard(raw):
                    return raw
                self.cache.put(
                    key=key,
                    raw=raw,
                    request_metadata={
                        "base_url": self.base_url,
                        "method": "GET",
                        "path": path,
                        "params": clean,
                    },
                )
            return raw

        if last_rate_error is not None:
            raise last_rate_error
        raise RuntimeError("unreachable retry loop")

    def instruments(self, status: str) -> list[Instrument]:
        if status not in {"Trading", "Closed"}:
            raise ValueError("inventory status must be Trading or Closed")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        out: list[Instrument] = []
        while True:
            raw = self._request_raw(
                "/v5/market/instruments-info",
                {
                    "category": "linear",
                    "status": status,
                    "limit": 1000,
                    "cursor": cursor,
                },
                cacheable=False,
            )
            try:
                envelope = InstrumentsEnvelope.model_validate(self._decode(raw))
            except ValidationError as exc:
                raise BybitApiError(-1, f"instrument schema error: {exc}") from exc
            out.extend(envelope.result.list)
            next_cursor = envelope.result.nextPageCursor or None
            if next_cursor is None:
                return out
            if next_cursor in seen_cursors:
                raise BybitApiError(-1, f"instrument pagination cursor repeated: {next_cursor}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _kline_cache_guard(interval: str, now_ms: int) -> Callable[[bytes], bool]:
        def guard(raw: bytes) -> bool:
            envelope = KlineEnvelope.model_validate(BybitPublicClient._decode(raw))
            return all(
                close_time_ms(int(row[0]), interval) <= now_ms
                for row in envelope.result.list
            )

        return guard

    def kline_page(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "1",
        limit: int = 1000,
        now_ms: int | None = None,
    ) -> KlineSeries:
        if limit < 1 or limit > 1000:
            raise ValueError("Bybit kline limit must be 1..1000")
        now_value = int(time.time() * 1000) if now_ms is None else now_ms
        step = interval_ms(interval)
        cacheable = end_ms + step <= now_value
        raw = self._request_raw(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": start_ms,
                "end": end_ms,
                "limit": limit,
            },
            cacheable=cacheable,
            cache_guard=self._kline_cache_guard(interval, now_value),
        )
        try:
            envelope = KlineEnvelope.model_validate(self._decode(raw))
        except ValidationError as exc:
            raise BybitApiError(-1, f"kline schema error: {exc}") from exc

        by_start: dict[int, Kline] = {}
        duplicates = 0
        unfinished = 0
        for row in envelope.result.list:
            candle = Kline(row)
            if close_time_ms(candle.start_ms, interval) > now_value:
                unfinished += 1
                continue
            if candle.start_ms in by_start:
                duplicates += 1
                continue
            by_start[candle.start_ms] = candle

        candles = tuple(by_start[key] for key in sorted(by_start))
        gaps = self._gaps(candles, step)
        return KlineSeries(candles, duplicates, unfinished, gaps)

    @staticmethod
    def _gaps(candles: tuple[Kline, ...], step_ms: int) -> tuple[tuple[int, int], ...]:
        gaps: list[tuple[int, int]] = []
        for previous, current in pairwise(candles):
            expected = previous.start_ms + step_ms
            if current.start_ms != expected:
                gaps.append((expected, current.start_ms))
        return tuple(gaps)

    def klines(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "1",
        limit: int = 1000,
        now_ms: int | None = None,
    ) -> KlineSeries:
        """Paginate newest->oldest, deduplicate boundaries, then verify continuity."""
        now_value = int(time.time() * 1000) if now_ms is None else now_ms
        step = interval_ms(interval)
        page_end = end_ms
        by_start: dict[int, Kline] = {}
        duplicates = 0
        unfinished = 0

        while page_end >= start_ms:
            page = self.kline_page(
                symbol=symbol,
                start_ms=start_ms,
                end_ms=page_end,
                interval=interval,
                limit=limit,
                now_ms=now_value,
            )
            duplicates += page.duplicate_start_times
            unfinished += page.unfinished_filtered
            if not page.candles:
                break
            for candle in page.candles:
                if candle.start_ms in by_start:
                    duplicates += 1
                else:
                    by_start[candle.start_ms] = candle

            oldest = page.candles[0].start_ms
            if len(page.candles) < limit or oldest <= start_ms:
                break
            next_end = oldest - 1
            if next_end >= page_end:
                raise RuntimeError("kline pagination did not move backward")
            page_end = next_end

        candles = tuple(by_start[key] for key in sorted(by_start))
        return KlineSeries(candles, duplicates, unfinished, self._gaps(candles, step))

    def funding_page(
        self,
        *,
        symbol: str,
        start_ms: int | None,
        end_ms: int,
        limit: int = 200,
        now_ms: int | None = None,
    ) -> list[FundingItem]:
        if limit < 1 or limit > 200:
            raise ValueError("Bybit funding limit must be 1..200")
        now_value = int(time.time() * 1000) if now_ms is None else now_ms
        raw = self._request_raw(
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
            cacheable=end_ms < now_value,
        )
        try:
            envelope = FundingEnvelope.model_validate(self._decode(raw))
        except ValidationError as exc:
            raise BybitApiError(-1, f"funding schema error: {exc}") from exc
        return envelope.result.list
