from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from orbit.core.taskspec import OperatingSystem
from orbit.core.verdict import Status
from orbit.run import HELPER_RUNTIME, task_images
from orbit.run.episode import run_episode
from orbit.run.framework import MATERIALIZER_IMAGE, framework_identity, framework_on_disk
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.images import pull_vm_image
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.providers.qemu import QemuProvider
from orbit.run.task_images import prepare_task_image_result
from orbit.run.tasksets.manifest import load_tasks
from tests.support import VM_BASE, VM_RESOURCES, provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


def _steps(result: task_images.ImagePreparationResult) -> dict[str, str]:
    return {step.name: step.outcome for step in result.steps}


@pytest.mark.asyncio
async def test_a_bare_linux_qcow2_ref_gets_the_framework_once_and_runs(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_orbit_cache: Path,
    docker_buildx: None,
    kvm_host: None,
) -> None:
    # A disk Orbit did not prepare: the VM base and the Task's files, without the framework.
    bare = tmp_path / "bare.qcow2"
    await pull_vm_image(VM_BASE, bare)
    subprocess.run(
        ["docker", "run", "--rm", *HELPER_RUNTIME, "--device=/dev/kvm", "--network=none",
         "--mount", f"type=bind,src={bare},dst=/disk.qcow2",
         "--entrypoint", "virt-customize", MATERIALIZER_IMAGE,
         "--format", "qcow2", "--add", "/disk.qcow2", "--no-logfile", "--run-command",
         "mkdir -p /home/user/input /home/user/output"
         " && printf world > /home/user/input/word.txt"
         " && chown -R user:user /home/user/input /home/user/output"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    builder = MATERIALIZER_IMAGE
    linux = OperatingSystem.LINUX
    assert await framework_on_disk(bare, operating_system=linux, builder=builder) is None

    def task_with_ref(name: str, disk: Path) -> Path:
        return write_repo(
            tmp_path / name,
            config={"environment": {"image": str(disk), **VM_RESOURCES}},
            dockerfile=None,
        )

    provider = QemuProvider()
    task_path = task_with_ref("bare-ref", bare)
    first = await prepare_task_image_result(load_tasks(task_path)[0], provider)
    assert _steps(first)["framework-layer"] == "executed"
    framed = Path(first.image.runtime_ref)
    assert framed != bare and first.image.resolved_reference == bare.as_uri()
    identity = framework_identity(user="user", vm=True)
    assert await framework_on_disk(framed, operating_system=linux, builder=builder) == identity
    # The framework is a layer on a copy of the disk, beside it in the cache.
    info = json.loads(
        subprocess.run(
            ["qemu-img", "info", "-U", "--output=json", str(framed)],
            capture_output=True, text=True, check=True,
        ).stdout
    )  # fmt: skip
    assert Path(info["full-backing-filename"]).parent == framed.parent

    second = await prepare_task_image_result(load_tasks(task_path)[0], provider)
    assert _steps(second)["framework-layer"] == "reused"
    assert second.image == first.image

    # A disk that already carries this framework is used as it is, without a layer. A disk
    # named by path must be standalone, so the framed one is flattened for the test.
    standalone = tmp_path / "framed.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(framed), str(standalone)], check=True)
    carried = await prepare_task_image_result(
        load_tasks(task_with_ref("framed-ref", standalone))[0], provider
    )
    assert _steps(carried)["framework-layer"] == "reused"
    assert carried.image.runtime_ref == str(standalone)

    task = load_tasks(task_path)[0]
    task.prepared_image = first.image
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
