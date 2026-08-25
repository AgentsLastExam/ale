"""The ``ale`` command line.

Kept thin on purpose: it resolves a reference, assembles the pieces, and prints what
came back. Every decision that matters — how a task loads, how an episode runs, what a
verdict means — lives in the library, so the same behaviour is available to anything
that imports it.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from ale.core.errors import AleError, ProviderCapabilityError
from ale.run import __version__
from ale.run.assets import (
    AssetSyncResult,
    asset_status,
    pull_assets,
    push_assets,
)
from ale.run.cli.tasks import (
    EXIT_BAD_REFERENCE,
    _config,
    _prepare_only,
    _run_one,
    _validate,
)
from ale.run.lint import lint_repository
from ale.run.providers.docker import (
    HANDLE_PREFIX as DOCKER_HANDLE_PREFIX,
)
from ale.run.providers.docker import (
    destroy_retained as destroy_retained_docker,
)
from ale.run.providers.docker import (
    list_retained as list_retained_docker,
)
from ale.run.providers.qemu import (
    HANDLE_PREFIX as QEMU_HANDLE_PREFIX,
)
from ale.run.providers.qemu import (
    destroy_retained as destroy_retained_qemu,
)
from ale.run.providers.qemu import (
    list_retained as list_retained_qemu,
)
from ale.run.scaffold import scaffold_task

app = typer.Typer(
    name="ale",
    help="Agents' Last Exam evaluation orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)
assets_app = typer.Typer(help="Synchronize canonical Task assets.", no_args_is_help=True)
app.add_typer(assets_app, name="assets")
vm_image_app = typer.Typer(help="Publish and acquire prebuilt VM disks.", no_args_is_help=True)
app.add_typer(vm_image_app, name="vm-image")
sandbox_app = typer.Typer(help="Inspect and remove retained sandboxes.", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")

EXIT_PROVIDER = 4


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version."
        ),
    ] = False,
) -> None:
    """Run, validate and author ALE evaluation tasks."""


@app.command()
def run(
    reference: Annotated[str, typer.Argument(help="Task or collection filesystem path")],
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="claude-code, codex-cli, grok-build, openclaw-cli, computer-use, oracle or nop",
        ),
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    auth: Annotated[
        str | None,
        typer.Option("--auth", help="auto, api-key or subscription"),
    ] = None,
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Model endpoint; defaults to Anthropic's"),
    ] = "",
    api_key_env: Annotated[
        str,
        typer.Option("--api-key-env", help="Which .env variable holds the key for it"),
    ] = "",
    config: Annotated[Path | None, typer.Option("--config", help="Run configuration TOML")] = None,
    overrides: Annotated[list[str] | None, typer.Option("--set", help="key.path=value")] = None,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    episodes: Annotated[int, typer.Option("-n", "--episodes", min=1)] = 1,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            help="How many episodes to run at once; bounded by what the host can hold",
        ),
    ] = 1,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Resume this run instead of starting one")
    ] = None,
    no_resume: Annotated[
        bool, typer.Option("--no-resume", help="Re-run episodes already recorded complete")
    ] = False,
    require_reportable: Annotated[
        bool,
        typer.Option(
            "--require-reportable",
            help="Fail unless every episode's provenance could back a published result",
        ),
    ] = False,
) -> None:
    """Run a task and report its verdict.

    Re-invoking with the same ``--run-id`` continues where an interrupted run stopped:
    episodes are matched by what they are, not by when they ran.
    """
    flags = [*(overrides or []), f"episodes={episodes}", f"concurrency={concurrency}"]
    if no_resume:
        flags.append("resume=false")
    if auth:
        flags.append(f"agent.authentication={auth}")
    if base_url:
        flags.append(f"gateway.base_url={base_url}")
    if api_key_env:
        flags.append(f"gateway.api_key_env={api_key_env}")
    settings = _config(config, flags, agent=agent, model=model)
    exit_code = asyncio.run(_run_one(reference, settings, runs_dir, run_id, require_reportable))
    raise typer.Exit(exit_code)


@app.command()
def validate(
    reference: Annotated[str, typer.Argument(help="Task or Task collection path")],
    config: Annotated[Path | None, typer.Option("--config", help="Run configuration TOML")] = None,
    overrides: Annotated[list[str] | None, typer.Option("--set", help="key.path=value")] = None,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
) -> None:
    """Require untouched all-zero and record oracle quality for every task.

    Verification uses the same run-level judge configuration as an ordinary episode.
    """
    settings = _config(config, overrides, agent="oracle", model="")
    exit_code = asyncio.run(_validate(reference, settings, runs_dir))
    raise typer.Exit(exit_code)


@app.command()
def prepare(
    reference: Annotated[str, typer.Argument(help="Task or Task collection path")],
    config: Annotated[Path | None, typer.Option("--config", help="Run configuration TOML")] = None,
    overrides: Annotated[list[str] | None, typer.Option("--set", help="key.path=value")] = None,
) -> None:
    """Build or acquire Task images without starting an episode."""
    settings = _config(config, overrides, agent="oracle", model="")
    raise typer.Exit(asyncio.run(_prepare_only(reference, settings)))


@app.command()
def lint(
    reference: Annotated[Path, typer.Argument(help="Task or repository path")] = Path(),
) -> None:
    """Check tasks without starting a container.

    The cheap half of admission: everything answerable from the files alone. Run it on
    every save; run `ale validate` when you want a task actually solved.
    """
    findings = lint_repository(reference)
    for finding in findings:
        typer.echo(str(finding), err=True)
    if findings:
        typer.echo(f"{len(findings)} problem(s)", err=True)
        raise typer.Exit(1)
    typer.echo("ok")


@vm_image_app.command("push")
def vm_image_push(
    source: Annotated[Path, typer.Argument(help="Local qcow2 disk")],
    reference: Annotated[str, typer.Argument(help="Destination OCI image reference")],
) -> int:
    """Publish a local qcow2 through an OCI registry."""
    from ale.run.images import publish_vm_image

    typer.echo(f"pushing {source} to {reference}")
    try:
        resolved = asyncio.run(publish_vm_image(source, reference))
    except AleError as error:
        typer.echo(str(error), err=True)
        return EXIT_BAD_REFERENCE
    typer.echo(f"ok    {resolved}")
    return 0


@vm_image_app.command("pull")
def vm_image_pull(
    reference: Annotated[str, typer.Argument(help="Source OCI image reference")],
    destination: Annotated[Path, typer.Argument(help="Local qcow2 destination")],
) -> int:
    """Acquire a published qcow2 from an OCI registry.

    The disk is published as the single layer of a container image, so it arrives over the
    registry everyone is already authenticated to.
    """
    from ale.core.sandbox import ImageRef
    from ale.run.images import resolve_vm_image

    typer.echo(f"pulling {reference}")
    try:
        prepared = asyncio.run(resolve_vm_image(ImageRef(kind="vm", reference=reference)))
    except AleError as error:
        typer.echo(str(error), err=True)
        return EXIT_BAD_REFERENCE

    cached = Path(prepared.runtime_ref)
    target = destination.resolve()
    if target != cached:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, target)
    typer.echo(f"ok    {target}")
    return 0


@app.command("new-task")
def new_task(
    path: Annotated[Path, typer.Argument(help="Where to create the task folder")],
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing folder")] = False,
) -> None:
    """Scaffold a task that passes `ale lint` immediately.

    The scaffold solves itself: its oracle writes what its verifier expects, so a new
    task starts from a green `ale validate` and the author changes one thing at a time
    from a known-good state.
    """
    try:
        created = scaffold_task(path, force=force)
    except AleError as error:
        typer.echo(f"{error}", err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error
    typer.echo(f"created {created}")
    typer.echo(f"next: ale lint {created}  &&  ale validate {created}")


@assets_app.command("status")
def assets_status(
    paths: Annotated[
        list[Path],
        typer.Argument(help="One or more Task or Task collection paths"),
    ],
) -> None:
    """Inspect Task-local assets without reading their file contents."""
    try:
        statuses = asset_status(paths)
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error
    for item in statuses:
        typer.echo(
            f"{'dirty' if item.dirty else 'clean':6} "
            f"{item.repository}/{item.task_path} commit={item.commit or 'none'}"
        )


@assets_app.command("pull")
def assets_pull(
    paths: Annotated[
        list[Path],
        typer.Argument(help="One or more Task or Task collection paths"),
    ],
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Hugging Face collection slug"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace locally dirty selected asset roots"),
    ] = False,
) -> None:
    """Pull selected asset roots directly into their Task folders."""
    try:
        results = pull_assets(paths, collection=collection, force=force)
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error
    _print_asset_sync(results)


@assets_app.command("push")
def assets_push(
    paths: Annotated[
        list[Path],
        typer.Argument(help="One or more Task or Task collection paths"),
    ],
    collection: Annotated[
        str | None,
        typer.Option("--collection", help="Hugging Face collection slug"),
    ] = None,
) -> None:
    """Push selected Task-local stage asset roots exactly."""
    try:
        results = push_assets(paths, collection=collection)
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error
    _print_asset_sync(results)


def _print_asset_sync(results: Sequence[AssetSyncResult]) -> None:
    for result in results:
        typer.echo(
            f"ok {result.repository} dataset={result.remote_repo_id} "
            f"collection={result.collection_slug} commit={result.commit}"
        )
        for task in result.tasks:
            typer.echo(f"  {task.task_path} {'dirty' if task.dirty else 'clean'}")


@sandbox_app.command("list")
def sandbox_list() -> None:
    """List ALE-managed sandboxes requested for retention."""
    try:
        records = asyncio.run(_list_retained_sandboxes())
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_PROVIDER) from error
    for record in records:
        typer.echo(
            f"{record['handle']} episode={record['episode']} role={record['role']} "
            f"gpus={','.join(record['gpus']) or '-'} cleanup={record['cleanup_command']}"
        )


@sandbox_app.command("destroy")
def sandbox_destroy(handle: Annotated[str, typer.Argument(help="Retained sandbox handle")]) -> None:
    """Destroy one ALE-retained sandbox and its owned resources."""
    try:
        asyncio.run(_destroy_retained_sandbox(handle))
    except AleError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_PROVIDER) from error
    typer.echo(f"destroyed {handle}")


async def _list_retained_sandboxes() -> list[dict[str, object]]:
    return sorted(
        [*await list_retained_docker(), *await list_retained_qemu()],
        key=lambda item: str(item["handle"]),
    )


async def _destroy_retained_sandbox(handle: str) -> None:
    if handle.startswith(QEMU_HANDLE_PREFIX):
        await destroy_retained_qemu(handle)
    elif handle.startswith(DOCKER_HANDLE_PREFIX):
        await destroy_retained_docker(handle)
    else:
        raise ProviderCapabilityError(
            "retained sandbox handle must start with 'docker:' or 'qemu:'"
        )


def main() -> None:
    """Console script entry point."""
    app()
