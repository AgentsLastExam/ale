from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.errors import ProviderStartError
from ale.core.taskspec import ImageKind
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers import ProviderRegistry
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image, prepare_task_image_result
from ale.run.tasksets.manifest import load_tasks

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

VM_BASE = "ghcr.io/agentslastexam/vm-ubuntu24-base:0.1.0"
IMAGE_ROOT = Path(__file__).resolve().parents[2] / "images"


def test_vm_base_and_materializer_sources_define_the_offline_contract() -> None:
    vm_base = IMAGE_ROOT / "base/vm-ubuntu24"
    materializer = IMAGE_ROOT / "builders/vm-materializer"
    runner = IMAGE_ROOT / "runtimes/qemu-runner"
    dockerfile = (vm_base / "Dockerfile").read_text()
    for required in (
        "ubuntu:24.04@sha256:",
        "linux-image-generic",
        "systemd",
        "ubuntu-desktop",
        "gdm3.service",
        "CUA_DRIVER_VERSION=0.12.6",
        "cua-driver serve",
        "sudo",
        "ale-guestd.service",
    ):
        assert required in dockerfile
    for forbidden in (
        "docker.io",
        "docker-ce",
        "ros-",
        "gazebo",
        "python3-pil",
        "python3-xlib",
        "xdotool",
    ):
        assert forbidden not in dockerfile

    manifest = json.loads((vm_base / "image.json").read_text())
    assert manifest == {
        "schema": 1,
        "os": "ubuntu",
        "release": "24.04",
        "user": "user",
        "gui": True,
        "port": 7411,
        "guest_contract": "ale-guest/v1",
        "base_release": "0.1.0",
    }

    materialize = (materializer / "materialize.sh").read_text()
    assert "truncate -s 40G" not in materialize
    assert "qemu-img convert" in materialize
    assert "qemu-img check" in materialize
    assert (materializer / "10-root.conf").read_text().find("GrowFileSystem=yes") >= 0

    guestd_unit = (vm_base / "ale-guestd.service").read_text()
    assert "After=network.target" in guestd_unit
    assert "network-online.target" not in guestd_unit

    runner_entrypoint = (runner / "entrypoint.sh").read_text()
    assert (
        'iptables -t nat -A POSTROUTING -p tcp -d 172.30.0.2 --dport "$guest_port"'
        in runner_entrypoint
    )


@pytest.mark.asyncio
async def test_local_container_build_reuse_and_input_boundaries(tmp_path: Path) -> None:
    task_path = scaffold_task(tmp_path / "task")
    registry = ProviderRegistry(RunConfig())
    first, second = await asyncio.gather(
        prepare_task_image(load_tasks(task_path)[0], registry),
        prepare_task_image(load_tasks(task_path)[0], registry),
    )
    assert second.input_identity == first.input_identity
    assert second.prepared_identity == first.prepared_identity

    verifier = task_path / "verify" / "verify.py"
    verifier.write_text(verifier.read_text() + "\n# verifier-only edit\n")
    verifier_edit = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert verifier_edit.input_identity == first.input_identity

    dockerfile = task_path / "image" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\nRUN true\n")
    image_edit = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert image_edit.input_identity != first.input_identity


@pytest.mark.asyncio
async def test_local_vm_materialization_reuse_and_late_layer_invalidation(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
) -> None:
    task_path = write_repo(tmp_path / "repo", image={"kind": "vm"})
    dockerfile = task_path / "image" / "Dockerfile"
    dockerfile.write_text(f"FROM {VM_BASE}\nRUN printf first > /etc/ale/task-layer\n")
    registry = ProviderRegistry(RunConfig())

    first, second = await asyncio.gather(
        prepare_task_image(load_tasks(task_path)[0], registry),
        prepare_task_image(load_tasks(task_path)[0], registry),
    )
    assert first.kind is ImageKind.VM
    assert second == first
    assert Path(first.runtime_ref).is_file()
    assert list((isolated_ale_cache / "vm-builds").glob("*.qcow2")) == [Path(first.runtime_ref)]

    started = time.monotonic()
    reused = await prepare_task_image_result(load_tasks(task_path)[0], registry)
    assert time.monotonic() - started < 5
    assert any(
        step.name == "vm-materialization" and step.outcome == "reused" for step in reused.steps
    )

    verifier = task_path / "verify" / "verify.py"
    verifier.write_text(verifier.read_text() + "\n# unrelated edit\n")
    assert await prepare_task_image(load_tasks(task_path)[0], registry) == first

    dockerfile.write_text(f"FROM {VM_BASE}\nRUN printf second > /etc/ale/task-layer\n")
    changed = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert changed.oci_identity != first.oci_identity
    assert changed.prepared_identity != first.prepared_identity


@pytest.mark.asyncio
async def test_failed_local_vm_build_leaves_no_partial_disk(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
) -> None:
    task_path = write_repo(tmp_path / "repo", image={"kind": "vm"})
    (task_path / "image" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    with pytest.raises(ProviderStartError, match="no initramfs"):
        await prepare_task_image(
            load_tasks(task_path)[0],
            ProviderRegistry(RunConfig()),
        )
    root = isolated_ale_cache / "vm-builds"
    assert not list(root.glob("*.qcow2"))
    assert not list(root.glob(".*-*"))


@pytest.mark.asyncio
async def test_episode_starts_exact_prepared_local_image(tmp_path: Path) -> None:
    task = load_tasks(scaffold_task(tmp_path / "task"))[0]
    providers = ProviderRegistry(RunConfig())
    task.prepared_image = await prepare_task_image(task, providers)
    result = await run_episode(
        task,
        StandardEnvironment(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"content": 1.0, "overall": 1.0}
