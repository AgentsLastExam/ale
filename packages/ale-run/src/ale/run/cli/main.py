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
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, Limits
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.kits import read_lock, scan_kits, write_lock
from ale.run.ledger import Ledger, episode_identity
from ale.run.lint import lint_repository
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.scaffold import scaffold_task
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
    episodes: Annotated[int, typer.Option("-n", "--episodes", min=1)] = 1,
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
    flags = [*(overrides or []), f"episodes={episodes}"]
    if no_resume:
        flags.append("resume=false")
    settings = _config(config, flags, agent=agent, model=model, provider=provider)
    exit_code = asyncio.run(
        _run_one(reference, settings, runs_dir, tasks_ref, run_id, require_reportable)
    )
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
    reference: str,
    settings: RunConfig,
    runs_dir: Path,
    tasks_ref: str | None,
    run_id: str | None = None,
    require_reportable: bool = False,
) -> int:
    try:
        resolved = resolve(reference, registry=_registry(), ref=tasks_ref)
        task = next(iter(ManifestTaskset(resolved.task_dir).load()))
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE

    run_id = run_id or uuid.uuid4().hex[:8]
    run_dir = runs_dir / run_id
    harness = _harness(settings)
    inputs = ProvenanceInputs(
        source=resolved.source,
        agent=agent_provenance(harness, settings.agent.model),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )

    identity = episode_identity(
        task.spec,
        agent=f"{inputs.agent.harness}@{inputs.agent.version}",
        seed=settings.seed,
        config_hash=settings.config_hash,
    )

    gateway = None
    proxy = None
    gateway_url, token, proxy_url = "", "", ""
    needs_model = settings.agent.name not in {"oracle", "nop"}
    allowed = frozenset(task.spec.network.allowed_hosts)

    if needs_model:
        api_key, upstream = provider_credentials()
        if not api_key:
            typer.echo("no ANTHROPIC_API_KEY: copy .env.example to .env and fill it in", err=True)
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
                # What the task declared, and nothing it did not: the proxy refuses the
                # rest, so an agent cannot widen its own reach by asking.
                allowed_hosts=allowed,
            )
        )
        token = session.token

        if allowed:
            proxy = EgressProxy(gateway.sessions, host=_gateway_host(settings))
            proxy_url = await proxy.start()

    ledger = Ledger(run_dir)
    ledger.open_run(run_id, settings.config_hash)
    # Resume compares what an episode *is*, so a rerun with a different seed or a
    # different agent is correctly new work rather than something to skip. Repeats of
    # one identity are counted, not merely detected: asking for five episodes after
    # three finished must run two more, and a set could only ever say "yes, some".
    done = (
        sum(1 for row in ledger.episodes(run_id) if row.identity == identity and row.succeeded)
        if settings.resume
        else 0
    )
    failures = 0

    try:
        for index in range(settings.episodes):
            if index < done:
                typer.echo(f"skip  {task.spec.label}: episode {index + 1} already complete")
                continue

            result = await run_episode(
                task,
                StandardEnvironment(harness, max_steps=settings.gateway.limits.max_steps),
                _provider(settings),
                run_dir=run_dir,
                gateway_url=gateway_url,
                token=token,
                model=settings.agent.model,
                seed=settings.seed,
                work_dir=settings.work_dir,
                collect_artifacts=settings.artifacts.collect == "host",
                provenance=inputs,
                proxy_url=proxy_url,
            )
            ledger.start_episode(
                episode_id=result.episode_id, run_id=run_id, identity=identity, spec=task.spec
            )
            ledger.finish_episode(
                result.episode_id,
                result.verdict,
                result.lock.model_dump(mode="json") if result.lock else None,
            )
            ledger.event(result.episode_id, "finished", status=result.verdict.status.value)

            _report(result)
            reportable = _check_reportable(result)
            unusable = require_reportable and not reportable
            if result.verdict.status is not Status.COMPLETED or unusable:
                failures += 1
    finally:
        ledger.close()
        if proxy is not None:
            await proxy.stop()
        if gateway is not None:
            await gateway.stop()

    typer.echo(f"run {run_id}: {settings.episodes - failures}/{settings.episodes} completed")
    return 0 if failures == 0 else EXIT_SOME_FAILED


def _check_reportable(result) -> bool:  # type: ignore[no-untyped-def]
    """Say whether this result could back a published number (Constitution III).

    Always said out loud, never silently enforced. Running a task from a local path is
    how authoring works and must stay frictionless, but the run still states plainly
    that the result is not publishable — the failure mode worth designing against is a
    number that quietly loses its provenance, not an author who is told about it.
    ``--require-reportable`` is what turns the statement into a gate.
    """
    if result.lock is None:
        typer.echo("  not reportable: no provenance was recorded", err=True)
        return False
    problems = result.lock.missing_for_report()
    for problem in problems:
        typer.echo(f"  not reportable: {problem}", err=True)
    return not problems


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
            work_dir=settings.work_dir,
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
