from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.errors import TaskDefinitionError
from ale.core.lock import TaskSource
from ale.core.taskspec import ImageKind
from ale.core.verdict import Status
from ale.run import task_images
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.provenance import ProvenanceInputs, agent_provenance, gateway_provenance
from ale.run.providers import ProviderRegistry
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image, prepare_task_image_result
from ale.run.tasksets.manifest import load_tasks
from tests.integration.conftest import HELLO_BUILD
from tests.support import VM_BASE, VM_RESOURCES

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

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
    # Uncompressed: `ale vm-image push` refuses a disk with compressed clusters.
    (convert,) = [line for line in materialize.splitlines() if line.startswith("qemu-img convert")]
    assert " -c " not in f"{convert} "
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


def _chain(disk: Path) -> list[Path]:
    info = subprocess.run(
        ["qemu-img", "info", "-U", "--backing-chain", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [Path(entry["filename"]).resolve() for entry in json.loads(info.stdout)]


def _allocated(disk: Path) -> int:
    """The bytes a disk file takes on the Host."""
    return disk.stat().st_blocks * 512


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_linux_vm_build_is_one_layer_on_the_pulled_base(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
    kvm_host: None,
) -> None:
    task_path = write_repo(
        tmp_path / "repo",
        config={"environment": {"image": VM_BASE, **VM_RESOURCES}},
        dockerfile=None,
        build_script=HELLO_BUILD,
    )
    build = task_path / "environment" / "build.sh"
    registry = ProviderRegistry(RunConfig())

    first, second = await asyncio.gather(
        prepare_task_image(load_tasks(task_path)[0], registry),
        prepare_task_image(load_tasks(task_path)[0], registry),
    )
    assert first.kind is ImageKind.VM and first.source == "solver-local"
    assert second == first
    # The base is pulled once, kept by its manifest digest, and the build's layer, the
    # framework included, sits on it in place.
    layer = Path(first.runtime_ref)
    (base,) = (isolated_ale_cache / "vm-images").glob("*/disk.qcow2")
    assert first.base_materials == ("sha256:" + base.parent.name,)
    assert layer.parent == isolated_ale_cache / "vm-builds"
    assert _chain(layer) == [layer, base.resolve()]

    started = time.monotonic()
    reused = await prepare_task_image_result(load_tasks(task_path)[0], registry)
    assert time.monotonic() - started < 30
    assert ("linux-build", "reused") in [(step.name, step.outcome) for step in reused.steps]

    verifier = task_path / "tests" / "verify.py"
    verifier.write_text(verifier.read_text() + "\n# unrelated edit\n")
    assert await prepare_task_image(load_tasks(task_path)[0], registry) == first

    # Another Task on the same base uses the same pulled disk, and its own layer holds only
    # the framework and what its build wrote.
    other = write_repo(
        tmp_path / "other",
        config={"environment": {"image": VM_BASE, **VM_RESOURCES}},
        dockerfile=None,
        build_script=HELLO_BUILD + "printf other > /etc/other-task\n",
    )
    own = Path((await prepare_task_image(load_tasks(other)[0], registry)).runtime_ref)
    assert list((isolated_ale_cache / "vm-images").glob("*/disk.qcow2")) == [base]
    assert own != layer and _chain(own) == [own, base.resolve()]
    for disk in (layer, own):
        assert _allocated(disk) < min(_allocated(base) // 10, 1 << 30)

    # Every Sandbox boots its own overlay on the layer, which none of them writes.
    task = load_tasks(task_path)[0]
    task.prepared_image = first
    settings = RunConfig()
    result = await run_episode(
        task,
        EpisodeProtocol(OracleHarness()),
        registry,
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=TaskSource(kind="local", path=str(task_path)),
            agent=agent_provenance(OracleHarness(), ""),
            gateway=gateway_provenance(settings),
            config_hash=settings.config_hash,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.lock is not None
    assert result.lock.image.builder_identity == first.builder_identity
    assert result.lock.image.base_materials == first.base_materials
    assert await prepare_task_image(load_tasks(task_path)[0], registry) == first

    build.write_text(build.read_text() + "printf second > /etc/task-layer\n")
    changed = await prepare_task_image(load_tasks(task_path)[0], registry)
    assert changed.prepared_identity != first.prepared_identity
    assert changed.base_materials == first.base_materials
    assert _chain(Path(changed.runtime_ref))[1:] == [base.resolve()]


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_failed_linux_vm_build_leaves_no_partial_disk(
    tmp_path: Path,
    write_repo: Callable[..., Path],
    isolated_ale_cache: Path,
    docker_buildx: None,
    kvm_host: None,
) -> None:
    task_path = write_repo(
        tmp_path / "repo",
        config={"environment": {"image": VM_BASE, **VM_RESOURCES}},
        dockerfile=None,
        build_script="echo 'the build broke' >&2\nexit 3\n",
    )
    with pytest.raises(TaskDefinitionError, match=r"build\.sh failed \(exit=3.*the build broke"):
        await prepare_task_image(load_tasks(task_path)[0], ProviderRegistry(RunConfig()))
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
