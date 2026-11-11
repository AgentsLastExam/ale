"""``ale fleet``: serve the control plane, or lend this machine to it as a Node."""

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


NODE_PORT = 7471


@fleet_app.command("node")
def node(
    control_plane: Annotated[str, typer.Option(help="The control plane's URL.")],
    address: Annotated[
        str,
        typer.Option(
            help="Where every Run's machine reaches this agent, such as http://NAME:7471."
        ),
    ],
    name: Annotated[
        str | None, typer.Option(help="This Node's name. Default: the host name.")
    ] = None,
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "0.0.0.0",
    port: Annotated[
        int | None,
        typer.Option(help="Port to listen on. Default: the port of --address, else 7471."),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option(help="A Provider to lend (repeat). Default: every one that works here."),
    ] = None,
    label: Annotated[
        list[str] | None, typer.Option(help="A KEY=VALUE label for operators (repeat).")
    ] = None,
    reserve_cpus: Annotated[int, typer.Option(help="CPUs kept for the machine.")] = 1,
    reserve_memory_mb: Annotated[int, typer.Option(help="Memory kept for the machine.")] = 4096,
    macos_vm_slots: Annotated[
        int, typer.Option(help="macOS VMs to run at most, when the machine runs them.")
    ] = 2,
) -> None:
    """Lend this machine's Providers to the Fleet until stopped.

    A stopping agent destroys every Sandbox it holds.
    """
    import re
    import socket
    from urllib.parse import urlsplit

    from ale.core.errors import AleError
    from ale.run.cli.main import _run_async
    from ale.run.fleet.node import NodeAgent, NodeSettings, probe
    from ale.run.sources import cache_root

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = fleet_token()
    labels = dict(item.partition("=")[::2] for item in label or ())
    node_name = name or re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")
    state = cache_root() / "fleet"
    state.mkdir(parents=True, exist_ok=True)
    settings = NodeSettings(
        name=node_name,
        control_url=control_plane.rstrip("/"),
        address=address.rstrip("/"),
        host=host,
        port=port or urlsplit(address).port or NODE_PORT,
        providers=tuple(provider or ()),
        labels=labels,
        reserve_cpus=reserve_cpus,
        reserve_memory_mb=reserve_memory_mb,
        macos_vm_slots=macos_vm_slots,
        state_dir=state,
    )

    async def main() -> None:
        declaration, providers = await probe(settings)
        for profile in declaration.profiles:
            typer.echo(
                f"lending {profile.provider}: {profile.kind} {profile.os}/{profile.arch} "
                f"network={','.join(sorted(profile.network_modes))}"
            )
        agent = NodeAgent(
            settings,
            token,
            declaration=declaration,
            providers=providers,
        )
        await agent.serve()

    try:
        _run_async(main())
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(4) from error
