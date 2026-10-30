from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.errors import ProviderStartError
from ale.core.taskspec import ImageKind
from ale.core.verdict import Status
from ale.run import task_images
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers import ProviderRegistry
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image, prepare_task_image_result
from ale.run.tasksets.manifest import load_tasks
from tests.support import VM_BASE

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

VM_BASE = VM_BASE
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
        "sudo",
    ):
        assert required in dockerfile
    # The engine adds guestd and Cua Driver to every prepared image; the base carries neither.
    for forbidden in (
        "/opt/ale",
        "ale-guestd.service",
        "cua-driver",
        "LABEL",
        "nftables",
        "docker.io",
        "docker-ce",
        "ros-",
        "gazebo",
        "python3-pil",
        "python3-xlib",
        "xdotool",
    ):
        assert forbidden not in dockerfile

    materialize = (materializer / "materialize.sh").read_text()
    assert "truncate -s 40G" not in materialize
    assert "qemu-img convert" in materialize
    assert "qemu-img check" in materialize
    assert (materializer / "10-root.conf").read_text().find("GrowFileSystem=yes") >= 0

    runner_entrypoint = (runner / "entrypoint.sh").read_text()
    assert (
        'iptables -t nat -A POSTROUTING -p tcp -d 172.30.0.2 --dport "$guest_port"'
        in runner_entrypoint
    )


@pytest.mark.asyncio
async def test_local_container_build_reuse_and_input_boundaries(
    tmp_path: Path, isolated_ale_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_path = scaffold_task(tmp_path / "task")
    registry = ProviderRegistry(RunConfig())
    first, second = await asyncio.gather(
        prepare_task_image(load_tasks(task_path)[0], registry),
        prepare_task_image(load_tasks(task_path)[0], registry),
    )
    assert second.input_identity == first.input_identity
    assert second.prepared_identity == first.prepared_identity

    # An unchanged Task uses its prepared image directly; Docker is not asked to build.
    commands: list[tuple[str, ...]] = []
    run = task_images._run

    async def recording_run(*argv: str, timeout: float = 1800) -> tuple[int, str, str]:
        commands.append(argv)
        return await run(*argv, timeout=timeout)

    monkeypatch.setattr(task_images, "_run", recording_run)
    reused = await prepare_task_image_result(load_tasks(task_path)[0], registry)
    monkeypatch.setattr(task_images, "_run", run)
    assert reused.image == first
    assert [(step.name, step.outcome) for step in reused.steps[1:3]] == [
        ("oci-build", "reused"),
        ("framework-layer", "reused"),
    ]
    assert not any(command[:2] == ("docker", "buildx") for command in commands)

    verifier = task_path / "tests" / "verify.py"
    verifier.write_text(verifier.read_text() + "\n# verifier-only edit\n")
    verifier_edit = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert verifier_edit.input_identity == first.input_identity

    dockerfile = task_path / "environment" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\nRUN true\n")
    image_edit = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert image_edit.input_identity != first.input_identity


# Building a VM disk takes minutes even with KVM, so these run with the VM suite.
@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_setup_never_enters_the_container_build_context(
    tmp_path: Path, isolated_ale_cache: Path, docker_buildx: None
) -> None:
    task_path = scaffold_task(tmp_path / "task")
    environment = task_path / "environment"
    (environment / "keep.txt").write_text("kept\n")
    (environment / "ignored.txt").write_text("ignored\n")
    # The Task's own rules still apply, and cannot bring setup/ back.
    (environment / ".dockerignore").write_text("ignored.txt\n!setup\n!setup/**\n")
    (environment / "setup").mkdir()
    (environment / "setup" / "setup.sh").write_text("true\n")
    (environment / "Dockerfile").write_text(
        (environment / "Dockerfile").read_text()
        + "COPY . /opt/context/\n"
        + "RUN test -f /opt/context/keep.txt && test ! -e /opt/context/ignored.txt "
        + "&& test ! -e /opt/context/setup\n"
    )
    registry = ProviderRegistry(RunConfig())
    first = await prepare_task_image(load_tasks(task_path)[0], registry)

    (environment / "setup" / "setup.sh").write_text("echo changed\n")
    assert await prepare_task_image(load_tasks(task_path)[0], registry) == first


@pytest.mark.asyncio
async def test_local_vm_materialization_reuse_and_late_layer_invalidation(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
) -> None:
    task_path = write_repo(tmp_path / "repo", config={"environment": {"vm": True}})
    dockerfile = task_path / "environment" / "Dockerfile"
    dockerfile.write_text(f"FROM {VM_BASE}\nRUN printf first > /etc/task-layer\n")
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

    verifier = task_path / "tests" / "verify.py"
    verifier.write_text(verifier.read_text() + "\n# unrelated edit\n")
    assert await prepare_task_image(load_tasks(task_path)[0], registry) == first

    dockerfile.write_text(f"FROM {VM_BASE}\nRUN printf second > /etc/task-layer\n")
    changed = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert changed.oci_identity != first.oci_identity
    assert changed.prepared_identity != first.prepared_identity


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_failed_local_vm_build_leaves_no_partial_disk(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
) -> None:
    task_path = write_repo(tmp_path / "repo", config={"environment": {"vm": True}})
    # It has the agent account, so the framework layer succeeds and materialization fails.
    (task_path / "environment" / "Dockerfile").write_text(
        "FROM ubuntu:24.04\nRUN useradd -m user\n"
    )
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
        EpisodeProtocol(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
