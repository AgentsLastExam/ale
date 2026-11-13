"""The VM backend's decisions that can be checked without booting one.

A guest image is a multi-gigabyte build, so the boot path is covered by the conformance
suite behind ``needs_kvm``. What belongs here is everything that must be right *before*
anything is provisioned — because a provider that fails late fails expensively.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support import FakeQemuImg, write_qcow2

from ale.core.errors import (
    GuestUnreachableError,
    ProviderCapabilityError,
    ProviderStartError,
    TaskDefinitionError,
)
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    Identity,
    ImageRef,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
)
from ale.core.taskspec import (
    ImageKind,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
)
from ale.run.framework import MATERIALIZER_IMAGE
from ale.run.gpu import parse_nvidia_smi
from ale.run.images import chain_identity
from ale.run.providers.qemu import (
    EPISODE_LABEL,
    GATEWAY_CHAIN,
    GPU_LABEL,
    GUEST_BRIDGE,
    HANDLE_PREFIX,
    HOST_IP,
    MANAGED_LABEL,
    RETENTION_LABEL,
    ROLE_LABEL,
    STORAGE_LABEL,
    VM_STORAGE_OVERHEAD_MB,
    QemuProvider,
    QemuSandbox,
    _index_vfio_gpus,
    _inspect_vfio_gpu,
    _port_of,
    _verify_qemu_gpus,
    _VfioGpu,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


def request(**overrides: object) -> SandboxRequest:
    digest = DIGEST
    base: dict[str, object] = {
        "episode_id": "e1",
        "prepared_image": PreparedTaskImage(
            kind="vm",
            source="external-ref",
            input_identity=digest,
            runtime_ref="/tmp/ale-ubuntu-desktop.qcow2",
            prepared_identity=digest,
            resolved_reference="local:///tmp/ale-ubuntu-desktop.qcow2",
        ),
        "resources": Resources(cpus=2, memory_mb=2048),
        "network": NetworkPolicy(),
        "gateway_url": "http://0.0.0.0:8931",
        "proxy_token": "episode-token",
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


class TestPreflight:
    def test_kvm_needs_no_access_of_this_accounts_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker's daemon opens /dev/kvm for the runner, so an account outside the kvm
        group, as on a cloud VM, runs VMs."""
        exists = Path.exists
        monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/dev/kvm" or exists(self))
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("os.access", lambda *_args, **_kwargs: False)
        asyncio.run(QemuProvider().preflight())

    def test_a_host_without_kvm_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exists = Path.exists
        monkeypatch.setattr(Path, "exists", lambda self: str(self) != "/dev/kvm" and exists(self))
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        with pytest.raises(ProviderCapabilityError, match="/dev/kvm is missing"):
            asyncio.run(QemuProvider().preflight())

    def test_a_missing_image_names_the_way_to_prepare_one(self, tmp_path: Path) -> None:
        provider = QemuProvider(image=tmp_path / "absent.qcow2")
        with pytest.raises(ProviderCapabilityError, match=r"ale prepare"):
            asyncio.run(provider.preflight())

    def test_problems_are_reported_together(self, tmp_path: Path) -> None:
        """One run should surface every reason, not the first one alphabetically."""
        provider = QemuProvider(image=tmp_path / "absent.qcow2")
        try:
            asyncio.run(provider.preflight())
        except ProviderCapabilityError as error:
            assert "guest image" in str(error)


