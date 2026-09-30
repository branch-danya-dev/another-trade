from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
OUT = Path("tests/fixtures/bybit")
USER_AGENT = "another-trade-data-audit/0.1 fixture-capture"


def request(path: str, params: dict[str, Any]) -> tuple[bytes, int, str]:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read(), response.status, url
    except urllib.error.HTTPError as exc:
        return exc.read(), exc.code, url


def decode(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=int)
    if not isinstance(value, dict):
        raise RuntimeError("Bybit response is not a JSON object")
    return value


def require_ok(raw: bytes, status: int, label: str) -> dict[str, Any]:
    try:
        payload = decode(raw)
    except Exception as exc:
        if status == 403:
            raise RuntimeError(
                f"{label}: Bybit returned HTTP 403 from {BASE_URL}. "
                "Run fixture capture from a network/jurisdiction allowed by Bybit."
            ) from exc
        raise
    if status != 200:
        raise RuntimeError(f"{label}: HTTP {status}: {raw[:300]!r}")
    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"{label}: retCode={payload.get('retCode')} "
            f"retMsg={payload.get('retMsg')!r}"
        )
    return payload


def store(
    name: str,
    raw: bytes,
    status: int,
    url: str,
    manifest: list[dict[str, Any]],
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / name
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (OUT / f"{name}.sha256").write_text(digest + "\n", encoding="utf-8")
    manifest.append(
        {
            "file": name,
            "http_status": status,
            "sha256": digest,
            "url": url,
        }
    )


def fetch_instruments(status_name: str, limit: int = 1000) -> tuple[bytes, dict[str, Any], str]:
    raw, status, url = request(
        "/v5/market/instruments-info",
        {"category": "linear", "status": status_name, "limit": limit},
    )
    return raw, require_ok(raw, status, f"{status_name} instruments"), url


def main() -> None:
    manifest: list[dict[str, Any]] = []

    trading_raw, _trading, trading_url = fetch_instruments("Trading", 100)
    store("instruments_trading.json", trading_raw, 200, trading_url, manifest)

    closed_raw, closed, closed_url = fetch_instruments("Closed", 1000)
    store("instruments_closed.json", closed_raw, 200, closed_url, manifest)

    closed_items = closed.get("result", {}).get("list", [])
    candidates = [
        item
        for item in closed_items
        if item.get("contractType") == "LinearPerpetual"
        and item.get("quoteCoin") == "USDT"
        and item.get("settleCoin") == "USDT"
    ]
    if not candidates:
        raise RuntimeError(
            "Closed inventory page has no USDT LinearPerpetual candidate. "
            "Extend the capture script with cursor pagination before inventing a fixture."
        )

    instrument = candidates[0]
    symbol = str(instrument["symbol"])
    launch_ms = int(instrument["launchTime"])
    delivery_ms = int(instrument.get("deliveryTime") or 0)
    if delivery_ms <= launch_ms:
        raise RuntimeError(f"{symbol}: invalid closed lifetime")

    kline_end = delivery_ms - 60_000
    kline_start = max(launch_ms, kline_end - 30 * 60_000)
    kline_raw, status, url = request(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": kline_start,
            "end": kline_end,
            "limit": 100,
        },
    )
    require_ok(kline_raw, status, f"{symbol} closed kline")
    store("kline_closed_symbol.json", kline_raw, status, url, manifest)

    funding_raw, status, url = request(
        "/v5/market/funding/history",
        {
            "category": "linear",
            "symbol": symbol,
            "endTime": delivery_ms - 1,
            "limit": 20,
        },
    )
    require_ok(funding_raw, status, f"{symbol} funding")
    store("funding_closed_symbol.json", funding_raw, status, url, manifest)

    now_ms = int(time.time() * 1000)
    safe_end = ((now_ms // 60_000) - 2) * 60_000
    safe_start = safe_end - 10 * 60_000
    live_kline_raw, status, url = request(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "1",
            "start": safe_start,
            "end": safe_end,
            "limit": 20,
        },
    )
    require_ok(live_kline_raw, status, "BTCUSDT kline")
    store("kline_trading_symbol.json", live_kline_raw, status, url, manifest)

    error_raw, status, url = request(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": "THIS_SYMBOL_MUST_NOT_EXIST_USDT",
            "interval": "1",
            "limit": 1,
        },
    )
    error_payload = decode(error_raw)
    if error_payload.get("retCode") == 0:
        raise RuntimeError("Expected a real non-zero retCode response fixture")
    store("retcode_error.json", error_raw, status, url, manifest)

    manifest_doc = {
        "captured_at_ms": int(time.time() * 1000),
        "base_url": BASE_URL,
        "closed_fixture_symbol": symbol,
        "files": manifest,
    }
    manifest_raw = json.dumps(
        manifest_doc,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    (OUT / "manifest.json").write_bytes(manifest_raw)
    (OUT / "manifest.json.sha256").write_text(
        hashlib.sha256(manifest_raw).hexdigest() + "\n",
        encoding="utf-8",
    )

    print(f"Captured real fixtures from {BASE_URL}; closed symbol={symbol}")
    print(f"Output: {OUT}")


if __name__ == "__main__":
    main()
