from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.verdict import Status
from ale.run import task_images
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.framework import framework_identity, framework_on_disk
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers import ProviderRegistry
from ale.run.task_images import prepare_task_image_result
from ale.run.tasksets.manifest import load_tasks
from tests.support import VM_BASE

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]


def _steps(result: task_images.ImagePreparationResult) -> dict[str, str]:
    return {step.name: step.outcome for step in result.steps}


@pytest.mark.asyncio
async def test_a_bare_linux_qcow2_ref_gets_the_framework_once_and_runs(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
    kvm_host: None,
) -> None:
    # A disk ALE did not prepare: the VM base and the Task's layer, without the framework.
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text(
        f"FROM {VM_BASE}\n"
        "RUN mkdir -p /home/user/input /home/user/output \\\n"
        " && printf 'world' > /home/user/input/word.txt \\\n"
        " && chown -R user:user /home/user/input /home/user/output\n"
    )
    built = subprocess.run(
        ["docker", "buildx", "build", "--load", "--provenance=false", "-q", str(context)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    bare = tmp_path / "bare.qcow2"
    await task_images._materialize(built, bare)
    builder = task_images.MATERIALIZER_IMAGE
    assert await framework_on_disk(bare, builder=builder) is None

    def task_with_ref(name: str, disk: Path) -> Path:
        return write_repo(
            tmp_path / name, image={"kind": "vm", "ref": str(disk)}, with_image_dockerfile=False
        )

    registry = ProviderRegistry(RunConfig())
    task_path = task_with_ref("bare-ref", bare)
    first = await prepare_task_image_result(load_tasks(task_path)[0], registry)
    assert _steps(first)["framework-layer"] == "executed"
    framed = Path(first.image.runtime_ref)
    assert framed != bare and first.image.resolved_reference == bare.as_uri()
    identity = framework_identity(user="user", sudo=False, vm=True)
    assert await framework_on_disk(framed, builder=builder) == identity

    second = await prepare_task_image_result(load_tasks(task_path)[0], registry)
    assert _steps(second)["framework-layer"] == "reused"
    assert second.image == first.image

    # A disk that already carries this framework is used as it is, without a copy.
    carried = await prepare_task_image_result(
        load_tasks(task_with_ref("framed-ref", framed))[0], registry
    )
    assert _steps(carried)["framework-layer"] == "reused"
    assert carried.image.runtime_ref == str(framed)

    task = load_tasks(task_path)[0]
    task.prepared_image = first.image
    result = await run_episode(
        task, StandardEnvironment(OracleHarness()), registry, run_dir=tmp_path / "runs"
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
