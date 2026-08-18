"""Declared Skills enter a real sandbox; ambient host state does not."""

from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.harness import HarnessSession
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import (
    NetworkPolicy,
    Resources,
    SkillSource,
    ToolProvision,
)
from ale.run.agent_resources import resolve_agent_resources
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.providers.docker import DockerProvider
from tests.support import prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/container-ubuntu22-base:latest"


def write_skill(path: Path, text: str, *, executable: bool = False) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(text)
    script = path / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    if executable:
        script.chmod(0o755)


@pytest.mark.asyncio
async def test_task_and_run_skills_stage_once_without_ambient_host_state(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "task"
    write_skill(task_root / "tools/skills/task-skill", "task", executable=True)
    run_skill = tmp_path / "run-skill"
    write_skill(run_skill, "run")
    write_skill(tmp_path / ".claude" / "skills" / "ambient", "ambient")

    resources = resolve_agent_resources(
        task=ToolProvision(skills=(SkillSource(path="tools/skills/task-skill"),)),
        agent_skills=(SkillSource(path=str(run_skill), origin="run", declared=str(run_skill)),),
        task_root=task_root,
        task_source=TaskSource(
            kind="registry",
            repo="https://example.com/tasks.git",
            commit="a" * 40,
            path="tasks/demo/hello",
        ),
    )
    harness = ClaudeCodeHarness()
    harness.validate_resources(resources)

    provider = DockerProvider()
    await provider.preflight()
    prepared = await prepare_reference(provider, IMAGE)
    request = SandboxRequest(
        episode_id="agent-resources",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(),
    )
    async with await provider.create(request) as sandbox:
        session = HarnessSession(
            episode_id="agent-resources",
            gateway_url="http://gateway",
            token="episode-token",
            model="test-model",
            home="/home/user",
        )
        await harness.install_resources(sandbox, session, resources)
        result = await sandbox.exec(
            [
                "find",
                "/home/user/.claude-config/skills",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-type",
                "d",
                "-printf",
                "%f\n",
            ],
            identity=Identity.AGENT,
        )
        assert sorted(result.stdout.splitlines()) == ["run-skill", "task-skill"]
        assert "ambient" not in result.stdout
        executable = await sandbox.exec(
            ["test", "-x", "/home/user/.claude-config/skills/task-skill/run.sh"],
            identity=Identity.AGENT,
        )
        assert executable.ok
