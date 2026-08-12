"""Assembling a run's provenance record.

The shapes live in ``ale.core.lock``; this is where the values are actually found. They
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
from dataclasses import dataclass, field
from pathlib import Path

from ale.core.config import RunConfig
from ale.core.harness import EffectiveAgentResources
from ale.core.lock import (
    AgentProvenance,
    AgentResourceProvenance,
    AleVerifyProvenance,
    AssetProvenance,
    AuthenticationProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    HarnessPresetProvenance,
    ImageProvenance,
    JudgeProvenance,
    LimitTermination,
    ResourceProvenance,
    RunLock,
    SandboxProvenance,
    TaskProvenance,
    TaskSource,
    VerificationProvenance,
)
from ale.core.result import SandboxOutcome
from ale.core.sandbox import PreparedTaskImage, ResolvedImage, ResourceAllocation
from ale.core.taskspec import ImageSpec, TaskSpec
from ale.run import __version__
from ale_verify import VerificationRecord

__all__ = [
    "ProvenanceInputs",
    "agent_provenance",
    "build_lock",
    "framework_provenance",
    "gateway_provenance",
    "judge_provenance",
]

UNKNOWN_COMMIT = "unknown"


def _git_commit() -> str:
    """The engine's own commit, or ``unknown`` outside a checkout.

    An installed copy legitimately has no git metadata, so this is not an error on its
    own — it only becomes one when such a run tries to report a result.
    """
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


def judge_provenance(record: VerificationRecord) -> tuple[JudgeProvenance, ...]:
    """Project collected sandbox observations into the stable RunLock shape."""
    observed: list[JudgeProvenance] = []
    for invocation in record.judge_invocations:
        if not invocation.attempts:
            continue
        first = invocation.attempts[0]
        observed.append(
            JudgeProvenance(
                invocation_id=invocation.id,
                kind=invocation.kind,
                model=first.model,
                reasoning_effort=first.reasoning_effort,
                endpoint_identity=first.endpoint_identity,
                prompt_hash=first.prompt_hash,
                rubric_hash=first.rubric_hash,
                adapter=invocation.adapter,
                adapter_version=invocation.adapter_version,
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
) -> AgentProvenance:
    """Read a harness's identity off the harness itself.

    The family comes from the harness rather than from the task, which is the whole
    point of it living there: provenance records what actually ran.
    """
    preset = (
        HarnessPresetProvenance(name=settings.preset_name, digest=settings.preset_digest)
        if settings and settings.preset_name and settings.preset_digest
        else None
    )
    harness_settings = settings.agent.settings if settings else {}
    native_limits = {
        key: value
        for key, value in harness_settings.items()
        if key in {"max_turns", "max_budget_usd"}
    }
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
    judges: tuple[JudgeProvenance, ...] = ()
    framework: FrameworkProvenance = field(default_factory=framework_provenance)


def build_lock(
    inputs: ProvenanceInputs,
    spec: TaskSpec,
    *,
    resolved_image: ResolvedImage,
    allocation: ResourceAllocation,
    task_digest: str,
    prepared_image: PreparedTaskImage | None,
    sandbox: SandboxProvenance | None = None,
    asset: AssetProvenance | None = None,
    verifier_resolved_image: ResolvedImage | None = None,
    verifier_allocation: ResourceAllocation | None = None,
    prepared_verifier_image: PreparedTaskImage | None = None,
    ale_verify: AleVerifyProvenance | None = None,
    termination: LimitTermination | None = None,
    sandbox_outcomes: tuple[SandboxOutcome, ...] = (),
    seed: int = 0,
) -> RunLock:
    """Bind one episode's result to everything that produced it."""
    return RunLock(
        task=TaskProvenance(
            name=spec.name,
            variant=spec.variant,
            spec_hash=spec.spec_hash,
            content_digest=task_digest,
            source=inputs.source,
        ),
        image=_image_provenance(spec.image, resolved_image, allocation, prepared_image),
        resources=ResourceProvenance(requested=spec.resources, effective=allocation),
        agent=inputs.agent,
        framework=inputs.framework,
        gateway=inputs.gateway,
        sandbox=sandbox,
        config_hash=inputs.config_hash,
        seed=seed,
        ale_verify=ale_verify,
        judges=inputs.judges,
        asset=asset,
        verification=VerificationProvenance(
            mode=spec.verify.environment_mode,
            image=(
                _image_provenance(
                    spec.verify.image or spec.image,
                    verifier_resolved_image,
                    verifier_allocation,
                    prepared_verifier_image,
                )
                if verifier_resolved_image is not None and verifier_allocation is not None
                else None
            ),
            resources=(
                ResourceProvenance(
                    requested=spec.verify.resources.as_resources(),
                    effective=verifier_allocation,
                )
                if spec.verify.resources is not None and verifier_allocation is not None
                else None
            ),
        ),
        termination=termination,
        sandbox_outcomes=sandbox_outcomes,
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
        oci_identity=prepared.oci_identity,
        base_materials=prepared.base_materials,
        resolved_reference=prepared.resolved_reference,
        materializer_identity=prepared.materializer_identity,
        provider=allocation.provider,
        observed_identity=resolved.observed_identity,
        observed_ref=resolved.observed_ref,
    )
