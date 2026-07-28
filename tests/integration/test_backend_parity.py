"""The same task, the same verdict, on a container and on a virtual machine.

The sandbox contract is only real if a task cannot tell which backend it landed in. This
runs one task repository through both, unchanged — same manifest, same setup, same oracle,
same verifier — and requires the two verdicts to agree. A backend that quietly differs
(an agent that is root here and not there, a stage that runs as the wrong identity, a
network that is open on one side) shows up as a disagreement rather than as a surprise
months later in a published number.

The container half also runs in the ordinary suite; the value here is the comparison, so
both halves live in one test and skip together when there is no KVM or no guest image.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, Provider, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.tasksets.manifest import ManifestTaskset

#: The container half needs an image that satisfies the contract; the VM half ignores it
#: and boots the disk it was given.
CONTAINER_IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

DEFAULT_IMAGE = Path.home() / ".cache/ale/images/ale-ubuntu-desktop.qcow2"
IMAGE = Path(os.environ.get("ALE_QEMU_IMAGE", DEFAULT_IMAGE))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_docker,
    pytest.mark.needs_kvm,
    pytest.mark.skipif(
        not IMAGE.is_file(),
        reason=f"no guest image at {IMAGE}; build one with images/base/qemu/build-desktop.sh",
    ),
]


async def _run(task_root: Path, run_dir: Path, provider: Provider):  # type: ignore[no-untyped-def]
    task = next(iter(ManifestTaskset(task_root).load()))
    return await run_episode(task, StandardEnvironment(OracleHarness()), provider, run_dir=run_dir)


@pytest.mark.asyncio
async def test_both_backends_reach_the_same_verdict(
    tmp_path: Path, write_repo: Callable[..., Path]
) -> None:
    task_root = write_repo(tmp_path / "repo")

    container = await _run(task_root, tmp_path / "runs-docker", DockerProvider())
    machine = await _run(task_root, tmp_path / "runs-qemu", QemuProvider(image=IMAGE))

    assert container.verdict.status is Status.COMPLETED, container.verdict.failure
    assert machine.verdict.status is Status.COMPLETED, machine.verdict.failure
    assert container.verdict.primary_reward == machine.verdict.primary_reward == 1.0


@pytest.mark.asyncio
async def test_both_backends_run_each_stage_as_the_same_identity() -> None:
    """Who runs what is the difference a task would notice first.

    Compared directly rather than through a verdict: a backend that ran the agent as root
    would still solve the task above, and the divergence would only surface in a result
    someone later has to defend.
    """
    request = SandboxRequest(
        episode_id="parity",
        image_ref=CONTAINER_IMAGE,
        resources=Resources(cpus=1, memory_mb=1024),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    seen: dict[str, tuple[str, str]] = {}
    for name, provider in (
        ("docker", DockerProvider()),
        ("qemu", QemuProvider(image=IMAGE)),
    ):
        sandbox = await provider.create(request)
        try:
            framework = await sandbox.exec(["id", "-un"], identity=Identity.FRAMEWORK)
            agent = await sandbox.exec(["id", "-un"], identity=Identity.AGENT)
            seen[name] = (framework.stdout.strip(), agent.stdout.strip())
        finally:
            await sandbox.destroy()

    assert seen["docker"] == seen["qemu"]
    assert seen["qemu"][0] == "root"
    assert seen["qemu"][1] != "root"
