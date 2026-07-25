"""The ``ale`` command line.

Kept thin on purpose: it resolves a reference, assembles the pieces, and prints what
came back. Every decision that matters — how a task loads, how an episode runs, what a
verdict means — lives in the library, so the same behaviour is available to anything
that imports it.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated

import typer

from ale.core.config import RunConfig, load_run_config
from ale.core.errors import AleError
from ale.core.verdict import Status
from ale.run import __version__
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.kits import read_lock, scan_kits, write_lock
from ale.run.secrets import provider_credentials
from ale.run.sources import Registry, resolve
from ale.run.tasksets.manifest import ManifestTaskset

app = typer.Typer(
    name="ale",
    help="Agents' Last Exam evaluation orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)
kit_app = typer.Typer(help="Work with the shared libraries a domain ships to its tasks.")
app.add_typer(kit_app, name="kit")

EXIT_SOME_FAILED = 2
EXIT_BAD_REFERENCE = 3
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
    reference: Annotated[str, typer.Argument(help="Task path, or <domain>/<task>")],
    agent: Annotated[
        str, typer.Option("--agent", help="claude-code, oracle or nop")
    ] = "claude-code",
    model: Annotated[str, typer.Option("--model")] = "",
    provider: Annotated[str, typer.Option("--provider")] = "docker",
    config: Annotated[Path | None, typer.Option("--config", help="Run configuration TOML")] = None,
    overrides: Annotated[list[str] | None, typer.Option("--set", help="key.path=value")] = None,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    tasks_ref: Annotated[
        str | None, typer.Option("--tasks-ref", help="Override the registry pin")
    ] = None,
) -> None:
    """Run one task and report its verdict."""
    settings = _config(config, overrides, agent=agent, model=model, provider=provider)
    exit_code = asyncio.run(_run_one(reference, settings, runs_dir, tasks_ref))
    raise typer.Exit(exit_code)


@app.command()
def validate(
    reference: Annotated[str, typer.Argument(help="Task or repository path")],
    provider: Annotated[str, typer.Option("--provider")] = "docker",
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
) -> None:
    """Run each task's oracle in place of the agent and require its declared score.

    This is the admission gate: a task nobody can solve is a broken task, and finding
    that out costs one container rather than one agent run.
    """
    settings = _config(None, None, agent="oracle", model="", provider=provider)
    exit_code = asyncio.run(_validate(reference, settings, runs_dir))
    raise typer.Exit(exit_code)


@kit_app.command("lock")
def kit_lock(
    repo: Annotated[Path, typer.Argument(help="Task repository root")] = Path(),
    check: Annotated[bool, typer.Option("--check", help="Fail if the lock is stale")] = False,
) -> None:
    """Regenerate ``kits.lock.yaml`` — or verify it still matches the kits on disk."""
    current = scan_kits(repo)
    if check:
        if read_lock(repo) != current:
            typer.echo("kits.lock.yaml is out of date; run `ale kit lock`", err=True)
            raise typer.Exit(1)
        typer.echo(f"{len(current.kits)} kit(s) locked and current")
        return
    path = write_lock(repo, current)
    typer.echo(f"wrote {path} ({len(current.kits)} kit(s))")


# --- internals ---


def _config(
    config: Path | None,
    overrides: list[str] | None,
    *,
    agent: str,
    model: str,
    provider: str,
) -> RunConfig:
    flags = [f"agent.name={agent}", f"provider={provider}", *(overrides or [])]
    if model:
        flags.append(f"agent.model={model}")
    try:
        return load_run_config(run_path=config, overrides=flags)
    except AleError as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error


def _harness(settings: RunConfig):  # type: ignore[no-untyped-def]
    match settings.agent.name:
        case "oracle":
            return OracleHarness()
        case "nop":
            return NopHarness()
        case "claude-code":
            return ClaudeCodeHarness(cli_version=settings.agent.version, **settings.agent.kwargs)
        case unknown:
            raise AleError(f"unknown agent {unknown!r}; try claude-code, oracle or nop")


def _provider(settings: RunConfig):  # type: ignore[no-untyped-def]
    if settings.provider != "docker":
        raise AleError(f"unknown provider {settings.provider!r}; only docker exists so far")
    from ale.run.providers.docker import DockerProvider

    return DockerProvider()


def _registry() -> Registry | None:
    engine_root = Path(__file__).resolve().parents[5]
    for candidate in (Path("registry.toml"), engine_root / "registry.toml"):
        if candidate.is_file():
            return Registry.load(candidate)
    return None


async def _run_one(
    reference: str, settings: RunConfig, runs_dir: Path, tasks_ref: str | None
) -> int:
    try:
        resolved = resolve(reference, registry=_registry(), ref=tasks_ref)
        task = next(iter(ManifestTaskset(resolved.task_dir).load()))
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE

    gateway = None
    gateway_url, token = "", ""
    needs_model = settings.agent.name not in {"oracle", "nop"}

    if needs_model:
        api_key, upstream = provider_credentials()
        if not api_key:
            typer.echo(
                "no ANTHROPIC_API_KEY: copy .env.example to .env and fill it in", err=True
            )
            return EXIT_BAD_REFERENCE
        gateway = Gateway(
            api_key=api_key,
            upstream=settings.gateway.base_url or upstream,
            host=_gateway_host(settings),
        )
        gateway_url = await gateway.start()
        session = gateway.open_session(
            GatewaySession(
                episode_id="pending",
                model=settings.agent.model,
                limits=Limits(**settings.gateway.limits.model_dump()),
            )
        )
        token = session.token

    try:
        result = await run_episode(
            task,
            StandardEnvironment(_harness(settings)),
            _provider(settings),
            run_dir=runs_dir / uuid.uuid4().hex[:8],
            gateway_url=gateway_url,
            token=token,
            model=settings.agent.model,
            seed=settings.seed,
        )
    finally:
        if gateway is not None:
            await gateway.stop()

    _report(result)
    return 0 if result.verdict.status is Status.COMPLETED else EXIT_SOME_FAILED


async def _validate(reference: str, settings: RunConfig, runs_dir: Path) -> int:
    try:
        resolved = resolve(reference, registry=_registry())
        tasks = list(ManifestTaskset(resolved.task_dir).load())
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE

    failures = 0
    for task in tasks:
        spec = task.spec
        if spec.validate_.mode == "manual":
            typer.echo(f"skip  {spec.label}: manual validation ({spec.validate_.reason})")
            continue

        result = await run_episode(
            task,
            StandardEnvironment(OracleHarness()),
            _provider(settings),
            run_dir=runs_dir / "validate",
        )
        reward = result.verdict.primary_reward
        passed = (
            result.verdict.status is Status.COMPLETED
            and reward is not None
            and reward >= spec.validate_.min_reward
        )
        failures += 0 if passed else 1
        mark = "ok  " if passed else "FAIL"
        detail = f"reward {reward}" if reward is not None else str(result.verdict.status)
        typer.echo(f"{mark}  {spec.label}: {detail}")

    return 0 if failures == 0 else EXIT_SOME_FAILED


def _gateway_host(settings: RunConfig) -> str:
    """Bind where a sandbox can reach us.

    Containers on an isolated bridge reach the host through its gateway address, so
    binding to all interfaces is what makes the deny-all topology usable at all.
    """
    return "0.0.0.0" if settings.provider == "docker" else "127.0.0.1"


def _report(result) -> None:  # type: ignore[no-untyped-def]
    verdict = result.verdict
    typer.echo(f"{verdict.status.value}  {result.episode_id}  ({result.duration_sec:.1f}s)")
    if verdict.rewards:
        typer.echo(f"  rewards: {verdict.rewards}")
    if verdict.failure:
        typer.echo(f"  {verdict.failure.error_class}: {verdict.failure.message}")
    typer.echo(f"  run dir: {result.run_dir}")


def main() -> None:
    """Console script entry point."""
    app()
