"""The ``ale`` command line.

Kept thin on purpose: it resolves a reference, assembles the pieces, and prints what
came back. Every decision that matters — how a task loads, how an episode runs, what a
verdict means — lives in the library, so the same behaviour is available to anything
that imports it.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Annotated

import typer

from ale.core.config import RunConfig, load_run_config, select_agent_name
from ale.core.errors import AleError
from ale.core.harness import EffectiveAgentResources
from ale.core.verdict import Status
from ale.run import __version__
from ale.run.agent_resources import resolve_agent_resources
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
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
PRESET_DIR = Path(__file__).resolve().parents[1] / "presets"


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
        str | None, typer.Option("--agent", help="claude-code, computer-use, oracle or nop")
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Model endpoint; defaults to Anthropic's"),
    ] = "",
    api_key_env: Annotated[
        str,
        typer.Option("--api-key-env", help="Which .env variable holds the key for it"),
    ] = "",
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Run configuration TOML")] = None,
    overrides: Annotated[list[str] | None, typer.Option("--set", help="key.path=value")] = None,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    tasks_ref: Annotated[
        str | None, typer.Option("--tasks-ref", help="Override the registry pin")
    ] = None,
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
    if base_url:
        flags.append(f"gateway.base_url={base_url}")
    if api_key_env:
        flags.append(f"gateway.api_key_env={api_key_env}")
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


@app.command("pull-guest")
def pull_guest(
    reference: Annotated[
        str, typer.Option("--from", help="Published guest image to take the disk from")
    ] = "",
    dest: Annotated[Path | None, typer.Option("--to", help="Where to write the qcow2")] = None,
) -> int:
    """Fetch the virtual-machine guest disk that the VM backend boots.

    The disk is published as the single layer of a container image, so it arrives over the
    registry everyone is already authenticated to. Building one instead takes about forty
    minutes and an Ubuntu ISO; this takes as long as the download.
    """
    from ale.run.providers.qemu import GUEST_IMAGE, QemuProvider

    source = reference or GUEST_IMAGE
    target = dest or QemuProvider().image
    target.parent.mkdir(parents=True, exist_ok=True)

    typer.echo(f"pulling {source}")
    if subprocess.run(["docker", "pull", source]).returncode != 0:
        typer.echo("could not pull the guest image", err=True)
        return EXIT_BAD_REFERENCE

    # Copied out of a stopped container rather than run: the image has no command and is
    # not meant to have one — it is a disk in transit, not something to execute.
    # An entrypoint has to be named even though the container is never started: the image
    # is `FROM scratch` and declares none of its own, and `docker create` refuses without
    # one. It is never executed — the container exists only to be copied out of.
    created = subprocess.run(
        ["docker", "create", "--entrypoint", "/disk.qcow2", source],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        typer.echo(f"could not stage the guest image: {created.stderr.strip()}", err=True)
        return EXIT_BAD_REFERENCE
    container = created.stdout.strip()
    try:
        typer.echo(f"writing {target}")
        copied = subprocess.run(
            ["docker", "cp", f"{container}:/disk.qcow2", str(target)], capture_output=True
        )
        if copied.returncode != 0:
            typer.echo("could not copy the disk out of the image", err=True)
            return EXIT_BAD_REFERENCE
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)

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
    agent: str | None,
    model: str | None,
    provider: str | None,
) -> RunConfig:
    flags = [*(overrides or [])]
    if agent is not None:
        flags.append(f"agent.name={agent}")
    if provider is not None:
        flags.append(f"provider={provider}")
    if model:
        flags.append(f"agent.model={model}")
    selected = select_agent_name(run_path=config, overrides=flags)
    preset = PRESET_DIR / f"{selected}.toml"
    if not preset.is_file():
        preset = None
    try:
        return load_run_config(preset_path=preset, run_path=config, overrides=flags)
    except AleError as error:
        typer.echo(f"configuration error: {error}", err=True)
        raise typer.Exit(EXIT_BAD_REFERENCE) from error


def _harness(settings: RunConfig):  # type: ignore[no-untyped-def]
    match settings.agent.name:
        case "oracle":
            return OracleHarness()
        case "nop":
            return NopHarness()
        case "computer-use":
            from ale.run.harnesses.computer_use import ComputerUseHarness

            return ComputerUseHarness(model=settings.agent.model, **settings.agent.settings)
        case "claude-code":
            return ClaudeCodeHarness(
                cli_version=settings.agent.version,
                settings=settings.agent.settings,
            )
        case unknown:
            raise AleError(
                f"unknown agent {unknown!r}; try claude-code, computer-use, oracle or nop"
            )


def _provider(settings: RunConfig):  # type: ignore[no-untyped-def]
    # Imported on demand: each provider pulls in its own tooling assumptions, and a
    # docker run should not fail because qemu is missing.
    match settings.provider:
        case "docker":
            from ale.run.providers.docker import DockerProvider

            return DockerProvider()
        case "qemu":
            from ale.run.providers.qemu import QemuProvider

            return QemuProvider()
        case unknown:
            raise AleError(f"unknown provider {unknown!r}; try docker or qemu")


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
    agent_resources = resolve_agent_resources(
        task=task.spec.tools,
        agent_skills=settings.agent.skills,
        agent_mcp_servers=settings.agent.mcp_servers,
        task_root=resolved.task_dir,
        task_source=resolved.source,
        network=task.spec.network,
    )
    harness.validate_resources(agent_resources)
    inputs = ProvenanceInputs(
        source=resolved.source,
        agent=agent_provenance(
            harness,
            settings.agent.model,
            settings,
            agent_resources,
        ),
        gateway=gateway_provenance(settings),
        config_hash=settings.config_hash,
    )

    identity = episode_identity(
        task.spec,
        agent=f"{inputs.agent.harness}@{inputs.agent.version}",
        seed=settings.seed,
        config_hash=settings.config_hash,
        resources_digest=agent_resources.digest,
    )

    gateway = None
    proxy = None
    limits = None
    gateway_url, proxy_url = "", ""
    needs_model = settings.agent.name not in {"oracle", "nop"}
    allowed = frozenset(task.spec.network.allowed_hosts)

    if needs_model:
        try:
            api_key, upstream = provider_credentials(
                settings.gateway.api_key_env, settings.gateway.base_url
            )
        except AleError as error:
            typer.echo(f"{error}", err=True)
            return EXIT_BAD_REFERENCE
        gateway = Gateway(api_key=api_key, upstream=upstream, host=_gateway_host(settings))
        gateway_url = await gateway.start()
        limits = Limits(
            **{
                key: None if value == "unlimited" else value
                for key, value in settings.gateway.limits.model_dump().items()
            }
        )

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

    # Episodes of one task are independent by construction — separate sandbox, separate
    # gateway session, separate run directory — so the only thing bounding them is what
    # the host can hold. One at a time is the default because a VM guest is measured in
    # gigabytes, and four of those is a choice an operator should make deliberately.
    gate = asyncio.Semaphore(settings.concurrency)

    async def one(episode_id: str) -> Status | None:
        async with gate:
            ledger.mark_running(episode_id)
            result = await run_episode(
                task,
                StandardEnvironment(harness, max_steps=settings.agent.max_steps),
                _provider(settings),
                run_dir=run_dir,
                gateway_url=gateway_url,
                model=settings.agent.model,
                gateway=gateway,
                limits=limits,
                # What the task declared, and nothing it did not: the proxy refuses the
                # rest, so an agent cannot widen its own reach by asking.
                allowed_hosts=allowed,
                seed=settings.seed,
                collect_artifacts=settings.artifacts.collect == "host",
                provenance=inputs,
                proxy_url=proxy_url,
                agent_resources=agent_resources,
                episode_id=episode_id,
                phase_callback=lambda phase: ledger.update_phase(episode_id, phase.value),
                logging_policy=settings.logging,
            )
        ledger.finish_episode(result.episode_id, result.record)

        _report(result)
        reportable = _check_reportable(result)
        unusable = require_reportable and not reportable
        return (
            None
            if result.verdict.status is Status.COMPLETED and not unusable
            else result.verdict.status
        )

    try:
        pending = []
        for index in range(settings.episodes):
            if index < done:
                typer.echo(f"skip  {task.spec.label}: episode {index + 1} already complete")
                continue
            episode_id = f"{task.spec.id}-{uuid.uuid4().hex[:8]}"
            ledger.queue_episode(
                episode_id=episode_id,
                run_id=run_id,
                identity=identity,
                spec=task.spec,
                episode_path=episode_id,
            )
            pending.append(one(episode_id))

        # One failing episode is a result, not an abort: the others are still work someone
        # asked for, and a run that stops at the first failure reports a smaller sample
        # than it took.
        for outcome in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(outcome, BaseException):
                typer.echo(f"error {task.spec.label}: {outcome}")
                failures += 1
            elif outcome is not None:
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

    run_id = f"validate-{uuid.uuid4().hex[:8]}"
    run_dir = runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.open_run(run_id, settings.config_hash)
    harness = OracleHarness()
    resources = EffectiveAgentResources()
    failures = 0
    try:
        for task in tasks:
            spec = task.spec
            episode_id = f"{spec.id}-{uuid.uuid4().hex[:8]}"
            inputs = ProvenanceInputs(
                source=resolved.source,
                agent=agent_provenance(
                    harness,
                    "",
                    settings,
                    resources,
                ),
                gateway=gateway_provenance(settings),
                config_hash=settings.config_hash,
            )
            identity = episode_identity(
                spec,
                agent=f"{harness.name}@{harness.version()}",
                seed=settings.seed,
                config_hash=settings.config_hash,
            )
            ledger.queue_episode(
                episode_id=episode_id,
                run_id=run_id,
                identity=identity,
                spec=spec,
            )
            ledger.mark_running(episode_id)
            result = await run_episode(
                task,
                StandardEnvironment(harness),
                _provider(settings),
                run_dir=run_dir,
                episode_id=episode_id,
                provenance=inputs,
                phase_callback=lambda phase, current=episode_id: ledger.update_phase(
                    current, phase.value
                ),
                logging_policy=settings.logging,
            )
            ledger.finish_episode(episode_id, result.record)
            rewards = result.verdict.rewards
            passed = result.verdict.status is Status.COMPLETED and _is_full_reward_map(rewards)
            failures += 0 if passed else 1
            mark = "ok  " if passed else "FAIL"
            if rewards:
                non_full = {key: value for key, value in rewards.items() if value != 1.0}
                detail = f"rewards {rewards}" if not non_full else f"non-full rewards: {non_full}"
            else:
                detail = str(result.verdict.status)
            typer.echo(f"{mark}  {spec.label}: {detail}")
    finally:
        ledger.close()

    typer.echo(f"validation run: {run_dir}")
    return 0 if failures == 0 else EXIT_SOME_FAILED


def _is_full_reward_map(rewards: dict[str, float] | None) -> bool:
    return bool(rewards) and all(value == 1.0 for value in rewards.values())


def _gateway_host(settings: RunConfig) -> str:
    """Bind where a sandbox can reach us.

    Every sandbox is in a different network namespace from this process, so loopback is
    never an address one of them can dial: a container on an isolated bridge reaches the
    host at its gateway address, and a virtual machine reaches it through the runner
    holding it. Binding to all interfaces is what makes the deny-all topology usable.

    This used to bind loopback for anything that was not Docker, which was true when the
    VM backend ran qemu on the host with slirp — the guest's own NAT delivered to the
    host's loopback. It stopped being true when the VM moved inside a runner, and nothing
    noticed, because every VM test until now used the oracle and never asked for a model.

    What keeps this safe is not the bind address: a request without a live per-episode
    bearer token is refused, and those tokens die with their episode.
    """
    return "0.0.0.0"


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