def test_overlays_and_snapshots_live_in_the_ale_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator places VM storage, on a fast disk say, with ``ALE_CACHE_DIR`` alone."""
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "cache"))
    provider = QemuProvider()
    assert provider.overlay_dir == tmp_path / "cache" / "qemu"
    assert provider.snapshot_dir == tmp_path / "cache" / "qemu-snapshots"


def test_cancelled_boot_removes_the_started_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        return (0, "container-id\n", "") if argv[:2] == ("docker", "run") else (0, "", "")

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    monkeypatch.setattr("ale.run.providers.qemu.asyncio.sleep", cancel_sleep)
    base = write_qcow2(tmp_path / "base.qcow2")
    task = write_qcow2(tmp_path / "task.qcow2", backing="base.qcow2")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(QemuProvider()._boot(request(retention="keep"), tmp_path, task, 7411))

    assert commands[-1][:3] == ("docker", "rm", "-f")
    assert commands[-1][3].startswith("ale-qemu-")
    started = commands[0]
    for label in (
        f"{MANAGED_LABEL}=true",
        f"{EPISODE_LABEL}=e1",
        f"{ROLE_LABEL}=solver",
        f"{RETENTION_LABEL}=keep",
        f"{STORAGE_LABEL}={tmp_path.resolve()}",
    ):
        assert label in started
    # Every layer at its Host path, where the chain's relative names resolve, read-only.
    for layer in (task, base):
        assert f"type=bind,src={layer},dst={layer},readonly" in started


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operating_system", "sudo", "marker"),
    [("windows", True, True), ("windows", False, False), ("linux", True, False)],
)
async def test_windows_admin_boot_is_explicit_and_uses_the_resolved_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operating_system: str,
    sudo: bool,
    marker: bool,
) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        return 0, "true\n", ""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    monkeypatch.setattr("ale.run.providers.qemu.asyncio.sleep", no_sleep)
    runner = "sha256:" + "a" * 64
    await QemuProvider(runner_image=runner)._boot(
        request(os=operating_system, sudo=sudo), tmp_path, write_qcow2(tmp_path / "b.qcow2"), 7411
    )
    assert commands[0][-1] == runner
    assert "VM_NET_IP=172.30.0.2" in commands[0]
    arguments = next(item for item in commands[0] if item.startswith("ARGUMENTS="))
    assert ("-smbios type=1,serial=ale-sudo" in arguments) is marker


@pytest.mark.asyncio
@pytest.mark.parametrize("operating_system", ["linux", "windows"])
async def test_the_display_announces_the_requested_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operating_system: str
) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        return 0, "true\n", ""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    monkeypatch.setattr("ale.run.providers.qemu.asyncio.sleep", no_sleep)
    await QemuProvider()._boot(
        request(os=operating_system, desktop_resolution="1366x768"),
        tmp_path,
        write_qcow2(tmp_path / "base.qcow2"),
        7411,
    )
    assert "VGA=std" in commands[0]
    arguments = next(item for item in commands[0] if item.startswith("ARGUMENTS="))
    assert "-global VGA.xres=1366 -global VGA.yres=768" in arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "shutdown", "wait"])
async def test_a_windows_build_keeps_its_overlay_as_a_layer_after_a_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None
) -> None:
    commands: list[tuple[str, ...]] = []

    class Client:
        closed = False

        async def exec(self, argv: list[str], **_kwargs: object) -> tuple[int, str, str, bool]:
            assert argv == ["shutdown.exe", "/s", "/t", "1"]
            return int(failure == "shutdown"), "", "shutdown result", False

        async def close(self) -> None:
            self.closed = True

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:2] == ("docker", "wait"):
            assert client.closed
            return 0, "137\n" if failure == "wait" else "0\n", ""
        if argv[:2] == ("docker", "run"):
            with overlay.open("ab") as discarded:
                discarded.write(b" discarded")
        return 0, "", ""

    client = Client()
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox._client = client  # type: ignore[assignment]
    sandbox._file_client = Client()  # type: ignore[assignment]
    sandbox.request = request(os="windows")
    sandbox.container = "ale-qemu-build"
    sandbox.storage = tmp_path / "storage"
    base = write_qcow2(tmp_path / "vm-builds/base.qcow2", data=b"windows")
    overlay = write_qcow2(sandbox.storage / "data.qcow2", backing=str(base), data=b"office")
    sandbox.resolved_image = ResolvedImage(
        kind="vm", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=str(base)
    )
    destination = base.parent / "task.qcow2"
    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    if failure:
        with pytest.raises(ProviderStartError, match="shut down"):
            await sandbox.save_image(destination)
        assert not any(command[:2] == ("docker", "run") for command in commands)
        assert overlay.exists() and not destination.exists()
        return

    identity = await sandbox.save_image(destination)
    assert sandbox._file_client.closed
    wait, discard = commands
    assert wait == ("docker", "wait", "ale-qemu-build")
    # The VM builder discards free clusters, with the overlay writable and its base not.
    assert discard[:2] == ("docker", "run") and MATERIALIZER_IMAGE in discard
    assert discard[-2:] == ("/ale/ntfs_discard.py", str(overlay.resolve()))
    assert f"type=bind,src={overlay.resolve()},dst={overlay.resolve()}" in discard
    assert f"type=bind,src={base.resolve()},dst={base.resolve()},readonly" in discard
    # Not flattened: the overlay itself, naming its base relative to itself.
    assert not overlay.exists()
    assert FakeQemuImg.backing(destination) == "base.qcow2"
    # The layer is hashed as the discard left it.
    assert destination.read_bytes().endswith(b"office discarded")
    assert identity == chain_identity(DIGEST, _digest(destination))
    assert json.loads(destination.with_suffix(".json").read_text())["parent"] == DIGEST


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_build_vm_frames_its_own_overlay_on_the_base_copy_before_it_boots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool
) -> None:
    base = write_qcow2(tmp_path / "vm-builds/base.qcow2", data=b"windows")
    framed: list[tuple[Path, object]] = []

    class Booted(Exception):
        pass

    async def resolve(image: PreparedTaskImage) -> ResolvedImage:
        return ResolvedImage(
            kind="vm", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref="/x"
        )

    async def copy(image: PreparedTaskImage) -> Path:
        return base

    async def frame(disk: Path, *, operating_system: object, user: str, builder: str) -> None:
        assert builder == MATERIALIZER_IMAGE and user == "user"
        framed.append((disk, FakeQemuImg.backing(disk)))
        if fails:
            raise TaskDefinitionError("the agent account 'user' does not exist")

    async def boot(
        _provider: QemuProvider, _request: object, _storage: Path, disk: Path, *_: object
    ) -> str:
        assert disk == base and framed
        raise Booted

    async def ready(_provider: QemuProvider) -> None:
        pass

    monkeypatch.setattr("ale.run.providers.qemu.resolve_prepared_vm_image", resolve)
    monkeypatch.setattr("ale.run.providers.qemu.base_layer", copy)
    monkeypatch.setattr("ale.run.providers.qemu.frame_disk", frame)
    monkeypatch.setattr("ale.run.providers.qemu._run", FakeQemuImg())
    monkeypatch.setattr(QemuProvider, "_boot", boot)
    monkeypatch.setattr(QemuProvider, "preflight", ready)
    with pytest.raises(TaskDefinitionError if fails else Booted):
        await QemuProvider(overlay_dir=tmp_path / "qemu").create_builder(
            request(os="windows", user="user")
        )
    # The VM's own overlay is framed, on the base copy, which is only read; a VM that
    # fails to frame or to boot leaves no overlay behind.
    ((overlay, backing),) = framed
    assert overlay.name == "data.qcow2" and backing == str(base.resolve())
    assert base.read_bytes().endswith(b"windows")
    assert not overlay.parent.exists()


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshotting(
    tmp_path: Path,
    commands: list[tuple[str, ...]],
    *,
    sandbox_id: str = "e1-abcdef",
    booted: ResolvedImage | None = None,
) -> QemuSandbox:
    class Client:
        async def exec(self, argv: list[str], **_kwargs: object) -> tuple[int, str, str, bool]:
            commands.append(tuple(argv))
            return 0, "", "", False

        async def close(self) -> None:
            pass

    base = tmp_path / "cache/vm-builds/base.qcow2"
    if not base.exists():
        write_qcow2(base, data=b"disk")
    booted = booted or ResolvedImage(
        kind="vm", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=str(base)
    )
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox._client = Client()  # type: ignore[assignment]
    sandbox._file_client = Client()  # type: ignore[assignment]
    sandbox.sandbox_id = sandbox_id
    sandbox.request = request(role="shared")
    sandbox.container = "ale-qemu-solver"
    sandbox.storage = tmp_path / sandbox_id
    write_qcow2(sandbox.storage / "data.qcow2", backing=booted.observed_ref, data=b"written")
    sandbox.snapshot_dir = tmp_path / "cache/qemu-snapshots"
    sandbox.resolved_image = booted
    return sandbox


def _snapshot_run(monkeypatch: pytest.MonkeyPatch, commands: list[tuple[str, ...]]) -> FakeQemuImg:
    qemu_img = FakeQemuImg()

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[0] == "qemu-img":
            return await qemu_img(*argv)
        return (0, "0\n", "") if argv[:2] == ("docker", "wait") else (0, "", "")

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.images._run", qemu_img)
    return qemu_img


@pytest.mark.asyncio
async def test_a_snapshot_is_the_overlay_kept_on_its_prepared_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    _snapshot_run(monkeypatch, commands)
    sandbox = _snapshotting(tmp_path, commands)
    retained = await sandbox.snapshot(roles=("solver", "verifier"), reason="kept")

    disk = tmp_path / "cache/qemu-snapshots/e1-abcdef/disk.qcow2"
    # Nothing is copied: a clean shutdown, then the overlay itself is kept.
    assert commands == [
        ("sync",),
        ("systemctl", "poweroff", "--no-block"),
        ("docker", "wait", "ale-qemu-solver"),
    ]
    assert FakeQemuImg.backing(disk) == "../../vm-builds/base.qcow2"
    assert disk.read_bytes().endswith(b"written")
    assert retained.handle == "qemu:snapshot:e1-abcdef"
    assert retained.cleanup_command == "ale sandbox destroy qemu:snapshot:e1-abcdef"
    record = json.loads((disk.parent / "snapshot.json").read_text())
    assert record == {
        "managed": True,
        "episode": "e1",
        "role": "shared",
        "os": "linux",
        "prepared_identity": DIGEST,
    }

    provider = QemuProvider(overlay_dir=tmp_path / "overlays", snapshot_dir=sandbox.snapshot_dir)
    prepared = sandbox.resolved_image
    booted = await provider._snapshot_image(retained.handle, request(), prepared)
    assert booted.observed_identity == chain_identity(DIGEST, _digest(disk))
    assert booted.observed_ref == str(disk.resolve())
    other = request(
        prepared_image=request().prepared_image.model_copy(
            update={"prepared_identity": "sha256:" + "1" * 64}
        )
    )
    with pytest.raises(ProviderCapabilityError, match="prepared solver image"):
        await provider._snapshot_image(retained.handle, other, prepared)
    with pytest.raises(ProviderCapabilityError, match="holds a linux guest"):
        await provider._snapshot_image(retained.handle, request(os="windows"), prepared)
    with pytest.raises(ProviderCapabilityError, match="not a QEMU snapshot handle"):
        await provider._snapshot_image("qemu:snapshot:../overlays", request(), prepared)
    # Only on the very disk it was taken on: not another, nor one made again since.
    for elsewhere in (
        prepared.model_copy(update={"observed_ref": str(tmp_path / "other.qcow2")}),
        prepared.model_copy(update={"observed_identity": "sha256:" + "2" * 64}),
    ):
        with pytest.raises(ProviderCapabilityError, match="cannot boot here"):
            await provider._snapshot_image(retained.handle, request(), elsewhere)


@pytest.mark.asyncio
async def test_a_snapshot_of_a_forked_sandbox_sits_on_the_prepared_disk_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    _snapshot_run(monkeypatch, commands)
    first = _snapshotting(tmp_path, commands)
    await first.snapshot(roles=("solver",), reason="kept")
    provider = QemuProvider(overlay_dir=tmp_path / "overlays", snapshot_dir=first.snapshot_dir)
    booted = await provider._snapshot_image(
        "qemu:snapshot:e1-abcdef", request(), first.resolved_image
    )
    fork = _snapshotting(tmp_path, commands, sandbox_id="e2-123456", booted=booted)
    commands.clear()
    await fork.snapshot(roles=("solver",), reason="kept")

    # The earlier snapshot's clusters are taken into the overlay, which then sits on the
    # prepared disk, so destroying the earlier snapshot never breaks this one.
    (rebase,) = [command for command in commands if command[:2] == ("qemu-img", "rebase")]
    assert "-u" not in rebase and rebase[-1] == str(fork.storage / "data.qcow2")
    assert Path(rebase[rebase.index("-b") + 1]).resolve() == Path(first.resolved_image.observed_ref)
    disk = tmp_path / "cache/qemu-snapshots/e2-123456/disk.qcow2"
    assert FakeQemuImg.backing(disk) == "../../vm-builds/base.qcow2"
    assert json.loads(disk.with_suffix(".json").read_text())["parent"] == DIGEST


@pytest.mark.asyncio
async def test_a_failed_snapshot_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    qemu_img = _snapshot_run(monkeypatch, commands)

    async def full(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        if argv[1] == "rebase":
            return 1, "", "no space left"
        return await qemu_img(*argv)

    monkeypatch.setattr("ale.run.images._run", full)
    sandbox = _snapshotting(tmp_path, commands)
    with pytest.raises(ProviderStartError, match="no space left"):
        await sandbox.snapshot(roles=("solver",), reason="kept")
    assert not any((tmp_path / "cache/qemu-snapshots").iterdir())


@pytest.mark.asyncio
async def test_a_snapshot_is_listed_and_not_destroyed_while_a_vm_runs_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    (snapshots / "e1-abcdef").mkdir(parents=True)
    disk = snapshots / "e1-abcdef/disk.qcow2"
    disk.write_bytes(b"disk")
    (snapshots / "e1-abcdef/disk.json").write_text(json.dumps({"identity": DIGEST}))
    (snapshots / "e1-abcdef/snapshot.json").write_text(
        json.dumps({"managed": True, "episode": "e1", "role": "solver"})
    )
    (snapshots / "unrelated").mkdir()
    mounts: list[dict[str, str]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "ps", "-a"):
            return 0, ("runner\n" if "label=ale.qemu.managed=true" in argv[-1] else ""), ""
        assert argv[:2] == ("docker", "inspect")
        return 0, json.dumps([{"Name": "/ale-qemu-fork", "Mounts": mounts}]), ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    provider = QemuProvider(overlay_dir=tmp_path / "overlays", snapshot_dir=snapshots)
    assert provider._list_snapshots() == [
        {
            "provider": "qemu",
            "handle": "qemu:snapshot:e1-abcdef",
            "episode": "e1",
            "role": "solver",
            "image": DIGEST,
            "gpus": (),
            "running": False,
            "cleanup_command": "ale sandbox destroy qemu:snapshot:e1-abcdef",
        }
    ]
    # A runner mounts every layer of its chain at its Host path.
    layer = str(disk.resolve())
    mounts.append({"Type": "bind", "Source": layer, "Destination": layer})
    with pytest.raises(ProviderStartError, match="qemu:ale-qemu-fork runs on it"):
        await provider.destroy_retained("qemu:snapshot:e1-abcdef")
    mounts.clear()
    await provider.destroy_retained("qemu:snapshot:e1-abcdef")
    assert not (snapshots / "e1-abcdef").exists()
    with pytest.raises(ProviderStartError, match="does not exist"):
        await provider.destroy_retained("qemu:snapshot:e1-abcdef")


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["ale.run.providers.qemu", "ale.run.task_images"])
async def test_cancelled_image_command_reaps_its_subprocess(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    module = importlib.import_module(module_name)
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()

    async def tracked(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original(*argv, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    command = asyncio.create_task(module._run(sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert processes[0].returncode is not None
    finally:
        command.cancel()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_qemu_retain_returns_a_provider_qualified_handle() -> None:
    class Client:
        closed = False

        async def close(self) -> None:
            self.closed = True

    client = Client()
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox._client = client  # type: ignore[attr-defined]
    sandbox._file_client = Client()  # type: ignore[assignment]
    sandbox.container = "ale-qemu-test"  # type: ignore[attr-defined]
    sandbox.request = request(retention="keep")  # type: ignore[attr-defined]
    sandbox.allocation = SimpleNamespace(gpu=None)  # type: ignore[attr-defined]

    retained = await sandbox.retain(roles=("solver",), reason="debug")

    assert client.closed
    assert sandbox._file_client.closed
    assert retained.provider == "qemu"
    assert retained.handle == f"{HANDLE_PREFIX}ale-qemu-test"
    assert retained.cleanup_command == "ale sandbox destroy qemu:ale-qemu-test"


@pytest.mark.asyncio
async def test_qemu_retained_listing_and_destroy_remove_the_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = tmp_path / "episode"
    storage.mkdir()
    (storage / "data.qcow2").touch()
    labels = {
        MANAGED_LABEL: "true",
        RETENTION_LABEL: "keep",
        EPISODE_LABEL: "episode",
        ROLE_LABEL: "solver",
        GPU_LABEL: "0000:65:00.0",
        STORAGE_LABEL: str(storage),
    }
    record = {
        "Name": "/ale-qemu-test",
        "Image": "sha256:runner",
        "Config": {"Labels": labels},
        "State": {"Running": True},
        "Mounts": [{"Type": "bind", "Source": str(storage), "Destination": "/storage"}],
    }
    removed: list[str] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        if argv[:4] == ("docker", "ps", "-a", "-q"):
            return 0, "container-id\n", ""
        if argv[:2] == ("docker", "inspect"):
            return 0, json.dumps([record]), ""
        if argv[:3] == ("docker", "rm", "-f"):
            removed.append(argv[3])
            return 0, "", ""
        raise AssertionError(argv)

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)

    assert await QemuProvider().list_retained() == [
        {
            "provider": "qemu",
            "handle": "qemu:ale-qemu-test",
            "episode": "episode",
            "role": "solver",
            "image": "sha256:runner",
            "gpus": ("0000:65:00.0",),
            "running": True,
            "cleanup_command": "ale sandbox destroy qemu:ale-qemu-test",
        }
    ]
    await QemuProvider().destroy_retained("qemu:ale-qemu-test")
    assert removed == ["ale-qemu-test"]
    assert not storage.exists()


@pytest.mark.asyncio
async def test_qemu_retained_solver_can_be_reattached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = tmp_path / "episode"
    storage.mkdir()
    (storage / "guestd-token").write_text("retained-token\n")
    below = write_qcow2(tmp_path / "below.qcow2")
    base = write_qcow2(tmp_path / "base.qcow2", backing="below.qcow2")
    write_qcow2(storage / "data.qcow2", backing=str(base))
    monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
    record = {
        "Name": "/ale-qemu-test",
        "Config": {
            "Labels": {
                MANAGED_LABEL: "true",
                RETENTION_LABEL: "keep",
                EPISODE_LABEL: "e1",
                ROLE_LABEL: "solver",
                STORAGE_LABEL: str(storage),
            }
        },
        "State": {"Running": True},
        "Mounts": [
            {"Type": "bind", "Source": str(storage), "Destination": "/storage"},
            {"Type": "bind", "Source": str(base), "Destination": str(base)},
        ],
        "NetworkSettings": {"Ports": {"7411/tcp": [{"HostPort": "17411"}]}},
    }
    rules: list[str] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        if argv[:2] == ("docker", "exec"):
            if "ip route" in argv[-1]:
                return 0, "172.17.0.1\n", ""
            rules.append(argv[-1])
            return 0, "", ""
        assert argv[:2] == ("docker", "inspect")
        return 0, json.dumps([record]), ""

    class Transport:
        def __init__(self, host: str, port: int, *, token: str) -> None:
            assert (host, port, token) == ("127.0.0.1", 17411, "retained-token")

        async def start(self, *, timeout_sec: float) -> None:
            assert timeout_sec > 0

    class Client:
        def __init__(self, _transport: object) -> None:
            pass

        async def close(self) -> None:
            pass

    async def contract(
        _provider: QemuProvider, _client: object, _request: SandboxRequest
    ) -> tuple[str, str, bool]:
        return "user", "/home/user", False

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.providers.qemu.TcpTransport", Transport)
    monkeypatch.setattr("ale.run.providers.qemu.GuestClient", Client)
    monkeypatch.setattr(QemuProvider, "_read_guest_contract", contract)
    allocation = ResourceAllocation(
        cpus=2,
        memory_mb=2048,
        sudo=False,
        network_mode=NetworkMode.BLOCK,
        provider="qemu",
    )
    image = ResolvedImage(
        kind="vm",
        prepared_identity=DIGEST,
        observed_identity=DIGEST,
        observed_ref=str(base),
    )

    sandbox = await QemuProvider().attach_retained(
        "qemu:ale-qemu-test", request(), allocation, image
    )

    assert sandbox.storage == storage
    assert sandbox.host_port == 17411
    assert sandbox.host_ip == "172.17.0.1"
    assert sandbox.request.role.value == "solver"
    assert sandbox.resolved_image == image
    # The reattaching run's own Gateway is routed in at the same guest address.
    assert f"iptables -t nat -F {GATEWAY_CHAIN}" in rules[0]
    assert f"--dport {SANDBOX_GATEWAY_PORT} -j DNAT --to-destination 172.17.0.1:8931" in rules[2]

    # Another disk, or a layer below the one the runner's overlay sits on, is refused.
    for elsewhere in (tmp_path / "other.qcow2", below):
        other = image.model_copy(update={"observed_ref": str(elsewhere)})
        with pytest.raises(ProviderCapabilityError, match="expected image"):
            await QemuProvider().attach_retained("qemu:ale-qemu-test", request(), allocation, other)


class _LeftRunning:
    """A runner a crashed node agent left: a verifier VM it created with retention destroy,
    behind a scripted docker CLI."""

    handle = "qemu:ale-qemu-test"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.storage = tmp_path / "episode"
        self.storage.mkdir()
        (self.storage / "guestd-token").write_text("token\n")
        base = write_qcow2(tmp_path / "base.qcow2")
        write_qcow2(self.storage / "data.qcow2", backing=str(base))
        monkeypatch.setattr("ale.run.images._run", FakeQemuImg())
        self.labels = {
            MANAGED_LABEL: "true",
            RETENTION_LABEL: "destroy",
            EPISODE_LABEL: "e1",
            ROLE_LABEL: "verifier",
            STORAGE_LABEL: str(self.storage),
        }
        self.record: dict[str, Any] = {
            "Name": "/ale-qemu-test",
            "Config": {"Labels": self.labels},
            "State": {"Running": True},
            "Mounts": [{"Type": "bind", "Source": str(self.storage), "Destination": "/storage"}],
            "NetworkSettings": {"Ports": {"7411/tcp": [{"HostPort": "17411"}]}},
        }
        self.removed: list[str] = []
        self.rules: list[str] = []
        #: Whether the runner's FORWARD chain holds the seal's drop rule.
        self.sealed = False
        #: What every ``docker inspect`` fails with, if anything does.
        self.failure: str | None = None
        self.client = _ExecClient()

        async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
            if argv[:2] == ("docker", "exec"):
                if "ip route" in argv[-1]:
                    return 0, "172.17.0.1\n", ""
                self.rules.append(argv[-1])
                if argv[-1] == f"iptables -C FORWARD -i {GUEST_BRIDGE} -j DROP":
                    return (0, "", "") if self.sealed else (1, "", "iptables: Bad rule")
                return 0, "", ""
            if argv[:3] == ("docker", "rm", "-f"):
                self.removed.append(argv[3])
                return 0, "", ""
            assert argv[:2] == ("docker", "inspect")
            if self.failure is not None:
                return 1, "", self.failure
            if self.removed:
                return 1, "", "Error: No such object: ale-qemu-test"
            return 0, json.dumps([self.record]), ""

        class Transport:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def start(self, *, timeout_sec: float) -> None:
                pass

        async def contract(
            _provider: QemuProvider, _client: object, _request: SandboxRequest
        ) -> tuple[str, str, bool]:
            return "user", "/home/user", False

        monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
        monkeypatch.setattr("ale.run.providers.qemu.TcpTransport", Transport)
        monkeypatch.setattr("ale.run.providers.qemu.GuestClient", lambda _: self.client)
        monkeypatch.setattr(QemuProvider, "_read_guest_contract", contract)
        self.allocation = ResourceAllocation(
            cpus=2, memory_mb=2048, sudo=False, network_mode=NetworkMode.BLOCK, provider="qemu"
        )
        self.image = ResolvedImage(
            kind="vm", prepared_identity=DIGEST, observed_identity=DIGEST, observed_ref=str(base)
        )


@pytest.mark.asyncio
async def test_a_vm_a_crashed_process_left_is_reattached_or_discarded_whatever_its_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    left = _LeftRunning(monkeypatch, tmp_path)
    handle, allocation, image = left.handle, left.allocation, left.image
    with pytest.raises(ProviderCapabilityError, match="not a retained ALE solver"):
        await QemuProvider().attach_retained(handle, request(), allocation, image)
    sandbox = await QemuProvider().reattach(handle, request(), allocation, image)
    assert sandbox.handle == handle
    assert sandbox.request.role.value == "verifier"

    await QemuProvider().discard(handle)
    assert left.removed == ["ale-qemu-test"] and not left.storage.exists()
    await QemuProvider().discard(handle)  # gone already: nothing to do
    left.labels[MANAGED_LABEL] = "false"
    left.removed.clear()
    with pytest.raises(ProviderCapabilityError, match="not an ALE QEMU sandbox"):
        await QemuProvider().reattach(handle, request(), allocation, image)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "drop", "sealed"),
    [
        (NetworkMode.ALLOWLIST, True, True),
        (NetworkMode.ALLOWLIST, False, False),
        (NetworkMode.OPEN, True, False),
    ],
)
async def test_a_reattached_vm_is_sealed_as_its_runner_was_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: NetworkMode, drop: bool, sealed: bool
) -> None:
    """An allowlist agent's commands get the proxy only while the VM is sealed, which the
    runner's firewall says, not the process driving it again."""
    left = _LeftRunning(monkeypatch, tmp_path)
    left.sealed = drop
    network = (
        NetworkPolicy(mode=mode, allowed_hosts=("example.com",))
        if mode is NetworkMode.ALLOWLIST
        else NetworkPolicy(mode=mode)
    )
    wanted = request(network=network, proxy_url="http://0.0.0.0:9443")
    sandbox = await QemuProvider().reattach(left.handle, wanted, left.allocation, left.image)
    await sandbox.exec(["true"], identity=Identity.AGENT)
    assert (left.client.env is not None) is sealed
    checked = f"iptables -C FORWARD -i {GUEST_BRIDGE} -j DROP" in left.rules
    assert checked is (mode is not NetworkMode.OPEN)


