"""Volumes end to end on every Provider that mounts them, Docker, Slurm, Firecracker and
QEMU: the agent's home, an absolute directory artifact, as a volume of its own, kept by
every round's snapshot and seen by every verifier through a view of its own; on Docker,
what is written to it counted against storage_mb; and a container engine's storage kept by
a snapshot as a copy."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ale.core.errors import StorageExhaustedError
from ale.core.sandbox import Identity, ImageRef, Provider, SandboxRequest
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.core.verdict import Status
from ale.run.episode import EpisodeResult, run_episode
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.providers.docker import volumes as docker_volumes
from ale.run.providers.firecracker import FirecrackerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.providers.slurm import SlurmProvider
from ale.run.scaffold import scaffold_task
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks
from tests.support import (
    CONTAINER_BASE,
    VM_BASE,
    VM_RESOURCES,
    ScriptedCliHarness,
    edit_task_config,
    provider_registry,
    skip_without_gvisor,
    skip_without_kata,
    slurm_config,
    write_task,
)

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

HOME = "/home/user"
#: One verifier for validation/ and tests/: a third of the reward per line of work; what of
#: the image's link, FIFO and excluded file it sees; and whether an earlier verifier's
#: writes reached it, which it then makes itself.
VERIFY = """\
import os
from pathlib import Path
from ale_verify import write

home = Path(os.environ["ALE_HOME"])
work = home / "work.txt"
lines = work.read_text().splitlines() if work.is_file() else []
seen = sorted(name for name in ("link", "fifo", "cache") if os.path.lexists(home / name))
marker = home / "verifier-was-here"
leaked = marker.exists()
marker.write_text("a verifier's own")
result = {"reward": round(min(1.0, len(lines) / 3), 2), "lines": lines, "seen": seen,
          "leaked": leaked}
write(result, feedback=f"{len(lines)} of 3 lines")
"""
TEST = '#!/usr/bin/env bash\nset -euo pipefail\nexec "$ALE_VERIFY_PYTHON" verify.py\n'
#: The image's home holds what copying never carries, which no separate verifier's view may
#: show.
MAKE = (
    f"ln -s /etc/hostname {HOME}/link && mkfifo {HOME}/fifo && mkdir {HOME}/cache "
    f"&& chown -h user:user {HOME}/link {HOME}/fifo {HOME}/cache"
)
CONTAINER = {"cpus": 1, "memory_mb": 1024}


def _task(root: Path, mode: str, validation: str | None, *, vm: bool = False) -> Path:
    scaffold_task(root)
    dockerfile = root / "environment" / "Dockerfile"
    if vm:
        dockerfile.unlink()
        (root / "environment" / "build.sh").write_text(f"set -eu\n{MAKE}\n")
    else:
        dockerfile.write_text(dockerfile.read_text() + f"\nRUN {MAKE}\n")
    size = {"cpus": 2, "memory_mb": 4096} if vm else CONTAINER
    verifier: dict[str, object] = {"environment_mode": mode, "timeout_sec": 600}
    if mode != "shared":
        verifier["environment"] = size
    config: dict[str, object] = {
        "artifacts": [{"source": HOME, "exclude": ["cache"]}],
        "environment": {"image": VM_BASE, **VM_RESOURCES} if vm else CONTAINER,
        "agent": {"validation_rounds": 2, "timeout_sec": 600},
        "verifier": verifier,
    }
    if validation is not None:
        verifier["validation"] = {"environment_mode": validation, "environment": size}
    edit_task_config(root, config)
    for stage in ("tests", "validation"):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / "test.sh").write_text(TEST)
        (root / stage / "verify.py").write_text(VERIFY)
    (root / "instruction.md").write_text("Complete the task described here.\n")
    return root


def _ale_volumes(owner: str) -> list[str]:
    listed = subprocess.run(
        ["docker", "volume", "ls", "--filter", f"label={docker_volumes.VOLUME_LABEL}",
         "--format", f'{{{{.Name}}}} {{{{.Label "{docker_volumes.VOLUME_LABEL}"}}}}'],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()  # fmt: skip
    return [line for line in listed if owner in line]


def _runtime(runtime: str) -> None:
    if runtime == "gvisor":
        skip_without_gvisor()
    if runtime == "kata":
        skip_without_kata()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "kata", "gvisor"])
@pytest.mark.parametrize(
    ("mode", "validation"),
    [("shared", None), ("snapshot", None), ("separate", None), ("shared", "separate")],
)
async def test_an_agent_works_in_a_volume_every_verifier_sees_through_a_view_of_its_own(
    tmp_path: Path, mode: str, validation: str | None, runtime: str
) -> None:
    _runtime(runtime)
    result = await _episode(tmp_path, mode, validation, DockerProvider(runtime=runtime))
    _check(result, mode, validation)
    assert _ale_volumes(result.episode_id) == []


@pytest.mark.needs_kvm
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "validation"), [("separate", None), ("snapshot", None), ("shared", "separate")]
)
async def test_an_agent_works_in_a_volume_of_a_microvm(
    tmp_path: Path, request: pytest.FixtureRequest, mode: str, validation: str | None
) -> None:
    """The volume is an overlay on the microVM's writable disk, which each snapshot keeps; a
    separate verifier mounts the snapshot's disks read-only under a layer of its own."""
    request.getfixturevalue("kvm_host")
    provider = FirecrackerProvider(
        overlay_dir=tmp_path / "overlays",
        snapshot_dir=tmp_path / "snapshots",
        disk_dir=tmp_path / "disks",
    )
    result = await _episode(tmp_path, mode, validation, provider)
    _check(result, mode, validation)
    assert await provider.list_retained() == []


