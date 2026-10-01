from __future__ import annotations

import json
import platform
import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer

from another_trade.audit.bulk_pilot import (
    PARQUET_WRITER_CONFIG,
    PartitionStatus,
    PilotAbort,
    compare_pilot_runs,
    download_symbol_month,
    instrument_covers_month,
    mark_price_only_diagnostics,
    parse_month,
    partition_status,
    pilot_symbol_diagnostics,
    select_default_pilot_symbols,
    write_pilot_summary,
)
from another_trade.audit.coverage import (
    discover_first_trade_ms,
    run_sample_coverage,
    write_coverage,
)
from another_trade.audit.full_download import (
    FULL_DATA_END_MS,
    FULL_DATA_START_MS,
    STRATEGY_DEVELOPMENT_START_MS,
    STRATEGY_HOLDOUT_END_MS,
    STRATEGY_HOLDOUT_START_MS,
    STRATEGY_VALIDATION_START_MS,
    build_delisting_announcement_candidates,
    build_partition_plan,
    discover_lifetime_records,
    fetch_announcements_snapshot,
    load_frozen_instruments,
    load_partition_plan,
    partition_manifest_is_complete,
    process_full_partition,
    structural_summary,
    write_partition_plan,
)
from another_trade.audit.identity import load_identity_config
from another_trade.audit.inventory import (
    collect_inventory,
    currently_eligible_crypto_perpetual,
    write_inventory,
)
from another_trade.bybit.client import BybitPublicClient
from another_trade.io import atomic_write_bytes
from another_trade.time import align_down_ms

SPEC_VERSION = "v0.2.6"
DATA_CONTRACT_VERSION = "v0.1.12"
DECIMAL_BUG_AFFECTED_FULL_DOWNLOAD_COMMIT = (
    "682ffce6597db150687d03141224368adbfb571f"
)

