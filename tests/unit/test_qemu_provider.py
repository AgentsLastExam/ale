"""The VM backend's decisions that can be checked without booting one.

A guest image is a multi-gigabyte build, so the boot path is covered by the conformance
suite behind ``needs_kvm``. What belongs here is everything that must be right *before*
anything is provisioned — because a provider that fails late fails expensively.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
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
from ale.run.providers.qemu import (
    EPISODE_LABEL,
    GPU_LABEL,
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
    _verify_qemu_gpu_count,
    _VfioGpu,
    attach_retained,
    destroy_retained,
    list_retained,
)

pytestmark = pytest.mark.unit


def request(**overrides: object) -> SandboxRequest:
    digest = "sha256:" + "0" * 64
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
    monkeypatch.setattr("ale.run.providers.qemu.asyncio.sleep", cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            QemuProvider()._boot(
                request(retention="keep"),
                tmp_path,
                tmp_path / "base.qcow2",
                7411,
            )
        )

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


@pytest.mark.asyncio
async def test_qemu_retain_returns_a_provider_qualified_handle() -> None:
    class Client:
        closed = False

        async def close(self) -> None:
            self.closed = True

    client = Client()
    sandbox = QemuSandbox.__new__(QemuSandbox)
    sandbox._client = client  # type: ignore[attr-defined]
    sandbox.container = "ale-qemu-test"  # type: ignore[attr-defined]
    sandbox.request = request(retention="keep")  # type: ignore[attr-defined]
    sandbox.allocation = SimpleNamespace(gpu=None)  # type: ignore[attr-defined]

    retained = await sandbox.retain(roles=("solver",), reason="debug")

    assert client.closed
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

    assert await list_retained() == [
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
    await destroy_retained("qemu:ale-qemu-test")
    assert removed == ["ale-qemu-test"]
    assert not storage.exists()


@pytest.mark.asyncio
async def test_qemu_retained_solver_can_be_reattached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = tmp_path / "episode"
    storage.mkdir()
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
        "Mounts": [{"Type": "bind", "Source": str(storage), "Destination": "/storage"}],
        "NetworkSettings": {"Ports": {"7411/tcp": [{"HostPort": "17411"}]}},
    }

    async def fake_run(*argv: str, **_kwargs: object) -> tuple[int, str, str]:
        assert argv[:2] == ("docker", "inspect")
        return 0, json.dumps([record]), ""

    class Transport:
        def __init__(self, host: str, port: int) -> None:
            assert (host, port) == ("127.0.0.1", 17411)

        async def start(self, *, timeout_sec: float) -> None:
            assert timeout_sec > 0

    class Client:
        def __init__(self, _transport: object) -> None:
            pass

        async def close(self) -> None:
            pass

    async def resolve(image: PreparedTaskImage) -> ResolvedImage:
        return ResolvedImage(
            kind="vm",
            prepared_identity=image.prepared_identity,
            observed_identity=image.prepared_identity,
            observed_ref=image.runtime_ref,
        )

    async def contract(
        _provider: QemuProvider, _client: object, _request: SandboxRequest
    ) -> tuple[str, str, bool]:
        return "user", "/home/user", False

    monkeypatch.setattr("ale.run.providers.qemu._run", fake_run)
    monkeypatch.setattr("ale.run.providers.qemu.TcpTransport", Transport)
    monkeypatch.setattr("ale.run.providers.qemu.GuestClient", Client)
    monkeypatch.setattr("ale.run.providers.qemu.resolve_prepared_vm_image", resolve)
    monkeypatch.setattr(QemuProvider, "_read_guest_contract", contract)
    allocation = ResourceAllocation(
        cpus=2,
        memory_mb=2048,
        sudo=False,
        network_mode=NetworkMode.BLOCK,
        provider="qemu",
    )

    sandbox = await attach_retained("qemu:ale-qemu-test", request(), allocation)

    assert sandbox.storage == storage
    assert sandbox.host_port == 17411
    assert sandbox.request.role.value == "solver"


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


def test_legacy_linux_health_uses_the_fixed_base_identity() -> None:
    class Client:
        async def health(self) -> dict[str, object]:
            return {"os": "linux", "gui": False}

        async def exists(self, path: str) -> bool:
            return path == "/home/user"

    observed = asyncio.run(
        QemuProvider()._read_guest_contract(Client(), request())  # type: ignore[arg-type]
    )
    assert observed == ("user", "/home/user", False)


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
    with pytest.raises(ProviderCapabilityError, match="kind=vm"):
        await QemuProvider().prepare_image(
            ImageRef(kind=ImageKind.CONTAINER, reference="ghcr.io/acme/container:v1")
        )


class TestCapabilities:
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

        What the guest can reach is the runner holding it, which forwards this one port
        onward — so that is the address the agent's SDK must be handed.
        """
        sandbox = QemuSandbox.__new__(QemuSandbox)
        sandbox.request = request(gateway_url="http://0.0.0.0:8931")  # type: ignore[attr-defined]
        assert sandbox.gateway_url == f"http://{HOST_IP}:8931"

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

    def test_guest_must_observe_the_requested_count(self) -> None:
        with pytest.raises(ProviderCapabilityError, match="count mismatch"):
            _verify_qemu_gpu_count(1, ())


class _ExecClient:
    def __init__(self) -> None:
        self.env: dict[str, str] | None = None
        self.argv: object = None

    async def exec(self, argv: object, **kwargs: Any) -> tuple[int, str, str, bool]:
        self.argv = argv
        self.env = kwargs.get("env")
        return 0, "", "", False


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
        "HTTP_PROXY": "http://episode-token:@172.30.0.1:9443",
        "HTTPS_PROXY": "http://episode-token:@172.30.0.1:9443",
        "http_proxy": "http://episode-token:@172.30.0.1:9443",
        "https_proxy": "http://episode-token:@172.30.0.1:9443",
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
    assert any("FORWARD -i docker ! -d 172.17.0.1 -j DROP" in rule for rule in rules)
    assert sandbox._sealed is True  # type: ignore[attr-defined]