@pytest.mark.asyncio
async def test_discard_fails_when_docker_cannot_say_the_runner_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    left = _LeftRunning(monkeypatch, tmp_path)
    left.failure = "Cannot connect to the Docker daemon"
    with pytest.raises(ProviderStartError, match="Cannot connect to the Docker daemon"):
        await QemuProvider().discard(left.handle)
    assert left.storage.exists()


def test_overlay_uses_the_prepared_disks_virtual_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:2] == ("qemu-img", "info"):
            return 0, '{"virtual-size":42949672960}', ""
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    asyncio.run(QemuProvider()._make_overlay(tmp_path / "overlay.qcow2", tmp_path / "base.qcow2"))
    assert commands[-1][-1] == "42949672960"


def test_overlay_size_is_driven_by_requested_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        commands.append(argv)
        if argv[:2] == ("qemu-img", "info"):
            return 0, '{"virtual-size":7516192768}', ""
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    asyncio.run(
        QemuProvider()._make_overlay(
            tmp_path / "overlay.qcow2",
            tmp_path / "base.qcow2",
            requested_storage_mb=8192,
        )
    )
    assert commands[-1][-1] == str((8192 + VM_STORAGE_OVERHEAD_MB) * 1024 * 1024)


def test_vm_storage_uses_observed_root_filesystem_capacity() -> None:
    class Client:
        async def disk_usage(self, path: str) -> tuple[int, int, int]:
            assert path == "/"
            return 40123 * 1024 * 1024, 1, 1

    assert (
        asyncio.run(QemuProvider()._root_capacity_mb(Client(), OperatingSystem.LINUX))  # type: ignore[arg-type]
        == 40123
    )


