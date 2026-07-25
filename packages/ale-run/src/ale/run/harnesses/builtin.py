"""Harnesses that need no model.

``nop`` proves the machinery without spending anything; ``oracle`` runs the task's own
known-good solution, which is how a task earns admission — a task nobody can solve is a
broken task, and finding that out costs one container rather than one agent run.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ale.core.harness import AgentRun, AutonomousHarness, HarnessSession
from ale.core.sandbox import Sandbox

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
            timeout_sec=timeout_sec,
        )
        return AgentRun(
            exit_code=result.exit_code,
            final_message=result.stderr.strip() or result.stdout.strip() or None,
        )
