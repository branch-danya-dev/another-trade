from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from another_trade.audit.coverage import sample_coverage, write_coverage
from another_trade.audit.inventory import collect_inventory, write_inventory
from another_trade.bybit.client import BybitPublicClient

app = typer.Typer(no_args_is_help=True)
audit_app = typer.Typer(no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@audit_app.command("inventory")
def inventory(
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    cache_dir: Annotated[Path, typer.Option()] = Path(".cache/bybit"),
) -> None:
    with BybitPublicClient(cache_dir=cache_dir) as client:
        snapshot = collect_inventory(client)
    write_inventory(snapshot, artifact_dir / "inventory.json")
    typer.echo(
        f"inventory: {len(snapshot.instruments)} symbols "
        f"(Trading={snapshot.trading_count}, Closed={snapshot.closed_count})"
    )
    typer.echo(f"snapshot sha256: {snapshot.sha256}")


@audit_app.command("sample-coverage")
def coverage(
    artifact_dir: Annotated[Path, typer.Option()] = Path("artifacts/data-audit"),
    cache_dir: Annotated[Path, typer.Option()] = Path(".cache/bybit"),
    max_symbols: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Safety cap. Milestone 1 performs only three small kline probes per symbol.",
        ),
    ] = None,
) -> None:
    with BybitPublicClient(cache_dir=cache_dir) as client:
        snapshot = collect_inventory(client)
        write_inventory(snapshot, artifact_dir / "inventory.json")
        results = sample_coverage(client, snapshot, max_symbols=max_symbols)
    write_coverage(results, artifact_dir)
    typer.echo(f"sample coverage written for {len(results)} symbols")
    typer.echo("No full-history download was performed.")


if __name__ == "__main__":
    app()