def test_invalid_vm_root_capacity_is_rejected() -> None:
    class Client:
        async def disk_usage(self, _path: str) -> tuple[int, int, int]:
            raise RuntimeError("disk probe failed")

    with pytest.raises(ProviderStartError, match="disk probe failed"):
        asyncio.run(QemuProvider()._root_capacity_mb(Client(), OperatingSystem.LINUX))  # type: ignore[arg-type]


def test_vm_root_storage_expands_to_the_request() -> None:
    class Client:
        def __init__(self) -> None:
            self.capacities = iter((6433, 8677))
            self.commands: list[tuple[str, ...]] = []

        async def disk_usage(self, path: str) -> tuple[int, int, int]:
            assert path == "/"
            capacity = next(self.capacities)
            return capacity * 1024 * 1024, 1, 1

        async def exec(self, argv: list[str], **_kwargs: object) -> tuple[int, str, str, bool]:
            self.commands.append(tuple(argv))
            return 0, "", "", False

    client = Client()
    assert (
        asyncio.run(
            QemuProvider()._prepare_root_storage(client, 8192, OperatingSystem.LINUX)  # type: ignore[arg-type]
        )
        == 8677
    )
    assert client.commands == [
        ("systemd-repart", "--dry-run=no"),
        ("/usr/lib/systemd/systemd-growfs", "/"),
    ]


