"""Agent harnesses.

Two families, distinguished by who owns the interaction loop — not by where the agent
process runs (see ``docs/adr/0002-harness-families.md``):

* :class:`AutonomousHarness` — the agent owns its loop. We hand over a prompt and take
  the result; what happens in between is the agent's business. It may run inside the
  sandbox or outside it against the sandbox's exposed interfaces.
* :class:`PolicyHarness` — we own the loop and ask the harness for one decision at a
  time, given an observation.

Both reach the sandbox through the contract and both send model traffic through the
gateway, so both produce the same trace. That is what makes a CLI agent and a GUI agent
comparable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ale.core.sandbox import Sandbox
from ale.core.trace import DesktopAction

__all__ = [
    "AgentRun",
    "AutonomousHarness",
    "Harness",
    "HarnessFamily",
    "HarnessSession",
    "Observation",
    "PolicyHarness",
    "ResumeSupport",
]


class HarnessFamily(StrEnum):
    """Which side owns the interaction loop.

    A property of the harness, not of the task: the same task can be attempted by an
    agent that runs itself and by one the framework drives step by step.
    """

    AUTONOMOUS = "autonomous"
    POLICY = "policy"


class ResumeSupport(StrEnum):
    """How, if at all, an agent can continue an interrupted exchange."""

    NONE = "none"
    REPLAY = "replay"
    """Restart on the accumulated conversation — correct for stateless programs."""

    NATIVE = "native"
    """The agent reopens its own recorded session; nothing is replayed."""


class HarnessSession(BaseModel):
    """What a harness needs in order to talk to the model through the gateway."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    gateway_url: str
    token: str = Field(description="Per-episode bearer; never a provider credential")
    model: str


class AgentRun(BaseModel):
    """The outcome of one autonomous agent run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    final_message: str | None = None
    artifacts_dir: Path | None = Field(
        default=None, description="Host directory holding whatever the agent left behind"
    )

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Observation(BaseModel):
    """What a policy harness is shown before it decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: int = Field(ge=0)
    screenshot_png: bytes | None = None
    text: str | None = None
    instruction: str | None = Field(default=None, description="Provided on the first step")


class Harness(ABC):
    """Common surface of both families."""

    name: str
    family: HarnessFamily

    @abstractmethod
    def version(self) -> str:
        """The agent build actually used; recorded in provenance."""

    def integrity(self) -> str:
        """What pins that build: an image digest, package hash, or commit."""
        return "unpinned"

    async def install(self, sandbox: Sandbox) -> None:
        """Prepare the sandbox. Default: nothing, because the image already has it."""
        return None

    def parse_artifacts(self, artifacts_dir: Path) -> list[dict[str, Any]]:
        """Turn agent-native output into semantic trace payloads.

        Pure and host-side: it reads collected files and returns records, so it can be
        re-run over a finished episode without touching a sandbox.
        """
        return []


class AutonomousHarness(Harness):
    """The agent owns its loop."""

    family = HarnessFamily.AUTONOMOUS
    resume_support: ResumeSupport = ResumeSupport.NONE

    @abstractmethod
    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        """Run the agent to completion."""

    async def resume(
        self,
        messages: Sequence[dict[str, Any]],
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        """Continue an exchange. Only valid when ``resume_support`` is not ``NONE``."""
        raise NotImplementedError(f"{self.name} does not support resume")


class PolicyHarness(Harness):
    """The framework owns the loop; the harness decides one step at a time."""

    family = HarnessFamily.POLICY

    @abstractmethod
    async def start(self, instruction: str, session: HarnessSession) -> None:
        """Begin an episode."""

    @abstractmethod
    async def decide(self, observation: Observation) -> list[DesktopAction]:
        """Return the actions to perform for this observation.

        An empty list means the agent considers the task finished.
        """

    async def finish(self) -> str | None:
        """Release per-episode state; may return a closing message."""
        return None
