from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CANDIDATE_BASE_URLS = [
    "https://api.bybit.eu",
    "https://api.bytick.com",
    "https://api-testnet.bybit.com",
    "https://api.bybit.com",
]
BASE_URL = ""
OUT = Path("tests/fixtures/bybit")
USER_AGENT = "another-trade-data-audit/0.1 fixture-capture"


def request(
    path: str,
    params: dict[str, Any],
    *,
    base_url: str | None = None,
) -> tuple[bytes, int, str]:
    query = urllib.parse.urlencode(params)
    root = base_url or BASE_URL
    url = f"{root}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    return raw, status, url


def decode(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Bybit response is not a JSON object")
    return value


def require_ok(raw: bytes, label: str) -> dict[str, Any]:
    payload = decode(raw)
    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"{label}: HTTP response contained retCode={payload.get('retCode')} "
            f"retMsg={payload.get('retMsg')!r}"
        )
    return payload


def select_base_url() -> str:
    errors: list[str] = []
    for base_url in CANDIDATE_BASE_URLS:
        raw, status, _ = request(
            "/v5/market/instruments-info",
            {"category": "linear", "status": "Trading", "limit": 3},
            base_url=base_url,
        )
        try:
            payload = decode(raw)
        except Exception as exc:
            errors.append(
                f"{base_url}: HTTP {status}, non-JSON: {raw[:160]!r} ({exc})"
            )
            continue
        if status == 200 and payload.get("retCode") == 0:
            return base_url
        errors.append(
            f"{base_url}: HTTP {status}, retCode={payload.get('retCode')} "
            f"retMsg={payload.get('retMsg')!r}"
        )
    raise RuntimeError(
        "No official Bybit endpoint reachable from runner:\n" + "\n".join(errors)
    )


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


def main() -> None:
    global BASE_URL
    BASE_URL = select_base_url()
    print(f"Capturing fixtures from {BASE_URL}")
    manifest: list[dict[str, Any]] = []

    trading_raw, status, url = request(
        "/v5/market/instruments-info",
        {"category": "linear", "status": "Trading", "limit": 20},
    )
    require_ok(trading_raw, "Trading instruments")
    store("instruments_trading.json", trading_raw, status, url, manifest)

    closed_raw, status, url = request(
        "/v5/market/instruments-info",
        {"category": "linear", "status": "Closed", "limit": 100},
    )
    closed = require_ok(closed_raw, "Closed instruments")
    store("instruments_closed.json", closed_raw, status, url, manifest)

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
            f"{BASE_URL}: Closed response contains no USDT LinearPerpetual candidate"
        )

    instrument = candidates[0]
    symbol = instrument["symbol"]
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
    require_ok(kline_raw, f"{symbol} closed kline")
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
    require_ok(funding_raw, f"{symbol} funding")
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
    require_ok(live_kline_raw, "BTCUSDT kline")
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
        raise RuntimeError("Expected a non-zero retCode fixture")
    store("retcode_error.json", error_raw, status, url, manifest)

    captured_at = int(time.time() * 1000)
    manifest_doc = {
        "captured_at_ms": captured_at,
        "base_url": BASE_URL,
        "closed_fixture_symbol": symbol,
        "files": manifest,
    }
    manifest_raw = json.dumps(
        manifest_doc, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    (OUT / "manifest.json").write_bytes(manifest_raw)
    (OUT / "manifest.json.sha256").write_text(
        hashlib.sha256(manifest_raw).hexdigest() + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