def test_desktop_readiness_retries_a_transient_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def screenshot(self) -> bytes:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            return b"non-flat-png"

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("ale.run.providers.qemu.asyncio.sleep", no_sleep)
    client = Client()
    asyncio.run(QemuProvider()._await_desktop(client, timeout_sec=1))  # type: ignore[arg-type]
    assert client.calls == 2


def test_windows_guest_contract_is_read_from_guestd_health() -> None:
    class Client:
        async def health(self) -> dict[str, object]:
            return {
                "os": "windows",
                "agent_user": "user",
                "agent_home": r"C:\Users\user",
                "gui": True,
            }

        async def exists(self, path: str) -> bool:
            return path == r"C:\Users\user"

    observed = asyncio.run(
        QemuProvider()._read_guest_contract(  # type: ignore[arg-type]
            Client(), request(os=OperatingSystem.WINDOWS)
        )
    )
    assert observed == ("user", r"C:\Users\user", True)


def test_windows_guest_account_comparison_is_case_insensitive() -> None:
    class Client:
        def __init__(self, user: str) -> None:
            self.user = user

        async def health(self) -> dict[str, object]:
            return {
                "os": "windows",
                "agent_user": self.user,
                "agent_home": r"C:\Users\User",
                "gui": True,
            }

        async def exists(self, path: str) -> bool:
            return path == r"C:\Users\User"

    observed = asyncio.run(
        QemuProvider()._read_guest_contract(  # type: ignore[arg-type]
            Client("User"), request(os=OperatingSystem.WINDOWS)
        )
    )
    assert observed == ("User", r"C:\Users\User", True)
    with pytest.raises(ProviderCapabilityError, match="prepared for agent account 'other'"):
        asyncio.run(
            QemuProvider()._read_guest_contract(  # type: ignore[arg-type]
                Client("other"), request(os=OperatingSystem.WINDOWS)
            )
        )


