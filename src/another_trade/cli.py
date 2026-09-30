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
    PilotAbort,
    download_symbol_month,
    instrument_covers_month,
    parse_month,
    pilot_symbol_diagnostics,
    select_default_pilot_symbols,
    write_pilot_summary,
)
from another_trade.audit.coverage import run_sample_coverage, write_coverage
from another_trade.audit.identity import load_identity_config
from another_trade.audit.inventory import (
    collect_inventory,
    currently_eligible_crypto_perpetual,
    write_inventory,
)
from another_trade.bybit.client import BybitPublicClient
from another_trade.io import atomic_write_bytes

SPEC_VERSION = "v0.2.5"
DATA_CONTRACT_VERSION = "v0.1.7"

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


if __name__ == "__main__":
    app()
