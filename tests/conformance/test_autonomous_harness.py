"""The shared autonomous harness contract is usable without the Claude adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from orbit.core.episode import EpisodeContext
from orbit.core.errors import AgentUnsupportedError
from orbit.core.harness import (
    AgentRun,
    AutonomousHarness,
    EffectiveAgentResources,
    HarnessSession,
    ResolvedSkill,
    TrajectoryParseContext,
)
from orbit.core.sandbox import Sandbox
from orbit.core.task import Task
from orbit.core.taskspec import OperatingSystem, TaskSpec
from orbit.core.testkit import AutonomousHarnessConformance
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.recording import BlobStore

pytestmark = pytest.mark.unit


class MinimalHarness(AutonomousHarness):
    name = "minimal"

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
        assert session.gateway_url
        return AgentRun(exit_code=0, final_message=instruction)


def session() -> HarnessSession:
    return HarnessSession(
        episode_id="episode",
        gateway_url="http://gateway",
        token="token",
        model="model",
    )


def test_minimal_harness_definition_session_and_parser(tmp_path: Path) -> None:
    harness = MinimalHarness()
    AutonomousHarnessConformance.check_definition(harness)
    AutonomousHarnessConformance.check_empty_resources(harness)
    AutonomousHarnessConformance.check_session(session())
    AutonomousHarnessConformance.check_parser_is_pure(
        harness,
        TrajectoryParseContext(
            episode_id="episode",
            trajectory_id="trajectory",
            instruction="hello",
            logs_dir=tmp_path,
            model="model",
            agent_version="1",
            blobs=BlobStore(tmp_path),
        ),
    )


def test_unsupported_resources_fail_explicitly() -> None:
    resources = EffectiveAgentResources(
        skills=(
            ResolvedSkill(
                name="skill",
                path=Path("/tmp/skill"),
                source_layers=("run",),
                declared_sources=("/tmp/skill",),
                digest="sha256:" + "a" * 64,
                reportable=False,
            ),
        )
    )
    with pytest.raises(AgentUnsupportedError):
        MinimalHarness().validate_resources(resources)


async def test_unsupported_os_fails_before_sandbox_creation() -> None:
    harness = MinimalHarness()
    harness.validate_os(OperatingSystem.LINUX)
    protocol = EpisodeProtocol(harness)
    protocol._provision = AsyncMock()
    task = Mock(spec=Task)
    task.spec = TaskSpec(
        name="agentslastexam/windows",
        os="windows",
        image={"kind": "vm", "ref": "base.qcow2"},
        instruction="work",
    )
    ctx = Mock(spec=EpisodeContext)
    with pytest.raises(AgentUnsupportedError, match="minimal does not support windows"):
        await protocol.run(task, ctx)
    protocol._provision.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_session_launch_is_conforming() -> None:
    run = await MinimalHarness().launch("hello", None, session(), timeout_sec=1)  # type: ignore[arg-type]
    assert run.final_message == "hello"