def test_a_linux_disk_must_be_prepared_for_the_tasks_agent_account() -> None:
    class Client:
        def __init__(self, user: str) -> None:
            self.user = user

        async def health(self) -> dict[str, object]:
            return {"os": "linux", "gui": True, "agent_user": self.user, "agent_home": "/home/u"}

        async def exists(self, path: str) -> bool:
            return path == "/home/u"

    contract = asyncio.run(
        QemuProvider()._read_guest_contract(Client("user"), request())  # type: ignore[arg-type]
    )
    assert contract == ("user", "/home/u", True)
    with pytest.raises(ProviderCapabilityError, match="prepared for agent account 'app'"):
        asyncio.run(QemuProvider()._read_guest_contract(Client("app"), request()))  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match="prepared for agent account 'User'"):
        asyncio.run(QemuProvider()._read_guest_contract(Client("User"), request()))  # type: ignore[arg-type]


def test_windows_health_must_declare_its_agent_identity() -> None:
    class Client:
        async def health(self) -> dict[str, object]:
            return {"os": "windows", "gui": True}

        async def exists(self, path: str) -> bool:
            return False

    with pytest.raises(ProviderCapabilityError, match="agent account"):
        asyncio.run(
            QemuProvider()._read_guest_contract(  # type: ignore[arg-type]
                Client(), request(os=OperatingSystem.WINDOWS)
            )
        )


@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize(
    "framework_admin,agent_admin", [(True, False), (True, True), (False, False)]
)
async def test_windows_privileges_must_match_framework_and_agent_roles(
    sudo: bool, framework_admin: bool, agent_admin: bool
) -> None:
    class Client:
        async def exec(self, argv, *, run_as, timeout_sec):
            elevated = framework_admin if run_as is None else agent_admin
            return 0, f"{int(elevated)}\r\n", "", False

    check = QemuProvider()._verify_windows_privileges(Client(), "user", sudo)
    if framework_admin and agent_admin == sudo:
        await check
    else:
        with pytest.raises(ProviderCapabilityError, match=r"Windows .* token"):
            await check


@pytest.mark.asyncio
async def test_prepared_vm_is_checked_without_runtime_reference_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = request().prepared_image
    seen: list[PreparedTaskImage] = []

    async def check(image: PreparedTaskImage) -> ResolvedImage:
        seen.append(image)
        return ResolvedImage(
            kind="vm",
            prepared_identity=image.prepared_identity,
            observed_identity=image.prepared_identity,
            observed_ref=image.runtime_ref,
        )

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a prepared VM must not be pulled again")

    monkeypatch.setattr("ale.run.providers.qemu.resolve_prepared_vm_image", check)
    monkeypatch.setattr("ale.run.providers.qemu.resolve_vm_image", forbidden)
    assert await QemuProvider().prepare_image(prepared) is prepared
    assert seen == [prepared]


@pytest.mark.asyncio
async def test_vm_reference_uses_provider_acquisition_and_wrong_kind_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = request().prepared_image

    async def acquire(image: ImageRef, **_kwargs: object) -> PreparedTaskImage:
        assert image == ImageRef(kind="vm", reference="ghcr.io/acme/vm:v1")
        return prepared

    async def check(image: PreparedTaskImage) -> ResolvedImage:
        return ResolvedImage(
            kind="vm",
            prepared_identity=image.prepared_identity,
            observed_identity=image.prepared_identity,
            observed_ref=image.runtime_ref,
        )

    monkeypatch.setattr("ale.run.providers.qemu.resolve_vm_image", acquire)
    monkeypatch.setattr("ale.run.providers.qemu.resolve_prepared_vm_image", check)
    assert (
        await QemuProvider().prepare_image(ImageRef(kind="vm", reference="ghcr.io/acme/vm:v1"))
        == prepared
    )
    with pytest.raises(ProviderCapabilityError, match="requires a VM image"):
        await QemuProvider().prepare_image(
            ImageRef(kind=ImageKind.CONTAINER, reference="ghcr.io/acme/container:v1")
        )


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_macos_is_rejected_before_vm_creation(self) -> None:
        with pytest.raises(ProviderCapabilityError, match="macos unsupported"):
            await QemuProvider().create(request(os="macos"))

    def test_allowlist_is_offered(self) -> None:
        provider = QemuProvider()
        assert NetworkMode.ALLOWLIST in provider.capabilities().network_modes
        provider.accepts(
            request(
                network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))
            )
        )

    def test_block_and_open_are_offered(self) -> None:
        modes = QemuProvider().capabilities().network_modes
        assert {NetworkMode.BLOCK, NetworkMode.OPEN} <= modes

    def test_a_desktop_is_not_something_admission_asks_about(self) -> None:
        """Whether a screen exists belongs to the image, and is answered by asking for one.

        Nothing in the request describes it, so nothing here can refuse on it. An agent
        that asks a screenless sandbox for a screenshot is told so by the screenshot.
        """
        QemuProvider().accepts(request())


class TestGatewayAddressing:
    def test_the_host_is_rewritten_to_the_runner_address(self) -> None:
        """A host bind address is meaningless inside the guest.

        What the guest can reach is the runner holding it, which forwards the fixed port to
        the one the Gateway is on — so that is the address the agent's SDK must be handed.
        """
        sandbox = QemuSandbox.__new__(QemuSandbox)
        sandbox.request = request(gateway_url="http://0.0.0.0:8931")  # type: ignore[attr-defined]
        assert sandbox.gateway_url == f"http://{HOST_IP}:{SANDBOX_GATEWAY_PORT}"

    def test_no_gateway_stays_absent(self) -> None:
        sandbox = QemuSandbox.__new__(QemuSandbox)
        sandbox.request = request(gateway_url="")  # type: ignore[attr-defined]
        assert sandbox.gateway_url is None


