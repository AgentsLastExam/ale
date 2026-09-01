from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ale.core.sandbox import PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.providers.qemu_darwin import DarwinRuntime, image_architecture

pytestmark = pytest.mark.unit


def request(reference: str = "ghcr.io/acme/vm-windows10-base:1") -> SandboxRequest:
    digest = "sha256:" + "0" * 64
    return SandboxRequest(
        episode_id="darwin-episode",
        prepared_image=PreparedTaskImage(
            kind="vm",
            source="external-ref",
            input_identity=digest,
            runtime_ref="/tmp/base.qcow2",
            prepared_identity=digest,
            resolved_reference=reference,
        ),
        os=OperatingSystem.WINDOWS,
        resources=Resources(cpus=4, memory_mb=6144),
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        gateway_url="http://0.0.0.0:8931",
        proxy_url="http://0.0.0.0:9443",
    )


def test_image_name_is_the_only_architecture_declaration() -> None:
    assert image_architecture(request().prepared_image) == "x86_64"
    assert (
        image_architecture(request("ghcr.io/acme/vm-windows11-arm64-base:0.1.0").prepared_image)
        == "arm64"
    )


def test_x86_command_uses_tcg_and_separate_control_and_egress_nics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ale.run.providers.qemu_darwin.host_architecture", lambda: "arm64")
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-x86_64-code.fd").write_bytes(b"code")
    (firmware / "edk2-i386-vars.fd").write_bytes(b"vars")
    storage = tmp_path / "episode"
    storage.mkdir()
    runtime = DarwinRuntime(tmp_path)
    monkeypatch.setattr(runtime, "_firmware_dir", lambda _binary: firmware)
    argv = runtime.command(request(), storage, 17411, "x86_64")
    rendered = " ".join(argv)
    assert argv[0] == "qemu-system-x86_64"
    assert "q35,accel=tcg" in argv
    assert "if=ide" in rendered
    assert "edk2-x86_64-code.fd" in rendered
    assert (storage / "efi-vars.fd").read_bytes() == b"vars"
    assert "hostfwd=tcp:127.0.0.1:17411-:7411" in rendered
    assert "guestfwd=tcp:172.30.0.100:8931-cmd:/usr/bin/nc 127.0.0.1 8931" in rendered
    assert "id=control-nic" in rendered
    assert "id=egress-nic" in rendered
    assert rendered.count("e1000e") == 2
    assert "virtio-net-pci" not in rendered


def test_arm_command_uses_hvf_and_private_uefi_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-aarch64-code.fd").write_bytes(b"code")
    (firmware / "edk2-arm-vars.fd").write_bytes(b"vars")
    storage = tmp_path / "episode"
    storage.mkdir()
    runtime = DarwinRuntime(tmp_path)
    monkeypatch.setattr(runtime, "_firmware_dir", lambda _binary: firmware)
    monkeypatch.setattr(
        runtime,
        "_arm_binary",
        lambda: tmp_path / "qemu-system-aarch64-utm",
    )
    monkeypatch.setattr("ale.run.providers.qemu_darwin.host_architecture", lambda: "arm64")

    argv = runtime.command(
        request("ghcr.io/acme/vm-windows11-arm64-base:0.1.0"),
        storage,
        17411,
        "arm64",
    )

    assert argv[0] == str(tmp_path / "qemu-system-aarch64-utm")
    assert "virt,accel=hvf,highmem=off" in argv
    assert "virtio-ramfb" in argv
    assert "format=qcow2,file=" in " ".join(argv)
    assert "if=none,id=system" in " ".join(argv)
    assert "nvme,drive=system,serial=ALEWIN11ARM64,bootindex=0" in argv
    assert " ".join(argv).count("virtio-net-pci") == 2
    assert (storage / "efi-vars.fd").read_bytes() == b"vars"


def test_native_x86_uses_hvf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ale.run.providers.qemu_darwin.host_architecture", lambda: "x86_64")
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-x86_64-code.fd").write_bytes(b"code")
    (firmware / "edk2-i386-vars.fd").write_bytes(b"vars")
    storage = tmp_path / "episode"
    storage.mkdir()
    runtime = DarwinRuntime(tmp_path)
    monkeypatch.setattr(runtime, "_firmware_dir", lambda _binary: firmware)
    argv = runtime.command(request(), storage, 17411, "x86_64")
    assert "q35,accel=hvf" in argv
    assert any(value.startswith("host,") for value in argv)


def test_egress_only_toggles_the_second_nic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def qmp(_socket: Path, execute: str, arguments: dict[str, object] | None = None) -> None:
        calls.append((execute, arguments))

    monkeypatch.setattr("ale.run.providers.qemu_darwin._qmp_command", qmp)
    runtime = DarwinRuntime(tmp_path)
    asyncio.run(runtime.set_egress(tmp_path / "episode", enabled=False))
    asyncio.run(runtime.set_egress(tmp_path / "episode", enabled=True))
    assert calls == [
        ("set_link", {"name": "egress-nic", "up": False}),
        ("set_link", {"name": "egress-nic", "up": True}),
    ]


@pytest.mark.asyncio
async def test_retention_registry_finds_custom_overlay_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("ale.run.providers.qemu_darwin._process_alive", lambda _pid: False)
    runtime = DarwinRuntime(tmp_path / "custom-overlays")
    storage = tmp_path / "elsewhere" / "retained-id"
    storage.mkdir(parents=True)
    state = {
        "runtime": "darwin",
        "id": "retained-id",
        "pid": 123,
        "episode": "episode",
        "role": "solver",
        "retention": "keep",
        "architecture": "arm64",
        "host_port": 17411,
        "image": "ghcr.io/acme/vm-windows11-arm64-base:v1",
        "storage": str(storage.resolve()),
    }
    (storage / "runtime.json").write_text(json.dumps(state))
    runtime.registry_dir.mkdir(parents=True)
    runtime._registry_file("retained-id").write_text(json.dumps(state))

    records = await runtime.list_retained()
    assert records[0]["handle"] == "qemu:retained-id"
    assert records[0]["running"] is False
    await runtime.destroy_retained("retained-id")
    assert not storage.exists()
    assert not runtime._registry_file("retained-id").exists()