app = typer.Typer(no_args_is_help=True)
audit_app = typer.Typer(no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "UNKNOWN"


def _git_commit_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


@audit_app.command("inventory")
def inventory(
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    cache_dir: Annotated[Path, typer.Option()] = Path(".cache/bybit"),
) -> None:
    with BybitPublicClient(cache_dir=cache_dir) as client:
        snapshot = collect_inventory(client)
    path = artifact_dir / f"inventory-{snapshot.sha256[:16]}.json"
    write_inventory(snapshot, path)
    typer.echo(f"inventory: {len(snapshot.instruments)} symbols")
    typer.echo(f"status counts: {snapshot.status_counts}")
    typer.echo(f"snapshot sha256: {snapshot.sha256}")
    typer.echo(f"written: {path}")


@audit_app.command("sample-coverage")
def coverage(
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    cache_dir: Annotated[Path, typer.Option()] = Path(".cache/bybit"),
    max_symbols: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Optional safety cap. This command never downloads full history.",
        ),
    ] = None,
    resume_run: Annotated[
        Path | None,
        typer.Option(help="Existing run directory to resume from its JSONL checkpoints."),
    ] = None,
) -> None:
    now_ms = int(time.time() * 1000)
    with BybitPublicClient(cache_dir=cache_dir) as client:
        snapshot = collect_inventory(client)

        if resume_run is None:
            run_id = f"{now_ms}-{snapshot.sha256[:12]}"
            run_dir = artifact_dir / "sample-coverage" / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            manifest = {
                "run_id": run_id,
                "kind": "sample-coverage",
                "sample_only": True,
                "bulk_download": False,
                "created_at_ms": now_ms,
                "now_ms": now_ms,
                "inventory_sha256": snapshot.sha256,
                "git_commit_sha": _git_commit_sha(),
                "spec_version": SPEC_VERSION,
                "data_contract_version": DATA_CONTRACT_VERSION,
                "requests_per_second": client.requests_per_second,
                "software_versions": {
                    "python": platform.python_version(),
                    "python_implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "tzdata": _package_version("tzdata"),
                    "httpx": _package_version("httpx"),
                    "pydantic": _package_version("pydantic"),
                    "typer": _package_version("typer"),
                },
                "identity_relationship_config": {
                    "version": load_identity_config().version,
                    "sha256": load_identity_config().sha256,
                },
                "identity_relationships": snapshot.identity_relationships,
            }
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            write_inventory(snapshot, run_dir / "inventory.json")
        else:
            run_dir = resume_run
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                raise typer.BadParameter("resume-run has no manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("inventory_sha256") != snapshot.sha256:
                raise typer.BadParameter(
                    "current inventory hash differs from the run being resumed"
                )
            if manifest.get("spec_version") != SPEC_VERSION:
                raise typer.BadParameter(
                    "spec version differs from the run being resumed"
                )
            if manifest.get("data_contract_version") != DATA_CONTRACT_VERSION:
                raise typer.BadParameter(
                    "data contract version differs from the run being resumed"
                )
            current_commit = _git_commit_sha()
            if manifest.get("git_commit_sha") != current_commit:
                raise typer.BadParameter(
                    "git commit differs from the run being resumed; start a new run"
                )
            now_ms = int(manifest["now_ms"])

        results = run_sample_coverage(
            client,
            snapshot,
            run_dir=run_dir,
            now_ms=now_ms,
            max_symbols=max_symbols,
        )

    write_coverage(results, run_dir)
    typer.echo(f"sample coverage written for {len(results)} symbols")
    typer.echo(f"run directory: {run_dir}")
    typer.echo("No full-history download was performed.")


@audit_app.command("bulk-pilot")
def bulk_pilot(
    month: Annotated[
        str,
        typer.Option(help="Complete UTC development month in YYYY-MM format."),
    ] = "2024-06",
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    symbols: Annotated[
        str | None,
        typer.Option(
            help="Optional comma-separated symbols. Default selects BTC + Trading/Closed mix."
        ),
    ] = None,
    target_count: Annotated[
        int,
        typer.Option(min=6, max=12, help="Default deterministic pilot symbol count."),
    ] = 9,
    resume_run: Annotated[
        Path | None,
        typer.Option(help="Existing bulk-pilot run directory to resume."),
    ] = None,
    abort_after_pages: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Testing only: intentionally abort after N newly committed raw pages.",
        ),
    ] = None,
) -> None:
    bounds = parse_month(month)
    development_end = parse_month("2025-01").start_ms
    if bounds.end_ms > development_end:
        raise typer.BadParameter(
            "bulk pilot is restricted to the development partition ending 2025-01-01"
        )
    now_ms = time.time_ns() // 1_000_000
    git_sha = _git_commit_sha()

    with BybitPublicClient(cache_dir=None) as client:
        snapshot = collect_inventory(client)

        if resume_run is None:
            if symbols:
                selected = sorted(
                    {item.strip().upper() for item in symbols.split(",") if item.strip()}
                )
                if "BTCUSDT" not in selected:
                    raise typer.BadParameter("explicit pilot symbols must include BTCUSDT")
            else:
                selected = select_default_pilot_symbols(
                    snapshot,
                    bounds,
                    target_count=target_count,
                )

            run_id = f"{now_ms}-{bounds.label}-{snapshot.sha256[:12]}"
            run_dir = artifact_dir / "bulk-pilot" / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            manifest = {
                "run_id": run_id,
                "kind": "bulk-pilot",
                "created_at_ms": now_ms,
                "now_ms": now_ms,
                "month": bounds.label,
                "month_start_ms": bounds.start_ms,
                "month_end_ms": bounds.end_ms,
                "inventory_sha256": snapshot.sha256,
                "git_commit_sha": git_sha,
                "spec_version": SPEC_VERSION,
                "data_contract_version": DATA_CONTRACT_VERSION,
                "selected_symbols": selected,
                "requests_per_second": client.requests_per_second,
                "parquet_writer_config": PARQUET_WRITER_CONFIG,
                "software_versions": {
                    "python": platform.python_version(),
                    "python_implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "tzdata": _package_version("tzdata"),
                    "httpx": _package_version("httpx"),
                    "pydantic": _package_version("pydantic"),
                    "pyarrow": _package_version("pyarrow"),
                    "typer": _package_version("typer"),
                },
                "identity_relationship_config": {
                    "version": load_identity_config().version,
                    "sha256": load_identity_config().sha256,
                },
                "status": "RUNNING",
            }
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            write_inventory(snapshot, run_dir / "inventory.json")
            resumed = False
        else:
            run_dir = resume_run
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                raise typer.BadParameter("resume-run has no manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key, expected in (
                ("kind", "bulk-pilot"),
                ("inventory_sha256", snapshot.sha256),
                ("git_commit_sha", git_sha),
                ("spec_version", SPEC_VERSION),
                ("data_contract_version", DATA_CONTRACT_VERSION),
                ("month", bounds.label),
            ):
                if manifest.get(key) != expected:
                    raise typer.BadParameter(
                        f"resume-run mismatch for {key}: "
                        f"{manifest.get(key)!r} != {expected!r}"
                    )
            selected = [str(item) for item in manifest["selected_symbols"]]
            now_ms = int(manifest["now_ms"])
            resumed = True

        by_symbol = {item.symbol: item for item in snapshot.instruments}
        missing_symbols = [symbol for symbol in selected if symbol not in by_symbol]
        if missing_symbols:
            raise typer.BadParameter(
                f"pilot symbols missing from inventory: {missing_symbols}"
            )
        invalid_symbols = [
            symbol
            for symbol in selected
            if not currently_eligible_crypto_perpetual(by_symbol[symbol])
            or not instrument_covers_month(by_symbol[symbol], bounds)
        ]
        if invalid_symbols:
            raise typer.BadParameter(
                "pilot symbols must be eligible perpetuals covering the full month: "
                f"{invalid_symbols}"
            )

        artifacts = []
        diagnostics = []
        abort_counter = [0]
        try:
            for symbol in selected:
                artifact = download_symbol_month(
                    client,
                    symbol=symbol,
                    bounds=bounds,
                    run_dir=run_dir,
                    now_ms=now_ms,
                    abort_counter=abort_counter,
                    abort_after_pages=abort_after_pages,
                )
                artifacts.append(artifact)

            for symbol in selected:
                diagnostic_path = run_dir / "diagnostics" / f"{symbol}.json"
                if diagnostic_path.exists():
                    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                else:
                    diagnostic = pilot_symbol_diagnostics(
                        client,
                        symbol=symbol,
                        bounds=bounds,
                        run_dir=run_dir,
                        now_ms=now_ms,
                    )
                    atomic_write_bytes(
                        diagnostic_path,
                        json.dumps(
                            diagnostic,
                            indent=2,
                            sort_keys=True,
                        ).encode("utf-8"),
                    )
                diagnostics.append(diagnostic)

        except PilotAbort as exc:
            manifest["status"] = "INTENTIONALLY_ABORTED"
            manifest["abort_message"] = str(exc)
            manifest["new_pages_committed_before_abort"] = abort_counter[0]
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            typer.echo(str(exc))
            typer.echo(f"resume with: --resume-run {run_dir}")
            raise typer.Exit(code=3) from exc

    write_pilot_summary(
        run_dir=run_dir,
        artifacts=artifacts,
        diagnostics=diagnostics,
        resumed=resumed,
    )
    manifest["status"] = "COMPLETE"
    manifest["completed_at_ms"] = time.time_ns() // 1_000_000
    manifest["resumed"] = resumed
    atomic_write_bytes(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    typer.echo(f"bulk pilot complete: {run_dir}")
    typer.echo(f"symbols: {', '.join(selected)}")


@audit_app.command("bulk-pilot-compare")
def bulk_pilot_compare(
    reference_run: Annotated[Path, typer.Option(help="Completed resumed pilot run.")],
    candidate_run: Annotated[Path, typer.Option(help="Independent clean pilot run.")],
) -> None:
    comparison = compare_pilot_runs(reference_run, candidate_run)
    output = candidate_run / "reproducibility-comparison.json"
    atomic_write_bytes(
        output,
        json.dumps(comparison, indent=2, sort_keys=True).encode("utf-8"),
    )
    typer.echo(
        "logical hashes equal: "
        f"{comparison['all_logical_equal']}; "
        f"parquet hashes equal: {comparison['all_parquet_equal']}; "
        f"payload hashes equal: {comparison['all_payload_equal']}"
    )
    typer.echo(f"written: {output}")
    if not comparison["all_logical_equal"]:
        raise typer.Exit(code=4)


@audit_app.command("bulk-boundary-pilot")
def bulk_boundary_pilot(
    month: Annotated[str, typer.Option()] = "2024-09",
    symbols: Annotated[str, typer.Option()] = "MATICUSDT,POLUSDT",
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
) -> None:
    bounds = parse_month(month)
    development_end = parse_month("2025-01").start_ms
    if bounds.end_ms > development_end:
        raise typer.BadParameter("boundary pilot must remain inside development data")

    selected = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if len(selected) < 2:
        raise typer.BadParameter("boundary pilot requires at least two symbols")

    now_ms = time.time_ns() // 1_000_000
    with BybitPublicClient(cache_dir=None) as client:
        snapshot = collect_inventory(client)
        by_symbol = {item.symbol: item for item in snapshot.instruments}
        missing = [symbol for symbol in selected if symbol not in by_symbol]
        if missing:
            raise typer.BadParameter(f"symbols missing from inventory: {missing}")

        run_id = f"{now_ms}-{bounds.label}-boundary-{snapshot.sha256[:12]}"
        run_dir = artifact_dir / "bulk-boundary-pilot" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        artifacts = []
        lifetime_rows = []
        for symbol in selected:
            instrument = by_symbol[symbol]
            if not currently_eligible_crypto_perpetual(instrument):
                raise typer.BadParameter(f"{symbol} is not an eligible crypto perpetual")
            first_trade_ms = discover_first_trade_ms(
                client,
                instrument,
                now_ms=now_ms,
            )
            if first_trade_ms is None:
                raise typer.BadParameter(f"first_trade_ms not found for {symbol}")

            start_ms = max(bounds.start_ms, first_trade_ms)
            delivery_ms = int(instrument.deliveryTime or 0)
            if delivery_ms > 0:
                if delivery_ms % 60_000 == 0:
                    end_ms = min(bounds.end_ms, delivery_ms)
                else:
                    end_ms = min(
                        bounds.end_ms,
                        align_down_ms(delivery_ms, "1") + 60_000,
                    )
            else:
                end_ms = bounds.end_ms

            artifact = download_symbol_month(
                client,
                symbol=symbol,
                bounds=bounds,
                run_dir=run_dir,
                now_ms=now_ms,
                data_start_ms=start_ms,
                data_end_ms=end_ms,
            )
            artifacts.append(artifact)
            lifetime_bounds = type(bounds)(
                label=bounds.label,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            boundary_diagnostic = mark_price_only_diagnostics(
                client,
                symbol=symbol,
                bounds=lifetime_bounds,
                run_dir=run_dir,
                now_ms=now_ms,
            )
            diagnostic_path = run_dir / "diagnostics" / f"{symbol}.json"
            atomic_write_bytes(
                diagnostic_path,
                json.dumps(
                    boundary_diagnostic,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8"),
            )
            lifetime_rows.append(
                {
                    "symbol": symbol,
                    "first_trade_ms": first_trade_ms,
                    "delivery_ms": delivery_ms,
                    "partition_data_start_ms": start_ms,
                    "partition_data_end_ms": end_ms,
                    "expected_minutes": artifact.expected_minutes,
                    "actual_minutes": artifact.actual_minutes,
                    "missing_minutes": artifact.missing_minutes,
                    "logical_content_sha256": artifact.logical_content_sha256,
                }
            )

    overlap_start = max(item.data_start_ms for item in artifacts)
    overlap_end = min(item.data_end_ms for item in artifacts)
    overlap_ms = max(0, overlap_end - overlap_start)
    manifest = {
        "run_id": run_id,
        "kind": "bulk-boundary-pilot",
        "created_at_ms": now_ms,
        "month": bounds.label,
        "selected_symbols": selected,
        "inventory_sha256": snapshot.sha256,
        "git_commit_sha": _git_commit_sha(),
        "spec_version": SPEC_VERSION,
        "data_contract_version": DATA_CONTRACT_VERSION,
        "lifetime_partitions": lifetime_rows,
        "overlap_start_ms": overlap_start if overlap_ms else None,
        "overlap_end_ms": overlap_end if overlap_ms else None,
        "overlap_ms": overlap_ms,
        "status": "COMPLETE",
    }
    atomic_write_bytes(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    typer.echo(f"boundary pilot complete: {run_dir}")
    typer.echo(f"overlap hours: {overlap_ms / 3_600_000:.3f}")


@audit_app.command("funding-mark-pilot")
def funding_mark_pilot(
    probes: Annotated[
        str,
        typer.Option(
            help=(
                "Comma-separated SYMBOL:YYYY-MM probes. "
                "Defaults target historically observed short-funding regimes."
            )
        ),
    ] = (
        "GSTUSDT:2022-06,MINAUSDT:2024-06,"
        "TRBUSDT:2024-08,SCUSDT:2024-03"
    ),
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
) -> None:
    parsed: list[tuple[str, str]] = []
    for item in probes.split(","):
        value = item.strip()
        if not value:
            continue
        if ":" not in value:
            raise typer.BadParameter(
                f"invalid probe {value!r}; expected SYMBOL:YYYY-MM"
            )
        symbol, month = value.split(":", 1)
        parsed.append((symbol.strip().upper(), month.strip()))
    if not parsed:
        raise typer.BadParameter("at least one funding/mark probe is required")

    now_ms = time.time_ns() // 1_000_000
    run_id = f"{now_ms}-historical-funding-mark"
    run_dir = artifact_dir / "funding-mark-pilot" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    rows: list[dict[str, object]] = []
    with BybitPublicClient(cache_dir=None) as client:
        snapshot = collect_inventory(client)
        by_symbol = {item.symbol: item for item in snapshot.instruments}

        for symbol, month in parsed:
            bounds = parse_month(month)
            development_end = parse_month("2025-01").start_ms
            if bounds.end_ms > development_end:
                raise typer.BadParameter(
                    f"{symbol}:{month} is outside development data"
                )
            if symbol not in by_symbol:
                rows.append(
                    {
                        "symbol": symbol,
                        "month": month,
                        "status": "MISSING_FROM_CURRENT_INVENTORY",
                    }
                )
                continue

            diagnostic = mark_price_only_diagnostics(
                client,
                symbol=symbol,
                bounds=bounds,
                run_dir=run_dir,
                now_ms=now_ms,
            )
            mark = diagnostic["mark_price_funding_open_comparison"]
            if not isinstance(mark, dict):
                raise RuntimeError("invalid mark-price diagnostic shape")
            observed = mark.get("observed_funding_intervals_minutes", [])
            if not isinstance(observed, list):
                raise RuntimeError("invalid observed funding interval list")
            observed_ints = [int(value) for value in observed]
            qualifies_fast = bool(observed_ints) and min(observed_ints) <= 240
            funding_count = int(mark.get("funding_event_count", 0))
            comparison_count = int(mark.get("sample_count", 0))
            hour_aligned_count = int(mark.get("hour_aligned_count", 0))
            all_equal = bool(mark.get("all_equal", False))
            complete_comparison = (
                funding_count > 0
                and comparison_count == funding_count
                and hour_aligned_count == funding_count
            )
            row = {
                "symbol": symbol,
                "month": month,
                "current_funding_interval_minutes": by_symbol[symbol].fundingInterval,
                "observed_funding_intervals_minutes": observed_ints,
                "qualifies_fast": qualifies_fast,
                "funding_event_count": funding_count,
                "comparison_count": comparison_count,
                "hour_aligned_count": hour_aligned_count,
                "all_equal": all_equal,
                "complete_comparison": complete_comparison,
                "aux_raw_count": diagnostic.get("aux_raw_count"),
                "aux_payload_index_sha256": diagnostic.get(
                    "aux_payload_index_sha256"
                ),
            }
            rows.append(row)
            atomic_write_bytes(
                run_dir / "diagnostics" / f"{symbol}-{month}.json",
                json.dumps(diagnostic, indent=2, sort_keys=True).encode("utf-8"),
            )

    qualifying = [
        row
        for row in rows
        if row.get("qualifies_fast") is True
        and row.get("complete_comparison") is True
        and row.get("all_equal") is True
    ]
    manifest = {
        "run_id": run_id,
        "kind": "funding-mark-pilot",
        "created_at_ms": now_ms,
        "git_commit_sha": _git_commit_sha(),
        "spec_version": SPEC_VERSION,
        "data_contract_version": DATA_CONTRACT_VERSION,
        "inventory_sha256": snapshot.sha256,
        "requested_probes": [
            {"symbol": symbol, "month": month}
            for symbol, month in parsed
        ],
        "results": rows,
        "qualifying_fast_count": len(qualifying),
        "gate_pass": len(qualifying) >= 2,
    }
    atomic_write_bytes(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    typer.echo(f"funding/mark pilot complete: {run_dir}")
    typer.echo(f"qualifying historical fast-funding probes: {len(qualifying)}")
    if len(qualifying) < 2:
        raise typer.Exit(code=5)


@audit_app.command("bulk-download")
def bulk_download(
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    resume_run: Annotated[
        Path | None,
        typer.Option(help="Existing full bulk-download run directory to resume."),
    ] = None,
    include_open: Annotated[
        bool,
        typer.Option(
            help=(
                "Allow downloading calendar partitions that have not passed the "
                "24h immutability horizon. Default is false."
            )
        ),
    ] = False,
    max_partitions: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Optional smoke-test cap for newly processed partitions.",
        ),
    ] = None,
    resume_decimal_bugfix: Annotated[
        bool,
        typer.Option(
            "--resume-decimal-bugfix",
            help=(
                "Permit the one known v0.1.12 migration from commit "
                "682ffce... after the Decimal precision crash."
            ),
        ),
    ] = False,
) -> None:
    invocation_now_ms = time.time_ns() // 1_000_000
    git_sha = _git_commit_sha()

    with BybitPublicClient(cache_dir=None) as client:
        if resume_run is None:
            snapshot = collect_inventory(client)
            run_id = f"{invocation_now_ms}-full-{snapshot.sha256[:12]}"
            run_dir = artifact_dir / "bulk-download" / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            manifest: dict[str, object] = {
                "run_id": run_id,
                "kind": "bulk-download",
                "created_at_ms": invocation_now_ms,
                "last_invocation_at_ms": invocation_now_ms,
                "inventory_sha256": snapshot.sha256,
                "git_commit_sha": git_sha,
                "spec_version": SPEC_VERSION,
                "data_contract_version": DATA_CONTRACT_VERSION,
                "requests_per_second": client.requests_per_second,
                "full_data_start_ms": FULL_DATA_START_MS,
                "full_data_end_ms": FULL_DATA_END_MS,
                "development_start_ms": STRATEGY_DEVELOPMENT_START_MS,
                "validation_start_ms": STRATEGY_VALIDATION_START_MS,
                "holdout_start_ms": STRATEGY_HOLDOUT_START_MS,
                "holdout_end_ms": STRATEGY_HOLDOUT_END_MS,
                "calendar_completion_after_holdout_end": True,
                "holdout_protection": {
                    "mode": "STRUCTURAL_DATA_ONLY",
                    "strategy_metrics_computed": False,
                    "signals_computed": False,
                    "setup_counts_computed": False,
                    "pnl_computed": False,
                },
                "software_versions": {
                    "python": platform.python_version(),
                    "python_implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "tzdata": _package_version("tzdata"),
                    "httpx": _package_version("httpx"),
                    "pydantic": _package_version("pydantic"),
                    "pyarrow": _package_version("pyarrow"),
                    "typer": _package_version("typer"),
                },
                "identity_relationship_config": {
                    "version": load_identity_config().version,
                    "sha256": load_identity_config().sha256,
                },
                "status": "LIFETIME_DISCOVERY",
            }
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            write_inventory(snapshot, run_dir / "inventory.json")
            instruments = snapshot.instruments
        else:
            run_dir = resume_run
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                raise typer.BadParameter("resume-run has no manifest.json")
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_value, dict):
                raise typer.BadParameter("bulk-download manifest must be an object")
            manifest = manifest_value
            for key, expected in (
                ("kind", "bulk-download"),
                ("spec_version", SPEC_VERSION),
                ("data_contract_version", DATA_CONTRACT_VERSION),
            ):
                if manifest.get(key) != expected:
                    raise typer.BadParameter(
                        f"resume-run mismatch for {key}: "
                        f"{manifest.get(key)!r} != {expected!r}"
                    )

            manifest_git = manifest.get("git_commit_sha")
            if manifest_git != git_sha:
                decimal_bugfix_allowed = (
                    resume_decimal_bugfix
                    and manifest_git
                    == DECIMAL_BUG_AFFECTED_FULL_DOWNLOAD_COMMIT
                )
                if not decimal_bugfix_allowed:
                    raise typer.BadParameter(
                        "resume-run mismatch for git_commit_sha: "
                        f"{manifest_git!r} != {git_sha!r}. "
                        "For a run started on the known Decimal-bug commit, "
                        "update the repository and pass --resume-decimal-bugfix."
                    )

                migrations_value = manifest.get("code_migrations", [])
                if not isinstance(migrations_value, list):
                    raise typer.BadParameter(
                        "resume-run code_migrations must be a list"
                    )
                migrations = migrations_value
                migrations.append(
                    {
                        "kind": "BUGFIX_ONLY",
                        "reason": "DECIMAL128_CANONICAL_HASH_PRECISION",
                        "from_git_commit_sha": manifest_git,
                        "to_git_commit_sha": git_sha,
                        "applied_at_ms": invocation_now_ms,
                        "spec_version": SPEC_VERSION,
                        "data_contract_version": DATA_CONTRACT_VERSION,
                    }
                )
                manifest["initial_git_commit_sha"] = manifest.get(
                    "initial_git_commit_sha",
                    manifest_git,
                )
                manifest["git_commit_sha"] = git_sha
                manifest["code_migrations"] = migrations
                manifest["decimal_bugfix_resume_applied"] = True
                atomic_write_bytes(
                    manifest_path,
                    json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                )

            instruments = load_frozen_instruments(run_dir / "inventory.json")
            manifest["last_invocation_at_ms"] = invocation_now_ms

        lifetime_path = run_dir / "lifetimes.jsonl"
        lifetimes = discover_lifetime_records(
            client,
            instruments,
            path=lifetime_path,
            now_ms=invocation_now_ms,
        )
        unresolved = [
            record.symbol
            for record in lifetimes.values()
            if record.eligible and record.first_trade_ms is None
        ]
        if unresolved:
            manifest["lifetime_unresolved_symbols"] = unresolved
            manifest["status"] = "LIFETIME_DISCOVERY_INCOMPLETE"
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            raise typer.BadParameter(
                f"first trade unresolved for {len(unresolved)} eligible symbols"
            )

        plan_path = run_dir / "partition-plan.json"
        if plan_path.exists():
            tasks = load_partition_plan(plan_path)
        else:
            tasks = build_partition_plan(lifetimes)
            write_partition_plan(plan_path, tasks)

        manifest["planned_partition_count"] = len(tasks)
        manifest["frozen_eligible_symbol_count"] = sum(
            1 for record in lifetimes.values() if record.eligible
        )

        announcement_path = run_dir / "announcements-snapshot.json"
        if announcement_path.exists():
            announcements_value = json.loads(
                announcement_path.read_text(encoding="utf-8")
            )
            announcements = (
                [item for item in announcements_value if isinstance(item, dict)]
                if isinstance(announcements_value, list)
                else []
            )
        else:
            announcements = fetch_announcements_snapshot(
                client,
                run_dir=run_dir,
                now_ms=invocation_now_ms,
            )
        candidates = build_delisting_announcement_candidates(
            instruments,
            announcements,
        )
        atomic_write_bytes(
            run_dir / "delisting-announcement-candidates.json",
            json.dumps(
                candidates,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )

        manifest["announcement_count"] = len(announcements)
        manifest["delisting_announcement_candidate_count"] = len(candidates)
        manifest["status"] = "DOWNLOADING"
        atomic_write_bytes(
            run_dir / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )

        newly_processed = 0
        pending_open = 0
        skipped_complete = 0
        try:
            for index, task in enumerate(tasks, start=1):
                if partition_manifest_is_complete(run_dir, task):
                    skipped_complete += 1
                    continue

                bounds = parse_month(task.month)
                if (
                    not include_open
                    and partition_status(bounds, invocation_now_ms)
                    is PartitionStatus.OPEN
                ):
                    pending_open += 1
                    continue

                partition = process_full_partition(
                    client,
                    task=task,
                    run_dir=run_dir,
                    now_ms=invocation_now_ms,
                )
                newly_processed += 1

                if newly_processed == 1 or newly_processed % 25 == 0:
                    typer.echo(
                        f"[{index}/{len(tasks)}] {task.month} {task.symbol} "
                        f"missing={partition['missing_minutes']} "
                        f"funding={partition['funding_event_count']}"
                    )

                if max_partitions is not None and newly_processed >= max_partitions:
                    break
        except Exception as exc:
            summary = structural_summary(run_dir, tasks)
            atomic_write_bytes(
                run_dir / "structural-summary.json",
                json.dumps(summary, indent=2, sort_keys=True).encode("utf-8"),
            )
            manifest["status"] = "INTERRUPTED_ERROR"
            manifest["last_error"] = f"{type(exc).__name__}: {exc}"
            manifest["last_invocation_new_partitions"] = newly_processed
            manifest["last_invocation_pending_open"] = pending_open
            manifest["last_invocation_skipped_complete"] = skipped_complete
            atomic_write_bytes(
                run_dir / "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            raise

    summary = structural_summary(run_dir, tasks)
    atomic_write_bytes(
        run_dir / "structural-summary.json",
        json.dumps(summary, indent=2, sort_keys=True).encode("utf-8"),
    )

    completed_value = summary["completed_partition_count"]
    if type(completed_value) is not int:
        raise RuntimeError("structural summary completed_partition_count must be int")
    completed = completed_value
    if completed == len(tasks):
        status = "COMPLETE"
    elif pending_open > 0 and max_partitions is None:
        status = "WAITING_FOR_OPEN_PARTITIONS"
    else:
        status = "PARTIAL"

    manifest["status"] = status
    manifest["last_invocation_new_partitions"] = newly_processed
    manifest["last_invocation_pending_open"] = pending_open
    manifest["last_invocation_skipped_complete"] = skipped_complete
    manifest["completed_partition_count"] = completed
    manifest["updated_at_ms"] = time.time_ns() // 1_000_000
    atomic_write_bytes(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )

    typer.echo(f"bulk download run: {run_dir}")
    typer.echo(
        f"status={status}; completed={completed}/{len(tasks)}; "
        f"new={newly_processed}; pending_open={pending_open}"
    )
    typer.echo("No strategy signals, setup counts, or PnL were computed.")


if __name__ == "__main__":
    app()