class TestGatewayPort:
    """The port the runner forwards is parsed from the URL, not configured twice."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://127.0.0.1:8931", "8931"),
            ("http://127.0.0.1:8931/v1", "8931"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_ports_are_read_from_the_url(self, url: str | None, expected: str) -> None:
        assert _port_of(url) == expected


class TestVfioGpu:
    def test_validates_a_prebound_nvidia_device(self, tmp_path: Path) -> None:
        sysfs = tmp_path / "sys"
        device = sysfs / "bus/pci/devices/0000:65:00.0"
        group = sysfs / "kernel/iommu_groups/17"
        member = group / "devices/0000:65:00.0"
        device.mkdir(parents=True)
        member.mkdir(parents=True)
        (device / "vendor").write_text("0x10de\n")
        (device / "class").write_text("0x030000\n")
        (device / "iommu_group").symlink_to(group)
        (device / "driver").symlink_to(sysfs / "bus/pci/drivers/vfio-pci")
        vfio = tmp_path / "dev/vfio"
        vfio.mkdir(parents=True)
        (vfio / "vfio").touch()
        (vfio / "17").touch()

        gpu = _inspect_vfio_gpu("0000:65:00.0", sysfs=sysfs, dev=tmp_path / "dev")
        assert gpu.bdf == "0000:65:00.0"
        assert gpu.group == "17"

    @pytest.mark.parametrize(
        ("vendor", "klass", "driver", "message"),
        [
            ("0x1234", "0x030000", "vfio-pci", "NVIDIA"),
            ("0x10de", "0x020000", "vfio-pci", "GPU"),
            ("0x10de", "0x030000", "nvidia", "vfio-pci"),
        ],
    )
    def test_rejects_unsafe_devices(
        self,
        tmp_path: Path,
        vendor: str,
        klass: str,
        driver: str,
        message: str,
    ) -> None:
        sysfs = tmp_path / "sys"
        device = sysfs / "bus/pci/devices/0000:65:00.0"
        group = sysfs / "kernel/iommu_groups/17"
        (group / "devices/0000:65:00.0").mkdir(parents=True)
        device.mkdir(parents=True, exist_ok=True)
        (device / "vendor").write_text(vendor)
        (device / "class").write_text(klass)
        (device / "iommu_group").symlink_to(group)
        (device / "driver").symlink_to(sysfs / f"bus/pci/drivers/{driver}")
        with pytest.raises(ProviderCapabilityError, match=message):
            _inspect_vfio_gpu("0000:65:00.0", sysfs=sysfs, dev=tmp_path / "dev")

    def test_rejects_two_gpu_candidates_in_one_iommu_group(self) -> None:
        with pytest.raises(ProviderCapabilityError, match="share IOMMU group 17"):
            _index_vfio_gpus(
                (
                    _VfioGpu("0000:65:00.0", "17"),
                    _VfioGpu("0000:66:00.0", "17"),
                )
            )

    def test_guest_must_observe_the_requested_count_and_models(self) -> None:
        (h100,) = parse_nvidia_smi(
            "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa, NVIDIA H100 80GB HBM3, "
            "00000000:65:00.0, 580.1\n"
        )
        with pytest.raises(ProviderCapabilityError, match="count mismatch"):
            _verify_qemu_gpus(Resources(gpus=1), ())
        assert _verify_qemu_gpus(Resources(gpus=1), (h100,)) == (h100,)
        assert _verify_qemu_gpus(Resources(gpus=1, gpu_types=("H100",)), (h100,)) == (h100,)
        with pytest.raises(ProviderCapabilityError, match=r"gave NVIDIA H100 80GB HBM3"):
            _verify_qemu_gpus(Resources(gpus=1, gpu_types=("RTX-PRO-6000",)), (h100,))


class _ExecClient:
    def __init__(self) -> None:
        self.env: dict[str, str] | None = None
        self.argv: object = None
        self.calls: list[object] = []

    async def exec(self, argv: object, **kwargs: Any) -> tuple[int, str, str, bool]:
        self.argv = argv
        self.env = kwargs.get("env")
        self.calls.append(argv)
        return 0, "", "", False

    async def system_proxy(self, proxy: str | None, *, bypass: object = ()) -> None:
        self.calls.append(("system_proxy", proxy, bypass))

    async def close(self) -> None:
        pass


def test_open_mode_uses_only_the_runner_firewall(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*_argv: str, **_kwargs: object) -> tuple[int, str, str]:
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox.request = request(network=NetworkPolicy(mode=NetworkMode.OPEN))  # type: ignore[attr-defined]
    sandbox.container = "runner"  # type: ignore[attr-defined]
    sandbox.host_ip = "172.17.0.1"  # type: ignore[attr-defined]
    sandbox._client = _ExecClient()  # type: ignore[attr-defined]
    sandbox._sealed = True  # type: ignore[attr-defined]

    asyncio.run(sandbox.open_egress())

    assert sandbox._client.argv is None  # type: ignore[attr-defined]
    assert sandbox._sealed is False  # type: ignore[attr-defined]


def test_allowlist_proxy_is_injected_only_for_sealed_agent_commands() -> None:
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox.request = request(  # type: ignore[attr-defined]
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9443",
    )
    sandbox.agent_user = "user"  # type: ignore[attr-defined]
    sandbox._client = _ExecClient()  # type: ignore[attr-defined]
    sandbox._sealed = False  # type: ignore[attr-defined]

    asyncio.run(sandbox.exec(["true"], identity=Identity.AGENT))
    assert sandbox._client.env is None  # type: ignore[attr-defined]

    sandbox._sealed = True  # type: ignore[attr-defined]
    asyncio.run(sandbox.exec(["true"], identity=Identity.AGENT))
    assert sandbox._client.env == {  # type: ignore[attr-defined]
        "HTTP_PROXY": "http://ale:episode-token@172.30.0.1:9443",
        "HTTPS_PROXY": "http://ale:episode-token@172.30.0.1:9443",
        "http_proxy": "http://ale:episode-token@172.30.0.1:9443",
        "https_proxy": "http://ale:episode-token@172.30.0.1:9443",
        "NO_PROXY": "172.30.0.1,localhost,127.0.0.1",
    }


def test_allowlist_forwards_gateway_and_proxy_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        command = argv[-1]
        if "ip route" in command:
            return 0, "172.17.0.1\n", ""
        commands.append(command)
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    provider = QemuProvider()
    host = asyncio.run(
        provider._wire_network(
            "runner",
            request(
                network=NetworkPolicy(
                    mode=NetworkMode.ALLOWLIST,
                    allowed_hosts=("example.com",),
                ),
                proxy_url="http://0.0.0.0:9443",
            ),
        )
    )
    assert host == "172.17.0.1"
    assert any("--dport 8931" in command for command in commands)
    assert any("--dport 9443" in command for command in commands)


def test_allowlist_direct_bypass_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    rules: list[str] = []

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        rules.append(argv[-1])
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox.request = request(  # type: ignore[attr-defined]
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))
    )
    sandbox.container = "runner"  # type: ignore[attr-defined]
    sandbox.host_ip = "172.17.0.1"  # type: ignore[attr-defined]
    sandbox._client = _ExecClient()  # type: ignore[attr-defined]
    sandbox._sealed = False  # type: ignore[attr-defined]

    asyncio.run(sandbox.close_egress())
    assert "iptables -I FORWARD 1 -i docker -j DROP" in rules
    accepted = [rule for rule in rules if "FORWARD" in rule and rule.endswith("-j ACCEPT")]
    assert any("ESTABLISHED,RELATED --ctdir REPLY" in rule for rule in accepted)
    # The host is reachable only on the episode's own ports, never on every port.
    assert all("ESTABLISHED" in rule or "--dports" in rule for rule in accepted)
    assert not any("! -d" in rule for rule in rules)
    assert sandbox._sealed is True  # type: ignore[attr-defined]


def _sealing_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    client: _ExecClient,
    operating_system: OperatingSystem = OperatingSystem.WINDOWS,
    mode: NetworkMode = NetworkMode.ALLOWLIST,
) -> QemuSandbox:
    """A sandbox whose runner rules and guest calls land, in order, in ``client.calls``."""

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        client.calls.append(argv[-1])
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox.request = request(  # type: ignore[attr-defined]
        os=operating_system,
        network=NetworkPolicy(
            mode=mode,
            allowed_hosts=("example.com",) if mode is NetworkMode.ALLOWLIST else (),
        ),
        proxy_url="http://0.0.0.0:9443",
    )
    sandbox.container = "runner"  # type: ignore[attr-defined]
    sandbox.host_ip = "172.17.0.1"  # type: ignore[attr-defined]
    sandbox._client = client  # type: ignore[attr-defined]
    sandbox._sealed = False  # type: ignore[attr-defined]
    return sandbox


@pytest.mark.parametrize(
    ("operating_system", "mode"),
    [
        (OperatingSystem.WINDOWS, NetworkMode.ALLOWLIST),
        (OperatingSystem.LINUX, NetworkMode.ALLOWLIST),
        (OperatingSystem.WINDOWS, NetworkMode.BLOCK),
    ],
)
def test_a_windows_allowlist_seal_points_the_agents_system_proxy_at_the_episode_proxy(
    monkeypatch: pytest.MonkeyPatch, operating_system: OperatingSystem, mode: NetworkMode
) -> None:
    """A Windows browser takes its proxy from the account's settings, not the variables;
    the framework's own phases get the account's settings back before the runner opens."""
    client = _ExecClient()
    sandbox = _sealing_sandbox(monkeypatch, client, operating_system, mode)

    asyncio.run(sandbox.close_egress())
    sealed, client.calls = client.calls, []
    asyncio.run(sandbox.open_egress())
    lifted = client.calls

    bypass = ["172.30.0.1", "localhost", "127.0.0.1"]
    if operating_system is OperatingSystem.WINDOWS and mode is NetworkMode.ALLOWLIST:
        assert all(isinstance(rule, str) for rule in sealed[:-1])
        assert sealed[-1] == ("system_proxy", "http://ale:episode-token@172.30.0.1:9443", bypass)
        assert lifted[0] == ("system_proxy", None, bypass)
        assert all(isinstance(rule, str) for rule in lifted[1:])
    else:
        assert all(isinstance(call, str) for call in sealed + lifted)


@pytest.mark.parametrize("failure", [GuestUnreachableError("guest session closed"), TimeoutError()])
def test_a_system_proxy_the_guest_cannot_set_fails_the_seal(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    class Unanswered(_ExecClient):
        async def system_proxy(self, proxy: str | None, *, bypass: object = ()) -> None:
            raise failure

    sandbox = _sealing_sandbox(monkeypatch, Unanswered())
    with pytest.raises(ProviderStartError, match="could not set the agent's system proxy"):
        asyncio.run(sandbox.close_egress())


def test_a_windows_allowlist_guest_gets_its_own_proxy_settings_back_before_power_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot outlives the relay its system proxy names, even one a failed seal set."""

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        return 0, "0\n", ""

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox.request = request(  # type: ignore[attr-defined]
        os=OperatingSystem.WINDOWS,
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9443",
    )
    sandbox.container = "runner"  # type: ignore[attr-defined]
    sandbox.agent_user = "user"  # type: ignore[attr-defined]
    sandbox._client = client = _ExecClient()  # type: ignore[attr-defined]
    sandbox._file_client = _ExecClient()  # type: ignore[attr-defined]
    sandbox._sealed = False  # type: ignore[attr-defined]

    asyncio.run(sandbox._power_off())

    assert client.calls[0] == ("system_proxy", None, ["172.30.0.1", "localhost", "127.0.0.1"])
    assert client.calls[1] == ["shutdown.exe", "/s", "/t", "1"]


