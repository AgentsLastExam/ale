"""Environments: how one task becomes one episode.

This is the extension point a domain reaches for when the standard flow is not enough —
a grading topology with two isolated containers, a staged protocol, a human gate.

Everything an environment needs arrives through :class:`EpisodeContext`. It never builds
its own infrastructure clients, so the guarantees the framework makes (sandboxes are
leased and reaped, model traffic is metered and recorded, artifacts are collected,
budgets are enforced) hold for every domain by construction rather than by review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ale.core.harness import HarnessSession
from ale.core.lock import AssetProvenance, KitProvenance
from ale.core.sandbox import Sandbox, SandboxRequest
from ale.core.taskspec import TaskSpec
from ale.core.trace import PhaseSpan, TraceWriter
from ale.core.verdict import Verdict

if TYPE_CHECKING:
    from ale.core.task import Task

__all__ = [
    "ArtifactSink",
    "Budget",
    "Environment",
    "EpisodeContext",
    "Phase",
    "SandboxLease",
]


class Phase(StrEnum):
    """Lifecycle stages, used for timeouts, traces and failure attribution."""

    PROVISION = "provision"
    SETUP = "setup"
    AGENT = "agent"
    VERIFY = "verify"
    TEARDOWN = "teardown"


class SandboxLease(Protocol):
    """Leases sandboxes on behalf of an environment.

    An environment may hold several at once (a grader and a solver, say); all of them
    are reaped when the episode ends, whatever the outcome.
    """

    async def acquire(self, request: SandboxRequest) -> Sandbox: ...

    async def release(self, sandbox: Sandbox) -> None: ...


class ArtifactSink(Protocol):
    """Collects files out of a sandbox into the episode's run directory."""

    async def collect(self, sandbox: Sandbox, source: str, name: str) -> Path: ...

    def path(self, name: str) -> Path: ...


@dataclass
class Budget:
    """Wall-clock and money ceilings for one episode.

    The gateway enforces token and cost limits by refusing calls; this object is the
    environment's read-only view of what is left.
    """

    deadline_sec: float
    max_cost_usd: float | None = None
    spent_usd: float = 0.0
    started_at: float = 0.0

    def remaining_sec(self, now: float) -> float:
        return max(0.0, self.deadline_sec - (now - self.started_at))

    @property
    def remaining_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return max(0.0, self.max_cost_usd - self.spent_usd)


@dataclass
class EpisodeContext:
    """Everything an environment is allowed to touch."""

    episode_id: str
    spec: TaskSpec
    task_dir: Path
    """Host-side task folder: manifest, verify, oracle, setup scripts, files."""

    run_dir: Path
    """Host-side output directory for this episode."""

    sandboxes: SandboxLease
    artifacts: ArtifactSink
    trace: TraceWriter
    budget: Budget
    session: HarnessSession
    """Gateway address plus this episode's bearer token — never a provider credential."""

    work_dir: str = "/ale/work"
    """Framework scratch inside the sandbox, created before setup runs."""

    phases: list[PhaseSpan] = field(default_factory=list)
    """Filled in as each phase completes; folded into the episode's timing record."""

    proxy_url: str = ""
    """Egress proxy for allowlist tasks; empty when none was started."""

    image_digest: str | None = None
    """Resolved when the sandbox is provisioned — the tag alone proves nothing."""

    assets: list[AssetProvenance] = field(default_factory=list)
    kits: list[KitProvenance] = field(default_factory=list)
    """What this episode actually materialised, recorded as it happens.

    An episode observes these; a caller cannot assert them in advance, which is why they
    accumulate here rather than being passed in.
    """

    seed: int = 0
    extras: dict[str, object] = field(default_factory=dict)


class Environment(ABC):
    """Turns one task into one episode."""

    name: str

    @abstractmethod
    async def run(self, task: Task, ctx: EpisodeContext) -> Verdict:
        """Administer ``task`` and return its verdict.

        Implementations should let framework errors propagate: the engine maps them to
        the status taxonomy. Catch only what you can genuinely handle.
        """

    def required_phases(self) -> Sequence[Phase]:
        """Phases this environment actually uses; drives timeout accounting."""
        return (Phase.PROVISION, Phase.SETUP, Phase.AGENT, Phase.VERIFY, Phase.TEARDOWN)