@pytest.mark.needs_kvm
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "validation"), [("separate", None), ("snapshot", None), ("shared", "separate")]
)
async def test_an_agent_works_in_a_volume_of_a_linux_vm(
    tmp_path: Path, request: pytest.FixtureRequest, mode: str, validation: str | None
) -> None:
    """The volume is an overlay the guest mounts as it boots, on its own disk, which each
    snapshot keeps; a separate verifier's guest mounts the snapshot's disk read-only under
    a layer of its own."""
    request.getfixturevalue("docker_buildx")
    request.getfixturevalue("kvm_host")
    provider = QemuProvider(overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots")
    result = await _episode(tmp_path, mode, validation, provider, vm=True)
    _check(result, mode, validation)
    assert await provider.list_retained() == []


#: Windows keeps no FIFO and the agent's own profile cannot be a verifier's view, so the
#: volume there is the scripted CLI's session directory, whose turns the verifier counts.
WINDOWS_VOLUME = r"C:\Users\user\.scripted-cli"
WINDOWS_VERIFY = """\
import os
from pathlib import Path
from ale_verify import write

volume = Path(os.environ["ALE_HOME"]) / ".scripted-cli"
turns = sorted(path.name for path in volume.glob("scripted/turn-*.md"))
marker = volume / "verifier-was-here"
leaked = marker.exists()
marker.write_text("a verifier's own")
reward = round(min(1.0, len(turns) / 3), 2)
write({"reward": reward, "turns": turns, "leaked": leaked}, feedback=f"{len(turns)} of 3")
"""
WINDOWS_TEST = "& $env:ALE_VERIFY_PYTHON .\\verify.py\nexit $LASTEXITCODE\n"


@pytest.mark.needs_kvm
@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "validation"), [("separate", None), ("shared", "separate")])
async def test_an_agent_works_in_a_volume_of_a_windows_vm(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    validation: str | None,
) -> None:
    """The volume stays a directory of the disk, which each snapshot keeps; a separate
    verifier's guest brings a copy of the snapshot's disk online and finds it through a
    junction in place of its own."""
    request.getfixturevalue("kvm_host")
    image = Path(os.environ.get("ALE_TEST_WINDOWS_IMAGE", ""))
    if not image.is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to the Windows base qcow2")
    monkeypatch.setattr("ale.run.task_images.cache_root", lambda: tmp_path / "cache")
    size = {"cpus": 2, "memory_mb": 4096}
    config: dict[str, object] = {
        "artifacts": [WINDOWS_VOLUME],
        "environment": {"os": "windows", "image": "local/windows-base", **size},
        "agent": {"timeout_sec": 900, "validation_rounds": 2},
        "verifier": {"environment_mode": mode, "timeout_sec": 900}
        | ({"environment": size} if mode != "shared" else {})
        | (
            {"validation": {"environment_mode": validation, "environment": size}}
            if validation is not None
            else {}
        ),
    }
    root = write_task(
        tmp_path / f"windows-{mode}-{validation or 'alike'}",
        name=f"agentslastexam/windows-{mode}-{validation or 'alike'}",
        config=config,
        dockerfile=None,
        test=None,
        solve=None,
    )
    (root / "environment").mkdir(exist_ok=True)
    (root / "environment/build.ps1").write_text(
        f"New-Item -ItemType Directory -Force -Path '{WINDOWS_VOLUME}' | Out-Null\n"
    )
    (root / "calibration/solution").mkdir(parents=True)
    (root / "calibration/solution/solve.ps1").write_text("exit 0\n")
    for stage in ("tests", "validation"):
        (root / stage).mkdir(exist_ok=True)
        (root / stage / "test.ps1").write_text(WINDOWS_TEST)
        (root / stage / "verify.py").write_text(WINDOWS_VERIFY)
    provider = QemuProvider(
        image=image, overlay_dir=tmp_path / "qemu", snapshot_dir=tmp_path / "snapshots"
    )
    task = load_tasks(root)[0]
    result = await run_episode(
        task,
        EpisodeProtocol(ScriptedCliHarness(), validation_rounds=task.spec.validation_rounds),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert [(item.index, item.reward) for item in result.record.validation_rounds] == [
        (1, 0.33),
        (2, 0.67),
    ]
    assert json.loads((result.run_dir / "verification.json").read_text())["leaked"] is False
    # Nothing was copied: every separate verifier viewed the solver's disk.
    assert not (result.run_dir / "artifacts").exists()
    assert await provider.list_retained() == []


async def _episode(
    tmp_path: Path, mode: str, validation: str | None, provider: Provider, *, vm: bool = False
) -> EpisodeResult:
    root = _task(tmp_path / f"volume-{mode}-{validation or 'alike'}", mode, validation, vm=vm)
    task = load_tasks(root)[0]
    return await run_episode(
        task,
        EpisodeProtocol(ScriptedCliHarness(), validation_rounds=task.spec.validation_rounds),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
    )


def _check(result: EpisodeResult, mode: str, validation: str | None) -> None:
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    # Each turn resumed the native session it kept in the volume, and wrote to its root.
    assert result.verdict.rewards == {"reward": 1.0}
    assert [(item.index, item.reward) for item in result.record.validation_rounds] == [
        (1, 0.33),
        (2, 0.67),
    ]
    verification = json.loads((result.run_dir / "verification.json").read_text())
    assert verification["lines"][1:] == [
        "Validation round 1 of 2: reward 0.33.",
        "Validation round 2 of 2: reward 0.67.",
    ]
    # A separate verifier's view is what copying would have given it; the others see the
    # solver's disk as it is. None sees what an earlier verifier wrote.
    assert verification["seen"] == ([] if mode == "separate" else ["cache", "fifo", "link"])
    assert verification["leaked"] is False
    # Nothing was copied: every separate verifier, of tests/ or of validation/, viewed it.
    assert not (result.run_dir / "artifacts").exists()
    if validation == "separate":
        for index in (1, 2):
            assert not list((result.run_dir / "validation" / str(index)).glob("artifacts/*"))
    assert all(item.outcome == "destroyed" for item in result.record.sandboxes)


async def _request(**overrides: object) -> SandboxRequest:
    image = await DockerProvider().prepare_image(
        ImageRef(kind="container", reference=CONTAINER_BASE)
    )
    base: dict[str, object] = {
        "episode_id": "volume-storage",
        "prepared_image": image,
        "resources": Resources(cpus=1, memory_mb=1024),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest.model_validate(base | overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["runc", "kata", "gvisor"])
async def test_what_a_sandbox_writes_to_its_volume_counts_against_its_storage(
    runtime: str,
) -> None:
    """Measured from outside it, less what the image held there; past storage_mb it is
    stopped as when it writes past it anywhere else."""
    _runtime(runtime)
    provider = DockerProvider(runtime=runtime)
    request = await _request(
        volumes=("/srv",), resources=Resources(cpus=1, memory_mb=1024, storage_mb=256)
    )
    async with await provider.create(request) as sandbox:
        assert sandbox.volumes == ("/srv",)
        fill = "head -c 200M /dev/urandom > /srv/a && sleep 12 && echo still"
        assert (await sandbox.exec(["sh", "-c", fill], timeout_sec=120)).stdout.strip() == "still"
        with pytest.raises(StorageExhaustedError):
            await sandbox.exec(
                ["sh", "-c", "head -c 100M /dev/urandom > /srv/b && sleep 30"], timeout_sec=120
            )
    assert _ale_volumes(sandbox.sandbox_id) == []


ENGINE = """\
FROM {base}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \\
    && rm -rf /var/lib/apt/lists/*
"""
START_ENGINE = (
    "(dockerd >/tmp/dockerd.log 2>&1 &); "
    "for i in $(seq 120); do docker info >/dev/null 2>&1 && break; sleep 1; done; "
    "docker info >/dev/null"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaf", ["docker:gvisor", pytest.param("slurm", marks=pytest.mark.needs_slurm)]
)
async def test_a_container_engines_images_survive_its_sandboxs_snapshot(
    tmp_path: Path, leaf: str
) -> None:
    """An image built in a Sandbox that runs Docker, deleting a file of its base, is there in
    the Sandbox booted from its snapshot, which deletes another file of it: the snapshot's
    copy of the engine's storage keeps its whiteouts, where a view would hide them. On Slurm
    the snapshot, kept by its Run, is written out with its volumes, and the Sandbox booted
    from it loads them in an allocation of its own."""
    provider: DockerProvider | SlurmProvider
    if leaf == "slurm":
        provider = SlurmProvider(slurm_config())
    else:
        skip_without_gvisor()
        provider = DockerProvider(runtime="gvisor")
    root = scaffold_task(tmp_path / "engine")
    (root / "environment" / "Dockerfile").write_text(ENGINE.format(base=CONTAINER_BASE))
    prepared = await prepare_task_image(load_tasks(root)[0], provider)
    request = SandboxRequest(
        episode_id="engine-snapshot",
        prepared_image=prepared,
        retention="snapshot",
        resources=Resources(
            cpus=2, memory_mb=4096, container_features=(ContainerFeature.NESTED_CONTAINERS,)
        ),
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )
    sandbox = await provider.create(request)
    try:
        built = await sandbox.exec(
            ["sh", "-c", START_ENGINE + "; docker pull -q busybox:1.36 >/dev/null && "
             "docker run --name first busybox:1.36 rm /bin/ls && docker commit first deleted:1"],
            identity=Identity.FRAMEWORK, timeout_sec=600,
        )  # fmt: skip
        assert built.ok, built.stderr[-2000:]
        retained = await sandbox.snapshot(roles=("solver",), reason="engine")
    finally:
        await sandbox.destroy()
    try:
        async with await provider.create(
            request.model_copy(update={"snapshot": retained.handle, "retention": "destroy"})
        ) as booted:
            checked = await booted.exec(
                ["sh", "-c", START_ENGINE + "; "
                 "docker run --rm deleted:1 sh -c 'test ! -e /bin/ls && echo gone' && "
                 "docker run --name second deleted:1 rm /bin/cat && docker commit second deleted:2 "
                 ">/dev/null && docker run --rm deleted:2 sh -c 'test ! -e /bin/cat && echo gone'"],
                identity=Identity.FRAMEWORK, timeout_sec=600,
            )  # fmt: skip
            assert checked.ok and checked.stdout.split() == ["gone", "gone"], checked.stderr
    finally:
        await provider.destroy_retained(retained.handle)