def test_only_whole_clusters_of_ntfs_free_space_are_discarded() -> None:
    from ale.run.providers.ntfs_discard import Volume, discardable

    kib, mib = 1024, 1024 * 1024
    bitmap = bytearray(b"\xff" * (64 * mib // (4 * kib) // 8))
    free = [*range(256, 512), *range(1024, 1032)]  # 1 MiB, and half a 64 KiB cluster
    for index in free:
        bitmap[index >> 3] &= ~(1 << (index & 7))
    volume = Volume(start=mib, length=64 * mib, cluster=4 * kib, bitmap=bytes(bitmap))
    held = [(0, 8 * mib), (2 * mib + 4 * kib, 64 * kib)]
    # Only the free megabyte, never a cluster partly in use, and the unaligned range not at all.
    assert list(discardable(held, [volume], 64 * kib)) == [(2 * mib, mib)]
    assert list(discardable([(0, mib)], [volume], 64 * kib)) == []  # outside the volume
    assert list(discardable(held, [], 64 * kib)) == []
    # qemu-io refuses 2 GiB or more at once, so a long free run comes in pieces.
    gib = 1024 * mib
    empty = Volume(start=0, length=5 * gib, cluster=4 * kib, bitmap=bytes(5 * gib // 4096 // 8))
    pieces = list(discardable([(0, 5 * gib)], [empty], 64 * kib))
    assert pieces == [(0, gib), (gib, gib), (2 * gib, gib), (3 * gib, gib), (4 * gib, gib)]
