"""The ``ale`` command line.

Kept thin on purpose: it resolves a reference, assembles the pieces, and prints what
came back. Every decision that matters — how a task loads, how an episode runs, what a
verdict means — lives in the library, so the same behaviour is available to anything
that imports it.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Annotated

import typer

from ale.core.config import RunConfig, load_run_config, select_agent_name
from ale.core.errors import AleError, ConfigError, ProviderCapabilityError, TaskDefinitionError
from ale.core.harness import EffectiveAgentResources
from ale.core.ids import content_hash
from ale.core.sandbox import ImageKind, PreparedTaskImage
from ale.core.taskspec import VerificationMode
from ale.core.validation import (
    TaskValidationObservation,
    ValidationAttempt,
    ValidationEngine,
    ValidationNotice,
    ValidationObservation,
)
from ale.core.verdict import Status, Verdict
from ale.run import __version__
from ale.run.agent_resources import resolve_agent_resources
from ale.run.assets import asset_status, observe_task_assets, pull_assets, push_assets
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import EpisodeResult, run_episode
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import Limits
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.ledger import Ledger, episode_identity
from ale.run.lint import lint_repository
from ale.run.provenance import (
    ProvenanceInputs,
    agent_provenance,
    framework_provenance,
    gateway_provenance,
)
from ale.run.providers import ProviderRegistry
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
from ale.run.recording import atomic_write_json
from ale.run.scaffold import scaffold_task
from ale.run.secrets import provider_credentials
from ale.run.sources import parse_task_reference, resolve, select_tasks
from ale.run.subscription import SubscriptionCredential, resolve_authentication
from ale.run.task_images import (
    ImagePreparationResult,
    ImagePreparationStep,
    prepare_task_image_result,
    prepare_verifier_image_result,
)
from ale.run.tasksets.manifest import load_tasks

app = typer.Typer(
    name="ale",
    help="Agents' Last Exam evaluation orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)
assets_app = typer.Typer(help="Synchronize canonical Task assets.", no_args_is_help=True)
app.add_typer(assets_app, name="assets")
sandbox_app = typer.Typer(help="Inspect and remove retained sandboxes.", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")

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
    from ale.core.sandbox import ImageRef
    from ale.run.images import resolve_vm_image
    from ale.run.providers.qemu import GUEST_IMAGE

    source = reference or GUEST_IMAGE
    typer.echo(f"pulling {source}")
    try:
        prepared = asyncio.run(resolve_vm_image(ImageRef(kind="vm", reference=source)))
    except AleError as error:
        typer.echo(str(error), err=True)
        return EXIT_BAD_REFERENCE

    cached = Path(prepared.runtime_ref)
    target = dest or cached
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


def _print_asset_sync(results: object) -> None:
    for result in results:  # type: ignore[union-attr]
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


# --- internals ---


def _config(
    config: Path | None,
    overrides: list[str] | None,
    *,
    agent: str | None,
    model: str | None,
) -> RunConfig:
    flags = [*(overrides or [])]
    if agent is not None:
        flags.append(f"agent.name={agent}")
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
            _require_gateway_dialect(settings, "anthropic")
            return ClaudeCodeHarness(
                cli_version=settings.agent.version,
                settings=settings.agent.settings,
            )
        case "grok-build":
            return GrokBuildHarness(
                cli_version=settings.agent.version,
                gateway_dialect=settings.gateway.dialect,
                settings=settings.agent.settings,
            )
        case "codex-cli":
            _require_gateway_dialect(settings, "openai-responses")
            return CodexCliHarness(
                cli_version=settings.agent.version,
                settings=settings.agent.settings,
            )
        case "openclaw-cli":
            _require_gateway_dialect(settings, "openai-responses")
            return OpenClawCliHarness(
                cli_version=settings.agent.version,
                settings=settings.agent.settings,
            )
        case unknown:
            raise AleError(
                f"unknown agent {unknown!r}; try claude-code, codex-cli, "
                "computer-use, grok-build, openclaw-cli, oracle or nop"
            )


def _require_gateway_dialect(settings: RunConfig, expected: str) -> None:
    if settings.gateway.dialect != expected:
        raise ConfigError(
            f"{settings.agent.name} requires gateway.dialect={expected!r}, "
            f"got {settings.gateway.dialect!r}"
        )


async def _run_one(
    reference: str,
    settings: RunConfig,
    runs_dir: Path,
    run_id: str | None = None,
    require_reportable: bool = False,
) -> int:
    try:
        authentication = resolve_authentication(settings.agent)
        harness = _harness(settings)
        subscription = None
        if authentication.mode == "subscription":
            subscription = SubscriptionCredential(
                authentication,
                cli_version=harness.version(),
                dialect=settings.gateway.dialect,
            )
            await subscription.preflight()
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE
    providers = ProviderRegistry(settings)
    try:
        task_reference = parse_task_reference(reference)
        resolved = resolve(task_reference.source)
        tasks = select_tasks(
            list(load_tasks(resolved.task_dir)),
            task_reference.variants,
        )
        await _prepare_images(tasks, providers)
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE

    run_id = run_id or uuid.uuid4().hex[:8]
    run_dir = runs_dir / run_id
    gateway = None
    proxy = None
    limits = None
    gateway_url, proxy_url = "", ""
    needs_model = settings.agent.name not in {"oracle", "nop"}

    if needs_model:
        if authentication.mode == "subscription":
            assert subscription is not None
            gateway = Gateway(subscription=subscription, host=_gateway_host(settings))
        else:
            try:
                api_key, upstream = provider_credentials(
                    settings.gateway.api_key_env,
                    settings.gateway.base_url,
                    settings.gateway.dialect,
                )
            except AleError as error:
                typer.echo(f"{error}", err=True)
                return EXIT_BAD_REFERENCE
            gateway = Gateway(
                api_key=api_key,
                upstream=upstream,
                dialect=settings.gateway.dialect,
                host=_gateway_host(settings),
            )
        gateway_url = await gateway.start()
        limits = Limits(
            **{
                key: None if value == "unlimited" else value
                for key, value in settings.gateway.limits.model_dump().items()
            }
        )
        if any(task.spec.network.allowed_hosts for task in tasks):  # type: ignore[attr-defined]
            proxy = EgressProxy(gateway.sessions, host=_gateway_host(settings))
            proxy_url = await proxy.start()

    profile = authentication.profile_slot_id or "none"
    typer.echo(
        f"auth  harness={settings.agent.name} provider={authentication.provider or 'none'} "
        f"requested={authentication.requested} effective={authentication.mode} "
        f"source={settings.authentication_source} model={settings.agent.model} profile={profile}"
    )

    ledger = Ledger(run_dir)
    ledger.open_run(run_id, settings.config_hash)
    failures = 0
    total = len(tasks) * settings.episodes

    # Episodes of one task are independent by construction — separate sandbox, separate
    # gateway session, separate run directory — so the only thing bounding them is what
    # the host can hold. One at a time is the default because a VM guest is measured in
    # gigabytes, and four of those is a choice an operator should make deliberately.
    gate = asyncio.Semaphore(settings.concurrency)

    async def one(
        task,  # type: ignore[no-untyped-def]
        episode_id: str,
        inputs: ProvenanceInputs,
        agent_resources: EffectiveAgentResources,
    ) -> Status | None:
        allowed = frozenset(task.spec.network.allowed_hosts)

        async def execute():  # type: ignore[no-untyped-def]
            return await run_episode(
                task,
                StandardEnvironment(harness, max_steps=settings.agent.max_steps),
                providers,
                run_dir=run_dir,
                gateway_url=gateway_url,
                model=settings.agent.model,
                gateway=gateway,
                limits=limits,
                # What the Task and selected Harness declared, and nothing else: the
                # proxy refuses the rest, so the agent cannot widen its own reach.
                allowed_hosts=allowed,
                seed=settings.seed,
                collect_artifacts=settings.artifacts.collect == "host",
                provenance=inputs,
                proxy_url=proxy_url,
                agent_resources=agent_resources,
                episode_id=episode_id,
                phase_callback=lambda phase: ledger.update_phase(episode_id, phase.value),
                logging_policy=settings.logging,
                verification_config=settings.verification,
                sandbox_retention=settings.sandbox_retention,
                authentication=authentication.mode,
                profile_slot_id=authentication.profile_slot_id,
            )

        async with gate:
            ledger.mark_running(episode_id)
            result = await execute()
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
        for task in tasks:
            agent_resources = resolve_agent_resources(
                task=task.spec.tools,
                agent_skills=settings.agent.skills,
                agent_mcp_servers=settings.agent.mcp_servers,
                task_root=getattr(task, "folder", None).root,
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
                    authentication=authentication.provenance(
                        source=settings.authentication_source,
                        cli_version=harness.version(),
                    ),
                ),
                gateway=gateway_provenance(settings),
                config_hash=settings.config_hash,
            )
            identity = episode_identity(
                task.spec,
                task_digest=task.task_digest,
                image_digest=task.prepared_image.prepared_identity,
                agent=f"{inputs.agent.harness}@{inputs.agent.version}",
                seed=settings.seed,
                config_hash=settings.config_hash,
                resources_digest=agent_resources.digest,
            )
            done = (
                sum(
                    1
                    for row in ledger.episodes(run_id)
                    if row.identity == identity and row.succeeded
                )
                if settings.resume
                and (
                    task.asset_observation is None
                    or (
                        not task.asset_observation.dirty
                        and task.asset_observation.commit is not None
                    )
                )
                else 0
            )
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
                pending.append(one(task, episode_id, inputs, agent_resources))

        # One failing episode is a result, not an abort: the others are still work someone
        # asked for, and a run that stops at the first failure reports a smaller sample
        # than it took.
        for outcome in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(outcome, BaseException):
                typer.echo(f"error: {outcome}")
                failures += 1
            elif outcome is not None:
                failures += 1
    finally:
        ledger.close()
        if proxy is not None:
            await proxy.stop()
        if gateway is not None:
            await gateway.stop()

    typer.echo(f"run {run_id}: {total - failures}/{total} completed")
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
    providers = ProviderRegistry(settings)
    try:
        task_reference = parse_task_reference(reference)
        resolved = resolve(task_reference.source)
        tasks = select_tasks(
            list(load_tasks(resolved.task_dir)),
            task_reference.variants,
        )
        await _prepare_images(tasks, providers)
    except AleError as error:
        typer.echo(f"{error}", err=True)
        return EXIT_BAD_REFERENCE

    run_id = f"validate-{uuid.uuid4().hex[:8]}"
    run_dir = runs_dir / run_id
    ledger = Ledger(run_dir)
    ledger.open_run(run_id, settings.config_hash)
    resources = EffectiveAgentResources()
    failures = 0
    observations: list[TaskValidationObservation] = []
    try:
        for task in tasks:
            spec = task.spec
            results = {}
            for pass_name, harness, agent_enabled in (
                ("untouched", NopHarness(), False),
                ("oracle", OracleHarness(), True),
            ):
                episode_id = f"{spec.id}-{pass_name}-{uuid.uuid4().hex[:8]}"
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
                    task_digest=task.task_digest,
                    image_digest=task.prepared_image.prepared_identity,
                    agent=f"validation-{pass_name}@{harness.version()}",
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
                    StandardEnvironment(harness, agent_enabled=agent_enabled),
                    providers,
                    run_dir=run_dir,
                    episode_id=episode_id,
                    provenance=inputs,
                    phase_callback=lambda phase, current=episode_id: ledger.update_phase(
                        current, phase.value
                    ),
                    logging_policy=settings.logging,
                    verification_config=settings.verification,
                    sandbox_retention=settings.sandbox_retention,
                )
                ledger.finish_episode(episode_id, result.record)
                results[pass_name] = result

            zero_result = results["untouched"]
            one_result = results["oracle"]
            reward_names, warnings, task_failures = _validation_notices(
                zero_result.verdict,
                one_result.verdict,
            )
            passed = not task_failures
            observations.append(
                TaskValidationObservation(
                    name=str(spec.name),
                    variant=spec.variant,
                    spec_hash=spec.spec_hash,
                    untouched=_validation_attempt(zero_result, run_dir),
                    oracle=_validation_attempt(one_result, run_dir),
                    reward_names=reward_names,
                    warnings=warnings,
                    failures=task_failures,
                    passed=passed,
                )
            )
            failures += 0 if passed else 1
            mark = "WARN" if warnings else ("ok  " if passed else "FAIL")
            typer.echo(
                f"{mark}  {spec.label}: untouched={_validation_outcome(zero_result.verdict)}, "
                f"oracle={_validation_outcome(one_result.verdict)}"
            )
    finally:
        ledger.close()

    framework = framework_provenance()
    observation = ValidationObservation(
        run_id=run_id,
        engine=ValidationEngine(version=framework.version, commit=framework.commit),
        tasks=tuple(observations),
    )
    atomic_write_json(run_dir / "validation.json", observation.model_dump(mode="json"))
    typer.echo(f"validation run: {run_dir}")
    return 0 if failures == 0 else EXIT_SOME_FAILED


async def _prepare_only(reference: str, settings: RunConfig) -> int:
    try:
        task_reference = parse_task_reference(reference)
        resolved = resolve(task_reference.source)
        tasks = select_tasks(
            list(load_tasks(resolved.task_dir)),
            task_reference.variants,
        )
        for root in {task.folder.root for task in tasks}:  # type: ignore[attr-defined]
            if findings := lint_repository(root):
                raise TaskDefinitionError(
                    "lint: " + "; ".join(str(item) for item in findings)[:2000]
                )
        results = await _prepare_images(tasks, ProviderRegistry(settings))
    except AleError as error:
        typer.echo(str(error), err=True)
        return EXIT_BAD_REFERENCE

    for result in results:
        image = result.image
        source = "ref" if image.source == "external-ref" else "local"
        typer.echo(
            f"{result.task} {result.role} kind={image.kind} source={source} "
            f"prepared={image.prepared_identity}"
        )
        for step in result.steps:
            suffix = " ".join(value for value in (step.identity, step.detail) if value is not None)
            typer.echo(f"  {step.name:<20} {step.outcome:<8} {suffix}".rstrip())
    return 0


async def _prepare_images(
    tasks: list[object], providers: ProviderRegistry
) -> tuple[ImagePreparationResult, ...]:
    by_source: dict[str, PreparedTaskImage] = {}
    by_identity: dict[tuple[ImageKind, str], PreparedTaskImage] = {}
    results: list[ImagePreparationResult] = []
    for task in tasks:
        task.asset_observation = observe_task_assets(task)  # type: ignore[attr-defined]
        spec = task.spec.image  # type: ignore[attr-defined]
        key = content_hash(
            {
                "role": "solver",
                "kind": spec.kind,
                "local": task.image_source_digest,  # type: ignore[attr-defined]
                "ref": spec.ref,
            }
        )
        image = by_source.get(key)
        if image is None:
            prepared = await prepare_task_image_result(task, providers)
            image = prepared.image
            image = by_identity.setdefault((image.kind, image.prepared_identity), image)
            by_source[key] = image
            if image is not prepared.image:
                prepared = _reused_result(task, "solver", image)
        else:
            prepared = _reused_result(task, "solver", image)
        results.append(prepared)
        task.prepared_image = image  # type: ignore[attr-defined]
        verify = task.spec.verify  # type: ignore[attr-defined]
        if verify.environment_mode is VerificationMode.SHARED:
            task.prepared_verifier_image = None  # type: ignore[attr-defined]
            continue
        if task.folder.verifier_dockerfile is None and verify.image is None:  # type: ignore[attr-defined]
            task.prepared_verifier_image = image  # type: ignore[attr-defined]
            reused = await prepare_verifier_image_result(task, providers)
            if reused is not None:
                results.append(reused)
            continue
        verifier_key = content_hash(
            {
                "role": "verifier",
                "kind": verify.image.kind,
                "local": task.verifier_image_source_digest,  # type: ignore[attr-defined]
                "ref": verify.image.ref,
            }
        )
        verifier = by_source.get(verifier_key)
        if verifier is None:
            prepared_verifier = await prepare_verifier_image_result(task, providers)
            if prepared_verifier is None:
                raise TaskDefinitionError("dedicated verifier produced no prepared image")
            verifier = prepared_verifier.image
            verifier = by_identity.setdefault((verifier.kind, verifier.prepared_identity), verifier)
            by_source[verifier_key] = verifier
            if verifier is not prepared_verifier.image:
                prepared_verifier = _reused_result(task, "verifier", verifier)
        else:
            prepared_verifier = _reused_result(task, "verifier", verifier)
        results.append(prepared_verifier)
        task.prepared_verifier_image = verifier  # type: ignore[attr-defined]
    return tuple(results)


def _reused_result(task: object, role: str, image: PreparedTaskImage) -> ImagePreparationResult:
    spec = task.spec  # type: ignore[attr-defined]
    return ImagePreparationResult(
        task=f"{spec.name}@{spec.variant}",
        role=role,
        image=image,
        steps=(
            ImagePreparationStep("lint", "executed"),
            ImagePreparationStep(
                "artifact-check",
                "reused",
                image.prepared_identity,
                image.runtime_ref,
            ),
        ),
    )


def _is_full_reward_map(rewards: dict[str, float] | None) -> bool:
    return bool(rewards) and all(value == 1.0 for value in rewards.values())


def _is_zero_reward_map(rewards: dict[str, float] | None) -> bool:
    return bool(rewards) and all(value == 0.0 for value in rewards.values())


def _validation_notices(
    untouched: Verdict,
    oracle: Verdict,
) -> tuple[
    tuple[str, ...],
    tuple[ValidationNotice, ...],
    tuple[ValidationNotice, ...],
]:
    failures: list[ValidationNotice] = []
    if untouched.status is not Status.COMPLETED:
        failures.append(
            ValidationNotice(
                code="untouched_not_completed",
                message=f"untouched episode ended with {untouched.status.value}",
            )
        )
    elif not _is_zero_reward_map(untouched.rewards):
        failures.append(
            ValidationNotice(
                code="untouched_nonzero",
                message=f"untouched rewards must all be zero, got {untouched.rewards}",
            )
        )

    if oracle.status is not Status.COMPLETED:
        failures.append(
            ValidationNotice(
                code="oracle_not_completed",
                message=f"oracle episode ended with {oracle.status.value}",
            )
        )

    untouched_names = tuple((untouched.rewards or {}).keys())
    oracle_names = tuple((oracle.rewards or {}).keys())
    if (
        untouched.status is Status.COMPLETED
        and oracle.status is Status.COMPLETED
        and set(untouched_names) != set(oracle_names)
    ):
        failures.append(
            ValidationNotice(
                code="reward_name_mismatch",
                message=(
                    f"untouched rewards {untouched_names} and oracle rewards "
                    f"{oracle_names} do not match"
                ),
            )
        )

    warnings: tuple[ValidationNotice, ...] = ()
    if not failures and oracle.rewards and not _is_full_reward_map(oracle.rewards):
        warnings = (
            ValidationNotice(
                code="partial_oracle",
                message=f"oracle rewards are not all 1.0: {oracle.rewards}",
            ),
        )
    return untouched_names or oracle_names, warnings, tuple(failures)


def _validation_attempt(result: EpisodeResult, run_dir: Path) -> ValidationAttempt:
    result_path = result.run_dir / "result.json"
    lock_path = result.run_dir / "lock.json" if result.lock is not None else None
    return ValidationAttempt(
        episode_id=result.episode_id,
        status=result.verdict.status,
        rewards=result.verdict.rewards,
        lock_path=lock_path.relative_to(run_dir).as_posix() if lock_path else None,
        lock_digest=(
            f"sha256:{hashlib.sha256(lock_path.read_bytes()).hexdigest()}" if lock_path else None
        ),
        result_path=result_path.relative_to(run_dir).as_posix(),
    )


def _validation_outcome(verdict: Verdict) -> object:
    if verdict.rewards is not None:
        return verdict.rewards
    if verdict.failure is not None:
        return f"{verdict.status.value} ({verdict.failure.error_class}: {verdict.failure.message})"
    return verdict.status.value


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
