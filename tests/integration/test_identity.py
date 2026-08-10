"""Who runs what, and what that stops them doing.

The agent must reach everything it needs and nothing that would let it change the
conditions of its own measurement. Both halves fail quietly when they fail: an agent that
cannot write its workspace reports a task failure that has nothing to do with the task,
and an agent that can rewrite the network policy produces a number that looks fine.

These run against the real base image, because the identity is the image's declaration
and a fake would only test our own assumption about it.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.lock import TaskSource
from ale.core.sandbox import Identity
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers.docker import DEFAULT_AGENT_USER, DockerProvider
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

ELEVATION_PROBE = """
    if sudo -n true 2>/dev/null; then echo yes > /home/user/output/sudo
    else echo no > /home/user/output/sudo; fi
    """


async def run_one(task_root: Path, run_dir: Path, **kwargs):  # type: ignore[no-untyped-def]
    task = load_tasks(task_root)[0]
    return await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
        **kwargs,
    )


def with_oracle(task_root: Path, body: str) -> None:
    (task_root / "oracle" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\nmkdir -p /home/user/output\n"
        + textwrap.dedent(body)
    )


def declare_sudo(task_root: Path) -> None:
    manifest = task_root / "task.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "resources: {cpus: 1, memory_mb: 512}",
            "resources: {cpus: 1, memory_mb: 512, sudo: true}",
        )
    )


def collected(result, name: str) -> str:  # type: ignore[no-untyped-def]
    return (result.run_dir / "artifacts" / "output" / name).read_text().strip()


class TestWhoRunsWhat:
    @pytest.mark.asyncio
    async def test_the_oracle_runs_as_the_agent_not_as_root(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """The oracle stands in for the agent, so it meets the agent's limits.

        With more privilege it would pass exactly the tasks a real agent then fails on
        access alone — and it is the only check a task gets before publication.
        """
        task_root = write_repo(tmp_path / "repo")
        with_oracle(task_root, "id -un > /home/user/output/who\n")

        result = await run_one(task_root, tmp_path / "runs")

        assert result.verdict.status is Status.COMPLETED, result.verdict.failure
        assert collected(result, "who") == DEFAULT_AGENT_USER

    @pytest.mark.asyncio
    async def test_setup_runs_as_the_framework(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """A task's stages are engine machinery, run on the task's behalf."""
        task_root = write_repo(tmp_path / "repo")
        (task_root / "setup").mkdir()
        (task_root / "setup" / "run.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p /home/user/input /home/user/output\n"
            "printf 'world' > /home/user/input/word.txt\nid -un > /home/user/output/setup_who\n"
        )
        with_oracle(
            task_root,
            "word=$(cat /home/user/input/word.txt)\n"
            "printf 'hello %s' \"$word\" > /home/user/output/result.txt\n",
        )

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "setup_who") == "root"


class TestWhatTheAgentCanReach:
    @pytest.mark.asyncio
    async def test_the_agent_can_write_what_was_staged_for_it(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """Declared paths are created by the framework and handed over at once.

        An agent that cannot write its own workspace fails in a way that looks like a
        task defect and is not one.
        """
        task_root = write_repo(tmp_path / "repo")
        with_oracle(
            task_root,
            """
            if touch /home/user/output/probe 2>/dev/null; then echo yes > /home/user/output/writable
            else echo no > /home/user/output/writable; fi
            """,
        )

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "writable") == "yes"

    @pytest.mark.asyncio
    async def test_the_agent_cannot_reach_what_drives_its_sandbox(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """Reading the guest service would hand the agent the protocol driving it."""
        task_root = write_repo(tmp_path / "repo")
        with_oracle(
            task_root,
            """
            if cat /opt/ale/guestd/main.py >/dev/null 2>&1; then echo yes > /home/user/output/saw
            else echo no > /home/user/output/saw; fi
            """,
        )

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "saw") == "no"

    @pytest.mark.asyncio
    async def test_the_agent_cannot_alter_the_system(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """Changing system configuration is how an agent would rewrite its own limits."""
        task_root = write_repo(tmp_path / "repo")
        with_oracle(
            task_root,
            """
            if touch /etc/ale-probe 2>/dev/null; then echo yes > /home/user/output/wrote
            else echo no > /home/user/output/wrote; fi
            """,
        )

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "wrote") == "no"


class TestPrivilegeDeclaration:
    @pytest.mark.asyncio
    async def test_no_elevation_unless_the_task_asked(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        task_root = write_repo(tmp_path / "repo")
        with_oracle(task_root, ELEVATION_PROBE)

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "sudo") == "no"

    @pytest.mark.asyncio
    async def test_a_declared_need_is_actually_granted(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """Granted, not merely configured.

        Writing a sudoers rule succeeds in an image with no sudo at all, so the provider
        confirms the grant by using it — otherwise a task would be told it had a privilege
        it never received, and provenance would record an isolation that never held.
        """
        task_root = write_repo(tmp_path / "repo")
        declare_sudo(task_root)
        with_oracle(task_root, ELEVATION_PROBE)

        result = await run_one(task_root, tmp_path / "runs")
        assert collected(result, "sudo") == "yes"

    @pytest.mark.asyncio
    async def test_the_grant_is_recorded_in_provenance(
        self, tmp_path: Path, write_repo: Callable[..., Path]
    ) -> None:
        """Two results at different isolation levels are not comparable."""
        task_root = write_repo(tmp_path / "repo")
        declare_sudo(task_root)
        with_oracle(task_root, "true\n")

        settings = RunConfig()
        inputs = ProvenanceInputs(
            source=TaskSource(kind="registry", repo="https://e/x.git", commit="a" * 40, path="t"),
            agent=agent_provenance(OracleHarness(), "none"),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        )

        result = await run_one(task_root, tmp_path / "runs", provenance=inputs)

        assert result.lock is not None and result.lock.sandbox is not None
        assert result.lock.sandbox.sudo is True
        assert result.lock.sandbox.user == DEFAULT_AGENT_USER


class TestIdentityContract:
    def test_the_two_roles_are_distinct(self) -> None:
        """Naming roles rather than accounts keeps the choice where it is made."""
        assert Identity.FRAMEWORK != Identity.AGENT
        assert {i.value for i in Identity} == {"framework", "agent"}
