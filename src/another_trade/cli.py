from __future__ import annotations

import json
import platform
import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer

from another_trade.audit.coverage import run_sample_coverage, write_coverage
from another_trade.audit.identity import load_identity_config
from another_trade.audit.inventory import collect_inventory, write_inventory
from another_trade.bybit.client import BybitPublicClient
from another_trade.io import atomic_write_bytes

SPEC_VERSION = "v0.2.4"
DATA_CONTRACT_VERSION = "v0.1.6"

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


if __name__ == "__main__":
    app()
