"""Assembling a run's provenance record.

The shapes live in ``orbit.core.lock``; this is where the values are actually found. They
come from four places and no two are alike: the caller knows the task source and the
configuration, the harness knows its own version, the container runtime knows the image
digest, and the episode only learns which assets it materialised while running.

The engine's own commit is resolved here too. When it cannot be, the value stays
``unknown`` and ``RunLock.require_reportable()`` refuses to publish against it, rather
than a placeholder being invented. A lock reading ``commit: unknown`` satisfies every
automated check and is worthless to whoever eventually has to reproduce the number,
which is the one moment provenance exists for.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from orbit.core.config import RunConfig, RunTimeouts
from orbit.core.harness import EffectiveAgentResources
from orbit.core.lock import (
    AgentProvenance,
    AgentResourceProvenance,
    AgentVariableProvenance,
    AssetProvenance,
    AuthenticationProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    HarnessPresetProvenance,
    ImageProvenance,
    JudgeProvenance,
    LimitTermination,
    OrbitVerifyProvenance,
    ResourceProvenance,
    ResumedFrom,
    RunLock,
    SandboxProvenance,
    TaskProvenance,
    TaskSource,
    TaskVariableProvenance,
    VerificationProvenance,
)
from orbit.core.result import SandboxOutcome
from orbit.core.sandbox import PreparedTaskImage, ResolvedImage, ResourceAllocation
from orbit.core.taskspec import ImageSpec, TaskSpec, VerifySpec
from orbit.run import __version__
from orbit_verify import JudgeInvocation

__all__ = [
    "ProvenanceInputs",
    "agent_provenance",
    "build_lock",
    "framework_provenance",
    "gateway_provenance",
    "judge_provenance",
]

UNKNOWN_COMMIT = "unknown"
#: The commit of the checkout an installed copy was installed from, which the installer
#: writes beside this module, as a cluster's engine is installed below its cache.
INSTALLED_COMMIT = Path(__file__).with_name("commit")


def _git_commit() -> str:
    """The engine's own commit: its checkout's, or the one an installed copy records, else
    ``unknown``.

    An installed copy legitimately has no git metadata, so this is not an error on its
    own — it only becomes one when such a run tries to report a result.
    """
    if INSTALLED_COMMIT.is_file():
        return INSTALLED_COMMIT.read_text().strip() or UNKNOWN_COMMIT
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_COMMIT
    return result.stdout.strip() if result.returncode == 0 else UNKNOWN_COMMIT


def framework_provenance() -> FrameworkProvenance:
    return FrameworkProvenance(version=__version__, commit=_git_commit())


def gateway_provenance(settings: RunConfig, *, observable: bool = True) -> GatewayProvenance:
    """Record the ceilings that were actually in force.

    Every field is present, and explicit ``unlimited`` remains visible.
    """
    return GatewayProvenance(
        dialect=settings.gateway.dialect,
        limits=settings.gateway.limits.model_dump() if observable else {},
        observability="available" if observable else "unavailable",
    )


def judge_provenance(
    invocations: tuple[JudgeInvocation, ...],
    endpoints: Mapping[str, str] | None = None,
    *,
    placement: Literal["verify", "validate"] = "verify",
) -> tuple[JudgeProvenance, ...]:
    """Project collected sandbox observations into the stable RunLock shape.

    ``endpoints`` names each Judge kind's real endpoint, which replaces the Gateway
    address the verifier observed; ``placement`` says which verifier called them.
    """
    endpoints = endpoints or {}
    observed: list[JudgeProvenance] = []
    for invocation in invocations:
        if not invocation.attempts:
            continue
        first = invocation.attempts[0]
        observed.append(
            JudgeProvenance(
                invocation_id=invocation.id,
                kind=invocation.kind,
                model=first.model,
                reasoning_effort=first.reasoning_effort,
                endpoint_identity=endpoints.get(invocation.kind, first.endpoint_identity),
                prompt_hash=first.prompt_hash,
                schema_hash=first.schema_hash,
                adapter=invocation.adapter,
                adapter_version=invocation.adapter_version,
                placement=placement,
                attempts=len(invocation.attempts),
            )
        )
    return tuple(observed)


def agent_provenance(
    harness: object,
    model: str,
    settings: RunConfig | None = None,
    resources: EffectiveAgentResources | None = None,
    authentication: AuthenticationProvenance | None = None,
    agent_env: tuple[AgentVariableProvenance, ...] | None = None,
) -> AgentProvenance:
    """Read a harness's identity off the harness itself.

    The family comes from the harness rather than from the task, which is the whole
    point of it living there: provenance records what actually ran. ``agent_env`` is where
    each of the Run's ``[agent.env]`` values came from, for an agent, to which the Run also
    gives its instruction's prefix and suffix; ``None`` for an oracle, which receives none
    of them.
    """
    preset = (
        HarnessPresetProvenance(name=settings.preset_name, digest=settings.preset_digest)
        if settings and settings.preset_name and settings.preset_digest
        else None
    )
    harness_settings = settings.agent.settings if settings else {}
    given = settings.agent if settings and agent_env is not None else None
    native_limits = {key: value for key, value in harness_settings.items() if key == "max_turns"}
    resource_records = (
        tuple(
            AgentResourceProvenance(
                kind="skill",
                name=skill.name,
                source_layers=skill.source_layers,
                resolved_source=", ".join(skill.declared_sources),
                digest=skill.digest,
                source_version=skill.source_version,
                reportable=skill.reportable,
                reportability_reason=skill.reportability_reason,
            )
            for skill in resources.skills
        )
        + tuple(
            AgentResourceProvenance(
                kind="mcp",
                name=server.name,
                source_layers=server.source_layers,
                resolved_source=", ".join(server.declared_sources),
                digest=server.digest,
                source_version=server.source_version,
                reportable=server.reportable,
                reportability_reason=server.reportability_reason,
            )
            for server in resources.mcp_servers
        )
        if resources
        else ()
    )
    return AgentProvenance(
        harness=getattr(harness, "name", type(harness).__name__),
        family=getattr(harness, "family", "autonomous"),
        version=harness.version(),  # type: ignore[attr-defined]
        integrity=harness.integrity(),  # type: ignore[attr-defined]
        model=model,
        preset=preset,
        settings=harness_settings,
        native_limits=native_limits,
        resources=resource_records,
        resources_digest=resources.digest if resources else None,
        authentication=authentication or AuthenticationProvenance(),
        instruction_prefix=given.instruction_prefix if given else "",
        instruction_suffix=given.instruction_suffix if given else "",
        env=agent_env or (),
    )


@dataclass
class ProvenanceInputs:
    """What the run layer knows before an episode starts.

    Everything an episode discovers for itself is filled in by ``build_lock`` from the
    episode's own record, so a caller cannot assert what it did not observe.
    """

    source: TaskSource
    agent: AgentProvenance
    gateway: GatewayProvenance
    config_hash: str
    timeouts: RunTimeouts = field(default_factory=RunTimeouts)
    validation_rounds: int | Literal["unlimited"] = 0
    """The effective count, which the run layer resolves before anything is prepared."""
    judges: tuple[JudgeProvenance, ...] = ()
    framework: FrameworkProvenance = field(default_factory=framework_provenance)


def build_lock(
    inputs: ProvenanceInputs,
    spec: TaskSpec,
    *,
    resolved_image: ResolvedImage,
    allocation: ResourceAllocation,
    task_digest: str,
    non_verifier_digest: str | None = None,
    prepared_image: PreparedTaskImage | None,
    sandbox: SandboxProvenance | None = None,
    asset: AssetProvenance | None = None,
    verifier_resolved_image: ResolvedImage | None = None,
    verifier_allocation: ResourceAllocation | None = None,
    prepared_verifier_image: PreparedTaskImage | None = None,
    validated: bool = False,
    validation_resolved_image: ResolvedImage | None = None,
    validation_allocation: ResourceAllocation | None = None,
    prepared_validation_image: PreparedTaskImage | None = None,
    orbit_verify: OrbitVerifyProvenance | None = None,
    termination: LimitTermination | None = None,
    sandbox_outcomes: tuple[SandboxOutcome, ...] = (),
    resumed_from: ResumedFrom | None = None,
    variables: tuple[TaskVariableProvenance, ...] = (),
    harbor: bool = False,
    warnings: tuple[str, ...] = (),
    seed: int = 0,
    validation_stage: Literal["validation", "tests"] = "validation",
) -> RunLock:
    """Bind one episode's result to everything that produced it: with the verifier of
    validation rounds where it has its own and the episode ``validated``."""
    return RunLock(
        task=TaskProvenance(
            name=spec.name,
            spec_hash=spec.spec_hash,
            content_digest=task_digest,
            non_verifier_digest=non_verifier_digest,
            source=inputs.source,
            variables=variables,
            harbor=harbor,
            warnings=warnings,
        ),
        image=_image_provenance(spec.image, resolved_image, allocation, prepared_image),
        resources=ResourceProvenance(requested=spec.resources, effective=allocation),
        agent=inputs.agent,
        framework=inputs.framework,
        gateway=inputs.gateway,
        sandbox=sandbox,
        config_hash=inputs.config_hash,
        timeouts=inputs.timeouts,
        validation_rounds=inputs.validation_rounds,
        validation_stage=validation_stage,
        seed=seed,
        orbit_verify=orbit_verify,
        judges=inputs.judges,
        asset=asset,
        verification=_verification(
            spec,
            spec.verify,
            verifier_resolved_image,
            verifier_allocation,
            prepared_verifier_image,
        ),
        validation=(
            _verification(
                spec,
                spec.validation,
                validation_resolved_image,
                validation_allocation,
                prepared_validation_image,
            )
            if validated and spec.validation is not None
            else None
        ),
        termination=termination,
        sandbox_outcomes=sandbox_outcomes,
        resumed_from=resumed_from,
    )


def _verification(
    spec: TaskSpec,
    placed: VerifySpec,
    resolved: ResolvedImage | None,
    allocation: ResourceAllocation | None,
    prepared: PreparedTaskImage | None,
) -> VerificationProvenance:
    """Where a verifier ``placed`` was, and the image and resources of its Sandbox of its
    own, if it ran one."""
    return VerificationProvenance(
        mode=placed.environment_mode,
        network=placed.network,
        image=(
            _image_provenance(placed.image or spec.image, resolved, allocation, prepared)
            if resolved is not None and allocation is not None
            else None
        ),
        resources=(
            ResourceProvenance(requested=placed.resources.as_resources(), effective=allocation)
            if placed.resources is not None and allocation is not None
            else None
        ),
    )


def _image_provenance(
    declaration: ImageSpec,
    resolved: ResolvedImage,
    allocation: ResourceAllocation,
    prepared: PreparedTaskImage | None,
) -> ImageProvenance:
    if prepared is None:
        raise ValueError("image provenance requires the prepared image")
    return ImageProvenance(
        declaration=declaration,
        source="ref" if prepared.source == "external-ref" else "local",
        input_identity=prepared.input_identity,
        image_source_identity=prepared.image_source_identity,
        prepared_identity=prepared.prepared_identity,
        runtime_ref=prepared.runtime_ref,
        base_materials=prepared.base_materials,
        resolved_reference=prepared.resolved_reference,
        builder_identity=prepared.builder_identity,
        provider=allocation.provider,
        observed_identity=resolved.observed_identity,
        observed_ref=resolved.observed_ref,
    )
