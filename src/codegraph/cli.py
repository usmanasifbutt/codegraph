"""`codegraph` command line. Exit codes: 0 success, 1 runtime failure, 2 usage error."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from codegraph.config import ConfigError, ConnectError, connect, load_settings
from codegraph.model import Summary
from codegraph.pipeline import extract
from codegraph.store.schema import ensure_schema
from codegraph.store.writer import write_batch

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2

app = typer.Typer(
    help="Build and query a code graph of a Python repository.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def _root() -> None:
    """codegraph command line."""


def _log(message: str) -> None:
    typer.echo(message, err=True)


def _print_summary(s: Summary) -> None:
    typer.echo(f"Indexed repo '{s.repo}' in {s.elapsed_seconds:.2f}s")
    typer.echo("Nodes:")
    for label, n in s.nodes.items():
        typer.echo(f"  {label:<14}{n:>8}")
    typer.echo("Relationships:")
    for rel_type, n in s.relationships.items():
        typer.echo(f"  {rel_type:<14}{n:>8}")
    typer.echo(f"Parse errors: {len(s.parse_errors)}")
    for e in s.parse_errors:
        typer.echo(f"  {e['path']}: {e['error']}")
    if s.warnings:
        typer.echo(f"Warnings: {len(s.warnings)}")
        for w in s.warnings:
            typer.echo(f"  {w}")


@app.command()
def index(
    path: Annotated[Path, typer.Argument(help="Repository root to index.")],
    repo_name: Annotated[
        str | None, typer.Option("--repo-name", help="Repository name (default: folder name).")
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Glob of paths to skip, e.g. 'migrations/**'. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the summary as one JSON object.")
    ] = False,
) -> None:
    """Index a Python repository into Neo4j, replacing any earlier graph for it."""
    if not path.exists():
        _log(f"error: path does not exist: {path}")
        raise typer.Exit(EXIT_USAGE)
    if not path.is_dir():
        _log(f"error: path is not a directory: {path}")
        raise typer.Exit(EXIT_USAGE)
    root = path.resolve()
    repo = repo_name or root.name
    try:
        settings = load_settings()
    except ConfigError as exc:
        _log(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from None

    started = time.monotonic()
    try:
        _log(f"connecting to {settings.uri} ...")
        driver = connect(settings)
    except ConnectError as exc:
        _log(f"error: {exc}")
        raise typer.Exit(EXIT_FAILURE) from None

    try:
        ensure_schema(driver)
        _log(f"parsing {root} ...")
        batch = extract(root, repo, exclude or [])
        _log(f"writing {sum(batch.node_counts().values())} nodes, {len(batch.rels)} relationships")
        write_batch(
            driver,
            batch,
            root=str(root),
            # null values remove any git metadata left by an earlier UI clone of this name
            repo_props={
                "source": "local",
                "source_url": None,
                "branch": None,
                "excludes": list(exclude or []),
            },
        )
    except Exception as exc:  # any failure after connecting is a runtime failure
        _log(f"error: indexing '{repo}' failed: {type(exc).__name__}: {exc}")
        raise typer.Exit(EXIT_FAILURE) from None
    finally:
        driver.close()

    summary = Summary(
        repo=repo,
        nodes=batch.node_counts(),
        relationships=batch.rel_counts(),
        parse_errors=batch.parse_errors,
        warnings=batch.warnings,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    if json_output:
        typer.echo(json.dumps(summary.to_dict()))
    else:
        _print_summary(summary)


def streamlit_command(port: int, address: str) -> list[str]:
    app_path = Path(__file__).parent / "ui" / "app.py"
    return [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.address", address,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]  # fmt: skip


@app.command()
def ui(
    port: Annotated[int, typer.Option("--port", help="Port for the web UI.")] = 8501,
    address: Annotated[
        str, typer.Option("--address", help="Interface to bind (keep 127.0.0.1: no auth).")
    ] = "127.0.0.1",
) -> None:
    """Start the web UI: connect and index repositories, then ask questions."""
    _log(f"codegraph UI on http://{'localhost' if address == '127.0.0.1' else address}:{port}")
    try:
        code = subprocess.call(streamlit_command(port, address))
    except KeyboardInterrupt:
        code = EXIT_OK
    raise typer.Exit(code)


def main() -> None:
    app()
