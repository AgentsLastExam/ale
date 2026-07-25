"""The ``ale`` command line entry point.

Commands are added as they are implemented; see
``specs/001-phase0-demo-e2e/contracts/cli.md`` for the full Phase 0 surface
(``run``, ``validate``, ``lint``, ``new task``).
"""

from __future__ import annotations

from typing import Annotated

import typer

from ale.run import __version__

app = typer.Typer(
    name="ale",
    help="Agents' Last Exam evaluation orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


# An explicit callback keeps subcommand routing even while only one command exists.
@app.callback()
def root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the engine version and exit.",
        ),
    ] = False,
) -> None:
    """Run, validate and author ALE evaluation tasks."""


@app.command()
def version() -> None:
    """Print the engine version."""
    typer.echo(__version__)


def main() -> None:
    """Console script entry point."""
    app()
