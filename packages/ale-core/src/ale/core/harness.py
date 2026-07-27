"""Agent harnesses.

Two families, distinguished by who owns the interaction loop — not by where the agent
process runs (see ``docs/adr/0002-harness-families.md``):

* :class:`AutonomousHarness` — the agent owns its loop. We hand over a prompt and take
  the result; what happens in between is the agent's business. It may run inside the
  sandbox or outside it against the sandbox's exposed interfaces.
* :class:`PolicyHarness` — the agent steps through a :class:`~ale.core.env.TaskEnv` we
  hand it. It drives, but every observation and action passes through our environment,
  so the trajectory is witnessed rather than reported.

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

from ale.core.env import Observation, StepResult, TaskEnv
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
    "StepwisePolicy",
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
    home: str = Field(
        default="/home/user",
        description=(
            "The agent's home directory, and so where a harness may write. Supplied "
            "rather than chosen: the agent runs unprivileged, and a path it does not own "
            "fails on the first write with an error about permissions that says nothing "
            "about the agent or the task."
        ),
    )


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


class Harness(ABC):
    """Common surface of both families."""

    name: str
    family: HarnessFamily

    logs: tuple[str, ...] = ()
    """Files this harness writes about its own run, relative to the agent's home.

    Not artifacts. An artifact is what the *agent produced* while doing the task, and the
    task declares it because only the task knows what its own output is. These are what
    the *harness* produced while running it — a transcript, a stream of tool calls, an
    error log — and only the harness knows about them.

    Keeping them apart matters because they answer different questions. Artifacts say what
    the agent achieved, which is what the verifier scores. These say how it went about it,
    which is the only evidence for why a score is what it is: a run that scored 1.0 by
    reading a file and one that scored 1.0 by looking at the screen are indistinguishable
    from the reward alone.
    """

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
    """The agent steps through an environment we supply.

    Driving is the agent's job because that is the shape most agents already have —
    gymnasium, cua-lite and verifiers all hand an agent an environment and let it run.
    Making the framework call ``decide(observation)`` instead would force every such
    agent through a queue-based inversion, and would buy nothing: the recording and the
    limits live in the environment either way.
    """

    family = HarnessFamily.POLICY

    @abstractmethod
    async def rollout(self, env: TaskEnv, session: HarnessSession) -> str | None:
        """Drive ``env`` until it is done. May return a closing message.

        Stop when a :class:`~ale.core.env.StepResult` reports ``done``; the environment
        enforces its own ceilings, so ignoring that is refused rather than obeyed.
        """


class StepwisePolicy(PolicyHarness):
    """A :class:`PolicyHarness` for agents that would rather be asked than drive.

    Both styles are legitimate, and neither should have to adapt to the other. This
    supplies the loop so a subclass only has to answer one question at a time.
    """

    async def rollout(self, env: TaskEnv, session: HarnessSession) -> str | None:
        await self.start(session)
        observation = await env.reset()
        while True:
            actions = await self.decide(observation)
            result = await env.step(actions)
            if result.done:
                return await self.finish(result)
            observation = result.observation

    async def start(self, session: HarnessSession) -> None:
        """Prepare for an episode. Default: nothing."""
        return None

    @abstractmethod
    async def decide(self, observation: Observation) -> list[DesktopAction]:
        """Actions for this observation. An empty list means finished."""

    async def finish(self, result: StepResult) -> str | None:
        """Release per-episode state; may return a closing message."""
        return None
