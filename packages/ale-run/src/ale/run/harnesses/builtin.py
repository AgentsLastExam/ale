"""Harnesses that need no model.

``nop`` proves the machinery without spending anything; ``oracle`` runs the task's own
known-good solution, which is how a task earns admission — a task nobody can solve is a
broken task, and finding that out costs one container rather than one agent run.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

from ale.core.env import Observation, StepResult
from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession, StepwisePolicy
from ale.core.sandbox import Sandbox
from ale.core.trace import DesktopAction

__all__ = ["ORACLE_DIR", "NopHarness", "OracleHarness"]

ORACLE_DIR = PurePosixPath("/ale/oracle")


class NopHarness(AutonomousHarness):
    """Does nothing at all.

    Useful for probing the environment itself: whatever a task scores here is what it
    scores for an agent that never acts.
    """

    name = "nop"

    def version(self) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        return AgentRun(exit_code=0, final_message="nop harness took no action")


class ScriptedPolicyHarness(StepwisePolicy):
    """Replays a fixed list of action batches, one per step.

    The policy-family counterpart to :class:`NopHarness`: no model, no key, no network,
    so the stepwise contract and both its guards can be exercised on their own. What a
    task scores under a scripted policy is what the environment itself contributes.
    """

    name = "scripted"

    def __init__(self, script: Sequence[Sequence[DesktopAction]] = ()) -> None:
        self._script = [list(batch) for batch in script]
        self.observations: list[Observation] = []
        self._step = 0

    def version(self) -> str:
        return "1"

    async def decide(self, observation: Observation) -> list[DesktopAction]:
        self.observations.append(observation)
        if self._step >= len(self._script):
            return []  # nothing left to do, which is how an agent says "finished"
        batch = self._script[self._step]
        self._step += 1
        return batch

    async def finish(self, result: StepResult) -> str | None:
        return f"scripted harness ran {self._step} step(s); {result.info.get('reason', '')}"


class OracleHarness(AutonomousHarness):
    """Runs the task's own solution in place of an agent.

    The oracle is uploaded by the environment into :data:`ORACLE_DIR`, kept out of the
    workspace so a task cannot accidentally read its own answer during a real run.
    """

    name = "oracle"

    def version(self) -> str:
        return "1"

    async def launch(
        self,
        instruction: str,
        sandbox: Sandbox,
        session: HarnessSession,
        *,
        timeout_sec: float,
    ) -> AgentRun:
        entry = ORACLE_DIR / "run.sh"
        result = await sandbox.exec(
            ["bash", str(entry)],
            cwd=str(ORACLE_DIR),
            # The oracle stands in for an agent, but it is task code and gets the same
            # environment contract a stage does — otherwise a solution that works during
            # authoring fails during validation for reasons that have nothing to do with it.
            env={
                "ALE_TASK_DIR": "/ale/work",
                "ALE_PARAMS_JSON": str(ORACLE_DIR / "params.json"),
            },
            timeout_sec=timeout_sec,
        )
        return AgentRun(
            exit_code=result.exit_code,
            final_message=result.stderr.strip() or result.stdout.strip() or None,
        )
