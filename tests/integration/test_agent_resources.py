"""Declared Skills enter a real sandbox; ambient host state does not."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ale.core.errors import TaskError
from ale.core.harness import HarnessSession
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import (
    NetworkPolicy,
    Resources,
    SkillSource,
    ToolProvision,
)
from ale.run.agent_resources import resolve_agent_resources
from ale.run.harnesses._npm import agent_path
from ale.run.harnesses.base import install_skills
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from tests.support import CONTAINER_BASE, prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = CONTAINER_BASE


def write_skill(path: Path, text: str, *, executable: bool = False) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(text)
    script = path / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    if executable:
        script.chmod(0o755)


@pytest.mark.asyncio
async def test_image_and_run_skills_stage_once_without_ambient_host_state(
    tmp_path: Path,
) -> None:
    image_skills = tmp_path / "image-skills"
    write_skill(image_skills / "image-skill", "image", executable=True)
    write_skill(image_skills / "shared", "image")
    run_skill = tmp_path / "run-skill"
    write_skill(run_skill, "run")
    replacement = tmp_path / "replacement" / "shared"
    write_skill(replacement, "operator")
    write_skill(tmp_path / ".claude" / "skills" / "ambient", "ambient")

    resources = resolve_agent_resources(
        task=ToolProvision(skills_dir="/skills"),
        agent_skills=(
            SkillSource(path=str(run_skill), origin="run", declared=str(run_skill)),
            SkillSource(path=str(replacement), origin="run", declared=str(replacement)),
        ),
        task_source=TaskSource(
            kind="git",
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
        # What an image build would have left there.
        await sandbox.upload_dir(str(image_skills), "/skills")
        await sandbox.exec(["chmod", "+x", "/skills/image-skill/run.sh"])
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
        assert sorted(result.stdout.splitlines()) == ["image-skill", "run-skill", "shared"]
        assert "ambient" not in result.stdout
        executable = await sandbox.exec(
            ["test", "-x", "/home/user/.claude-config/skills/image-skill/run.sh"],
            identity=Identity.AGENT,
        )
        assert executable.ok
        replaced = await sandbox.exec(
            ["cat", "/home/user/.claude-config/skills/shared/SKILL.md"],
            identity=Identity.AGENT,
        )
        assert replaced.stdout == "operator"

        missing = resources.model_copy(update={"skills_dir": "/missing"})
        with pytest.raises(TaskError, match="skills_dir"):
            await harness.install_resources(sandbox, session, missing)


@pytest.mark.asyncio
@pytest.mark.needs_kvm
async def test_windows_copies_the_images_skills_through_git_bash(tmp_path: Path) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk:
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to a prepared VM disk")
    image_skills = tmp_path / "image-skills"
    write_skill(image_skills / "image-skill", "image")
    write_skill(image_skills / "shared", "image")
    replacement = tmp_path / "replacement" / "shared"
    write_skill(replacement, "operator")

    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    request = SandboxRequest(
        episode_id=f"skills-{uuid.uuid4().hex[:8]}",
        os="windows",
        user=os.environ.get("ALE_TEST_WINDOWS_USER", "user"),
        sudo=False,
        prepared_image=await provider.prepare_image(
            ImageRef(kind="vm", reference="local/windows-skills")
        ),
        resources=Resources(cpus=2, memory_mb=8192),
        network=NetworkPolicy(mode="open"),
    )
    async with await provider.create(request) as sandbox:
        home = sandbox.agent_home
        skills_dir = str(agent_path(home) / "image-skills")
        await sandbox.upload_dir(str(image_skills), skills_dir, identity=Identity.AGENT)
        resources = resolve_agent_resources(
            task=ToolProvision(skills_dir=skills_dir),
            agent_skills=(SkillSource(path=str(replacement), origin="run"),),
            task_source=TaskSource(kind="local", path=str(tmp_path)),
        )
        session = HarnessSession(
            episode_id=request.episode_id,
            gateway_url="http://gateway",
            token="episode-token",
            model="test-model",
            os="windows",
            home=home,
        )
        root = agent_path(home) / ".proof-skills"
        await install_skills(sandbox, session, resources, root)
        check = await sandbox.exec(
            [
                "python.exe",
                "-c",
                "import pathlib, sys; r = pathlib.Path(sys.argv[1]); "
                "assert (r / 'image-skill/SKILL.md').read_text() == 'image'; "
                "assert (r / 'shared/SKILL.md').read_text() == 'operator'",
                str(root),
            ],
            identity=Identity.AGENT,
        )
        assert check.ok, check.stderr
