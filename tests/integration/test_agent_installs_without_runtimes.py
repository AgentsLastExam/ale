"""A Task image without Node.js, npm or Python installs and starts agent programs from npm.

Native programs and JavaScript ones, with the Node.js these run on, are fetched on the Host
and uploaded into a Sandbox that is already sealed, which verifies and unpacks them on the
framework's Python, so the image needs neither runtime nor network for them, and the agent's
PATH still finds no Node.js. The Agent Judge's program reaches a sealed verifier the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from orbit.core.errors import GuestUnreachableError
from orbit.core.sandbox import Identity, NetworkPolicy, Resources, Sandbox, SandboxRequest
from orbit.run.harnesses._agent import agent_env
from orbit.run.harnesses.base import CliHarness
from orbit.run.harnesses.claude_code import ClaudeCodeHarness
from orbit.run.harnesses.codex_cli import CodexCliHarness
from orbit.run.harnesses.grok_build import GrokBuildHarness
from orbit.run.harnesses.openclaw_cli import OpenClawCliHarness
from orbit.run.harnesses.pi_cli import PiCliHarness
from orbit.run.providers.docker import DockerProvider
from orbit.run.task_images import prepare_task_image
from orbit.run.tasksets.manifest import load_tasks
from orbit.run.verification import install_agent_judge
from tests.support import cli_sandbox, write_task

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_cli_install]

BARE = """\
FROM debian:bookworm-slim
RUN useradd -m -s /bin/bash user \\
 && ! command -v node && ! command -v npm && ! command -v python3 && ! command -v python
CMD ["sleep", "infinity"]
"""


@asynccontextmanager
async def bare_sandbox(tmp_path: Path, name: str) -> AsyncIterator[Sandbox]:
    """A sealed Sandbox of an image that has neither Node.js nor Python."""
    provider = DockerProvider()
    task = write_task(tmp_path / "task", name="agentslastexam/bare-runtime", dockerfile=BARE)
    image = await prepare_task_image(load_tasks(task)[0], provider)
    sandbox = await provider.create(
        SandboxRequest(
            episode_id=f"bare-{name}",
            prepared_image=image,
            resources=Resources(cpus=2, memory_mb=4096),
            network=NetworkPolicy(),
        )
    )
    try:
        await sandbox.close_egress()
        yield sandbox
    finally:
        await sandbox.destroy()


@pytest.mark.parametrize(
    "factory",
    [CodexCliHarness, ClaudeCodeHarness, GrokBuildHarness, PiCliHarness, OpenClawCliHarness],
    ids=["codex-cli", "claude-code", "grok-build", "pi-cli", "openclaw-cli"],
)
async def test_a_bare_sealed_image_runs_the_agent_program(
    tmp_path: Path, factory: type[CliHarness], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = factory()
    async with bare_sandbox(tmp_path, harness.name) as sandbox:
        uploads: list[str] = []
        upload = sandbox.upload_dir

        async def counted(source: str, target: str, **kwargs: object) -> None:
            uploads.append(target)
            await upload(source, target, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(sandbox, "upload_dir", counted)
        assert await harness.install(sandbox) == harness.version()
        assert uploads
        env = await agent_env(sandbox, sandbox.agent_home)
        found = await sandbox.exec(
            ["sh", "-c", "command -v node || echo none"], env=env, identity=Identity.AGENT
        )
        assert found.stdout.strip() == "none"
        # The agent's ~/.local stays its own.
        owners = await sandbox.exec(
            ["stat", "-c", "%U", f"{sandbox.agent_home}/.local", f"{sandbox.agent_home}/.local/bin"]
        )
        assert owners.stdout.split() == ["user", "user"]
        # Installed already: a second install uploads nothing and still names the version.
        uploads.clear()
        assert await harness.install(sandbox) == harness.version()
        assert uploads == []


async def test_the_agent_judge_program_reaches_a_sealed_verifier(tmp_path: Path) -> None:
    async with bare_sandbox(tmp_path, "judge") as sandbox:
        executable = await install_agent_judge(
            sandbox, "codex-cli", "0.146.0", "/opt/orbit/judge", fetch=False
        )
        assert executable.startswith("/opt/orbit/judge/codex/0.146.0/")
        version = await sandbox.exec([executable, "--version"])
        assert version.ok and "0.146.0" in version.stdout
        # The framework's copy, out of the agent's reach.
        with pytest.raises(GuestUnreachableError, match="Permission denied"):
            await sandbox.exec([executable, "--version"], identity=Identity.AGENT)


async def test_the_agent_judge_program_is_the_frameworks_on_every_system(tmp_path: Path) -> None:
    """Uploaded or downloaded by the Sandbox, the Judge's copy runs for the framework alone:
    on Linux, and in a macOS VM with ``ORBIT_TEST_CLI_OS=macos``."""
    async with cli_sandbox("judge-copy", "http://127.0.0.1:1", tmp_path) as (sandbox, _):
        for name, fetch in (("uploaded", False), ("downloaded", True)):
            if fetch:
                # As a verifier whose network is open.
                await sandbox.open_egress()
            executable = await install_agent_judge(
                sandbox, "codex-cli", "0.146.0", f"/opt/orbit/judge-{name}", fetch=fetch
            )
            version = await sandbox.exec([executable, "--version"])
            assert version.ok and "0.146.0" in version.stdout
            reach = await sandbox.exec(["/bin/test", "-x", executable], identity=Identity.AGENT)
            assert not reach.ok
