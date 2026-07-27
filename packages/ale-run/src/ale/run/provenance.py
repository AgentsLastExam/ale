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
from ale.core.lock import (
    AgentProvenance,
    AssetProvenance,
    FrameworkProvenance,
    GatewayProvenance,
    ImageProvenance,
    JudgeProvenance,
    KitProvenance,
    RunLock,
    SandboxProvenance,
    TaskProvenance,
    TaskSource,
)
from ale.core.taskspec import TaskSpec
from ale.run import __version__

__all__ = [
    "ProvenanceInputs",
    "agent_provenance",
    "build_lock",
    "framework_provenance",
    "gateway_provenance",
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


def gateway_provenance(settings: RunConfig) -> GatewayProvenance:
    """Record the ceilings that were actually in force.

    ``None`` means unlimited and is omitted rather than written as a zero, which would
    read as "nothing was allowed" — the exact opposite.
    """
    limits = {
        name: float(value)
        for name, value in settings.gateway.limits.model_dump().items()
        if value is not None
    }
    return GatewayProvenance(dialect=settings.gateway.dialect, limits=limits)


def agent_provenance(harness: object, model: str) -> AgentProvenance:
    """Read a harness's identity off the harness itself.

    The family comes from the harness rather than from the task, which is the whole
    point of it living there: provenance records what actually ran.
    """
    return AgentProvenance(
        harness=getattr(harness, "name", type(harness).__name__),
        family=getattr(harness, "family", "autonomous"),
        version=harness.version(),  # type: ignore[attr-defined]
        integrity=harness.integrity(),  # type: ignore[attr-defined]
        model=model,
    )


@dataclass
class ProvenanceInputs:
    """What the run layer knows before an episode starts.

    Everything an episode discovers for itself — the image digest it resolved, the
    assets it materialised, the kits it staged — is filled in by ``build_lock`` from the
    episode's own record, so a caller cannot assert what it did not observe.
    """

    source: TaskSource
    agent: AgentProvenance
    gateway: GatewayProvenance
    config_hash: str
    judge: JudgeProvenance | None = None
    framework: FrameworkProvenance = field(default_factory=framework_provenance)


def build_lock(
    inputs: ProvenanceInputs,
    spec: TaskSpec,
    *,
    image_digest: str,
    sandbox: SandboxProvenance | None = None,
    assets: tuple[AssetProvenance, ...] = (),
    kits: tuple[KitProvenance, ...] = (),
    seed: int = 0,
    requires_core: str | None = None,
) -> RunLock:
    """Bind one episode's result to everything that produced it."""
    return RunLock(
        task=TaskProvenance(
            id=spec.id,
            domain=spec.domain,
            variant=spec.variant,
            spec_hash=spec.spec_hash,
            source=inputs.source,
            requires_core=requires_core,
        ),
        image=ImageProvenance(ref=f"{spec.image.name}:{spec.image.tag}", digest=image_digest),
        agent=inputs.agent,
        framework=inputs.framework,
        gateway=inputs.gateway,
        sandbox=sandbox,
        config_hash=inputs.config_hash,
        seed=seed,
        judge=inputs.judge,
        assets=assets,
        kits=kits,
    )
