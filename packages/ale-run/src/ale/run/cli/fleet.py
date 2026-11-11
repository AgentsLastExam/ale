"""``ale fleet``: serve the control plane."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

fleet_app = typer.Typer(help="Pool Sandbox capacity across machines.", no_args_is_help=True)

#: Where the control plane listens unless told otherwise.
CONTROL_PORT = 7470
TOKEN_VARIABLE = "ALE_FLEET_TOKEN"


def fleet_token() -> str:
    """The Fleet token from the environment, which every Fleet command needs."""
    token = os.environ.get(TOKEN_VARIABLE, "")
    if not token:
        typer.echo(f"{TOKEN_VARIABLE} is not set; every Fleet peer needs the Fleet token", err=True)
        raise typer.Exit(2)
    return token


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "ale" / "fleet"


@fleet_app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = CONTROL_PORT,
    db: Annotated[
        Path | None,
        typer.Option(help="The ledger's SQLite file. Default: ~/.local/state/ale/fleet/fleet.db"),
    ] = None,
) -> None:
    """Run the control plane: the ledger, placement and the API Nodes and Runs call."""
    from ale.run.cli.main import _run_async
    from ale.run.fleet.control import ControlPlane
    from ale.run.fleet.server import serve as serve_control
    from ale.run.fleet.store import Store

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = fleet_token()
    store = Store(db or _state_dir() / "fleet.db")
    typer.echo(f"fleet control plane on http://{host}:{port}")
    try:
        _run_async(serve_control(ControlPlane(store, token), token, host=host, port=port))
    finally:
        store.close()
