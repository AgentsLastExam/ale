"""The microVM backend's decisions that can be checked without booting one.

Booting needs KVM and the runner image, so it is covered by the conformance suite behind
``needs_kvm``. What belongs here is what must be right before anything is provisioned:
the machine's configuration, the guest's settings, the egress rules and the records that
retention and snapshots trust.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    DesktopResolution,
    Identity,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.providers import firecracker_init
from ale.run.providers.firecracker import (
    GUEST_TAP,
    HOST_IP,
    MAX_LAYERS,
    SNAPSHOT_PREFIX,
    SNAPSHOT_RECORD,
    FirecrackerProvider,
    FirecrackerSandbox,
    _inject,
    _layer_devices,
    _sparse_digest,
    _vm_config,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "1" * 64


def request(**overrides: object) -> SandboxRequest:
    base: dict[str, object] = {
        "episode_id": "e1",
        "prepared_image": PreparedTaskImage(
            kind="container",
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref=f"image@{DIGEST}",
            prepared_identity=DIGEST,
            resolved_reference=f"image@{DIGEST}",
        ),
        "resources": Resources(cpus=2, memory_mb=2048),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        "gateway_url": "http://0.0.0.0:8931",
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *argv: str, timeout: float = 120) -> tuple[int, str, str]:
        self.calls.append(argv)
        return 0, "", ""

    def rules(self) -> list[str]:
        return [argv[-1] for argv in self.calls if argv[:2] == ("docker", "exec")]


class _Client:
    def __init__(self) -> None:
        self.env: dict[str, str] | None = None

    async def exec(
        self, argv, *, cwd=None, env=None, timeout_sec=None, run_as=None, output_sink=None
    ):  # type: ignore[no-untyped-def]
        self.env = env
        return 0, "", "", False

    async def close(self) -> None:
        return None


def sandbox(
    req: SandboxRequest, tmp_path: Path, client: _Client | None = None
) -> FirecrackerSandbox:
    client = client or _Client()
    return FirecrackerSandbox(
        sandbox_id="e1-abc",
        request=req,
        container="ale-fc-test",
        storage=tmp_path / "storage",
        layers=(),
        client=client,  # type: ignore[arg-type]
        file_client=client,  # type: ignore[arg-type]
        resolved_image=ResolvedImage(
            kind="container",
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref=str(tmp_path / "rootfs.ext4"),
        ),
        allocation=ResourceAllocation(
            cpus=2,
            memory_mb=2048,
            sudo=False,
            network_mode=req.network.mode,
            provider="firecracker",
        ),
        agent_user="user",
        agent_home="/home/user",
        host_ip="172.17.0.1",
        snapshot_dir=tmp_path / "snapshots",
        runner_image="ale-firecracker-runner:test",
    )


def test_capabilities_are_linux_microvms_with_every_network_mode() -> None:
    capabilities = FirecrackerProvider().capabilities()
    assert capabilities.operating_systems == frozenset({OperatingSystem.LINUX})
    assert capabilities.network_modes == frozenset(NetworkMode)
    assert capabilities.snapshot is True
    assert capabilities.max_cpus == 32


def test_gpus_vm_images_and_other_systems_are_refused_before_anything_starts() -> None:
    provider = FirecrackerProvider()
    with pytest.raises(ProviderCapabilityError, match="no GPU"):
        provider.accepts(request(resources=Resources(gpus=1)))
    vm = request(
        prepared_image=PreparedTaskImage(
            kind="vm",
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref="/tmp/disk.qcow2",
            prepared_identity=DIGEST,
            resolved_reference="local:///tmp/disk.qcow2",
        )
    )
    with pytest.raises(ProviderCapabilityError, match="container images"):
        provider.accepts(vm)
    with pytest.raises(ProviderCapabilityError, match="operating system"):
        provider.accepts(request(os=OperatingSystem.WINDOWS))
    with pytest.raises(ProviderCapabilityError, match="cpus"):
        provider.accepts(request(resources=Resources(cpus=33)))


def test_the_machine_boots_the_shared_kernel_on_the_read_only_root() -> None:
    config = _vm_config(cpus=3, memory_mb=1536, hostname="ale-x", layers=2)
    args = config["boot-source"]["boot_args"]  # type: ignore[index]
    assert "root=/dev/vda ro" in args
    assert f"ip=172.30.0.2::{HOST_IP}:255.255.255.0:ale-x:eth0:off" in args
    assert args.endswith("init=/opt/ale/python/bin/python3 -- -I /opt/ale/firecracker/init.py")
    drives = config["drives"]
    assert [(d["drive_id"], d["is_read_only"]) for d in drives] == [  # type: ignore[index, union-attr]
        ("rootfs", True),
        ("rw", False),
        ("layer0", True),
        ("layer1", True),
    ]
    assert config["machine-config"] == {"vcpu_count": 3, "mem_size_mib": 1536, "smt": False}
    assert config["network-interfaces"][0]["host_dev_name"] == GUEST_TAP  # type: ignore[index]


def test_layers_are_stacked_newest_first() -> None:
    # vda is the root, vdb the writable disk; the oldest layer is vdc.
    assert _layer_devices(3) == ["/dev/vde", "/dev/vdd", "/dev/vdc"]
    assert _layer_devices(0) == []


def test_the_guest_gets_the_run_resolution_the_gateway_and_the_request_env(
    tmp_path: Path,
) -> None:
    provider = FirecrackerProvider(overlay_dir=tmp_path)
    req = request(env={"TASK": "1"}, desktop_resolution=DesktopResolution(width=1366, height=768))
    provider._write_config(tmp_path, req, "ale-host", 1)
    sandbox_json = json.loads((tmp_path / "sandbox.json").read_text())
    assert sandbox_json["env"] == {
        "TASK": "1",
        "VNC_RESOLUTION": "1366x768",
        "ALE_GATEWAY_URL": f"http://{HOST_IP}:{SANDBOX_GATEWAY_PORT}",
    }
    assert sandbox_json["agent_user"] == "user"
    assert sandbox_json["layers"] == ["/dev/vdc"]
    vm = json.loads((tmp_path / "vm.json").read_text())
    assert vm["machine-config"]["vcpu_count"] == 2


def test_the_gateway_is_reached_at_the_runner_on_the_fixed_port(tmp_path: Path) -> None:
    assert sandbox(request(), tmp_path).gateway_url == f"http://{HOST_IP}:{SANDBOX_GATEWAY_PORT}"


@pytest.mark.asyncio
async def test_sealing_drops_everything_but_replies_and_the_episode_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr("ale.run.providers.firecracker._run", recorder)
    sealed = sandbox(
        request(network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
                proxy_url="http://0.0.0.0:9000"),
        tmp_path,
    )  # fmt: skip
    await sealed.close_egress()
    rules = recorder.rules()
    assert rules[0] == f"iptables -w -I FORWARD 1 -i {GUEST_TAP} -j DROP"
    assert any("--dports 8931,9000 -j ACCEPT" in rule for rule in rules)
    assert any("--ctdir REPLY" in rule for rule in rules)
    assert rules[-1] == f"iptables -w -I INPUT 1 -i {GUEST_TAP} -j DROP"
    recorder.calls.clear()
    await sealed.open_egress()
    assert all(rule.startswith("iptables -w -D ") for rule in recorder.rules())


@pytest.mark.asyncio
async def test_an_open_task_is_never_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr("ale.run.providers.firecracker._run", recorder)
    await sandbox(request(network=NetworkPolicy(mode=NetworkMode.OPEN)), tmp_path).close_egress()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_sealed_allowlist_agent_gets_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ale.run.providers.firecracker._run", _Recorder())
    client = _Client()
    req = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9000",
        proxy_token="t0ken",
    )
    box = sandbox(req, tmp_path, client)
    await box.exec(["true"], identity=Identity.AGENT)
    assert client.env is None
    await box.close_egress()
    await box.exec(["true"], identity=Identity.AGENT)
    assert client.env is not None
    assert client.env["HTTPS_PROXY"] == f"http://t0ken:@{HOST_IP}:9000"
    await box.exec(["true"])
    assert client.env is None


@pytest.mark.asyncio
async def test_the_gateway_and_proxy_are_routed_to_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        calls.append(argv[-1])
        return 0, "172.17.0.1\n" if "ip route" in argv[-1] else "", ""

    monkeypatch.setattr("ale.run.providers.firecracker._run", fake)
    req = request(
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        proxy_url="http://0.0.0.0:9000",
    )
    assert await FirecrackerProvider()._wire_network("ale-fc-x", req) == "172.17.0.1"
    rules = "\n".join(calls)
    assert (
        f"-i {GUEST_TAP} -p tcp -d {HOST_IP} --dport {SANDBOX_GATEWAY_PORT} "
        "-j DNAT --to-destination 172.17.0.1:8931" in rules
    )
    assert "--dport 9000 -j DNAT --to-destination 172.17.0.1:9000" in rules


def _snapshot(tmp_path: Path, name: str = "e1-abc", **record: object) -> Path:
    directory = tmp_path / "snapshots" / name
    directory.mkdir(parents=True)
    for disk in ("rootfs.ext4", "layer-0.ext4", "layer-1.ext4"):
        (directory / disk).write_bytes(b"disk")
    base = {
        "managed": True,
        "episode": "e1",
        "role": "solver",
        "os": "linux",
        "prepared_identity": DIGEST,
        "disks": ["rootfs.ext4", "layer-0.ext4", "layer-1.ext4"],
        "identity": "sha256:" + "2" * 64,
    }
    (directory / SNAPSHOT_RECORD).write_text(json.dumps(base | record))
    return directory


def test_a_snapshot_boots_its_root_under_its_layers_in_order(tmp_path: Path) -> None:
    directory = _snapshot(tmp_path)
    provider = FirecrackerProvider(snapshot_dir=tmp_path / "snapshots")
    image, layers = provider._snapshot_image(f"{SNAPSHOT_PREFIX}e1-abc", request())
    assert image.observed_ref == str(directory / "rootfs.ext4")
    assert image.observed_identity == "sha256:" + "2" * 64
    assert layers == (directory / "layer-0.ext4", directory / "layer-1.ext4")
    listed = provider._list_snapshots()
    assert [item["handle"] for item in listed] == [f"{SNAPSHOT_PREFIX}e1-abc"]


@pytest.mark.parametrize(
    ("handle", "record", "message"),
    [
        (f"{SNAPSHOT_PREFIX}../escape", {}, "not a Firecracker snapshot handle"),
        ("qemu:snapshot:e1-abc", {}, "not a Firecracker snapshot handle"),
        (f"{SNAPSHOT_PREFIX}e1-abc", {"managed": False}, "not an ALE snapshot"),
        (f"{SNAPSHOT_PREFIX}e1-abc", {"disks": ["../rootfs.ext4"]}, "not an ALE snapshot"),
        (f"{SNAPSHOT_PREFIX}e1-abc", {"disks": ["layer-0.ext4"]}, "not an ALE snapshot"),
        (
            f"{SNAPSHOT_PREFIX}e1-abc",
            {"disks": ["rootfs.ext4", "gone.ext4"]},
            "not an ALE snapshot",
        ),
        (f"{SNAPSHOT_PREFIX}e1-abc", {"prepared_identity": "sha256:" + "3" * 64}, "prepared"),
    ],
)
def test_only_an_intact_snapshot_of_the_tasks_image_boots(
    tmp_path: Path, handle: str, record: dict[str, object], message: str
) -> None:
    _snapshot(tmp_path, **record)
    provider = FirecrackerProvider(snapshot_dir=tmp_path / "snapshots")
    with pytest.raises(ProviderCapabilityError, match=message):
        provider._snapshot_image(handle, request())


@pytest.mark.asyncio
async def test_the_layer_chain_is_capped_before_the_guest_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr("ale.run.providers.firecracker._run", recorder)
    deep = sandbox(request(retention="snapshot"), tmp_path)
    deep.layers = tuple(tmp_path / f"{index}.ext4" for index in range(MAX_LAYERS))
    with pytest.raises(ProviderCapabilityError, match="snapshot layers"):
        await deep.snapshot(roles=("solver",), reason="test")
    assert recorder.calls == []
    assert not (tmp_path / "snapshots").exists()


@pytest.mark.asyncio
async def test_a_kept_container_must_carry_its_own_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    record = {
        "Name": "/ale-fc-x",
        "Config": {
            "Labels": {
                "ale.firecracker.managed": "true",
                "ale.firecracker.retention": "keep",
                "ale.firecracker.storage": str(storage),
            }
        },
        "Mounts": [{"Type": "bind", "Source": str(tmp_path / "other"), "Destination": "/storage"}],
    }

    async def fake(*argv: str, timeout: float = 120) -> tuple[int, str, str]:
        return 0, json.dumps([record]), ""

    monkeypatch.setattr("ale.run.providers.firecracker._run", fake)
    with pytest.raises(ProviderCapabilityError, match="ALE-owned"):
        await FirecrackerProvider().destroy_retained("firecracker:ale-fc-x")
    with pytest.raises(ProviderCapabilityError, match="not a Firecracker sandbox handle"):
        await FirecrackerProvider().destroy_retained("docker:ale-fc-x")


def test_sparse_digest_covers_size_offsets_and_bytes(tmp_path: Path) -> None:
    def disk(name: str, size: int, writes: dict[int, bytes]) -> Path:
        path = tmp_path / name
        with path.open("wb") as handle:
            handle.truncate(size)
            for offset, data in writes.items():
                handle.seek(offset)
                handle.write(data)
        return path

    a = disk("a", 1 << 22, {0: b"x" * 4096, 1 << 21: b"y" * 4096})
    same = disk("same", 1 << 22, {0: b"x" * 4096, 1 << 21: b"y" * 4096})
    moved = disk("moved", 1 << 22, {0: b"x" * 4096, (1 << 21) + 4096: b"y" * 4096})
    longer = disk("longer", 1 << 23, {0: b"x" * 4096, 1 << 21: b"y" * 4096})
    assert _sparse_digest(a) == _sparse_digest(same)
    assert _sparse_digest(a) != _sparse_digest(moved)
    assert _sparse_digest(a) != _sparse_digest(longer)


def test_the_init_and_the_image_config_ride_in_the_rootfs(tmp_path: Path) -> None:
    tar_path = tmp_path / "rootfs.tar"
    with tarfile.open(tar_path, "w") as tar:
        info = tarfile.TarInfo("etc/hostname")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"box\n"))
    config = {"Env": ["A=1"], "Entrypoint": None, "Cmd": ["sleep", "infinity"], "User": "user",
              "WorkingDir": "/work"}  # fmt: skip
    blocks, entries = _inject(tar_path, config)
    with tarfile.open(tar_path) as tar:
        members = {member.name: member for member in tar.getmembers()}
        assert json.loads(tar.extractfile("opt/ale/firecracker/image.json").read()) == config  # type: ignore[union-attr]
        init = tar.extractfile("opt/ale/firecracker/init.py").read()  # type: ignore[union-attr]
    assert init == Path(firecracker_init.__file__).read_bytes()
    for name in ("opt/ale/firecracker", "opt/ale/firecracker/rw", "opt/ale/firecracker/old"):
        assert members[name].isdir() and members[name].mode == 0o700
    assert members["opt/ale/firecracker/init.py"].uid == 0
    assert entries >= len(members) and blocks >= 3


# --- the guest's init ----------------------------------------------------------------


def test_the_init_resolves_an_oci_user_as_docker_does(monkeypatch: pytest.MonkeyPatch) -> None:
    import grp
    import pwd

    entry = pwd.struct_passwd(("user", "x", 1000, 1000, "", "/home/user", "/bin/sh"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry if name == "user" else _missing())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: entry if uid == 1000 else _missing())
    monkeypatch.setattr(grp, "getgrnam", lambda name: grp.struct_group(("video", "x", 44, [])))
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid, 27])
    assert firecracker_init._account("user") == (1000, 1000, "/home/user", [1000, 27])
    assert firecracker_init._account("1000:video") == (1000, 44, "/home/user", [44, 27])
    # A numeric uid with no account still runs, as Docker runs it, with that gid and /.
    assert firecracker_init._account("4242") == (4242, 4242, "/", [4242])
    with pytest.raises(KeyError):
        firecracker_init._account("nobody-here")


def _missing() -> None:
    raise KeyError("missing")


def test_the_init_gives_the_command_dockers_environment() -> None:
    env = firecracker_init._environment(
        ["PATH=/opt/bin", "A=image", "EMPTY="], {"A": "request"}, "ale-host", "/home/user"
    )
    assert env == {
        "PATH": "/opt/bin",
        "A": "request",
        "EMPTY": "",
        "HOME": "/home/user",
        "HOSTNAME": "ale-host",
    }
    defaults = firecracker_init._environment([], {}, "h", "/root")
    assert defaults["PATH"] == firecracker_init.DEFAULT_PATH
    assert defaults["HOME"] == "/root"


def test_the_container_slot_can_name_firecracker() -> None:
    from ale.core.config import RunConfig
    from ale.core.sandbox import ImageKind
    from ale.run.providers import RETAINING_PROVIDERS, ProviderRegistry, provider_named

    config = RunConfig.model_validate({"container": {"provider": "firecracker"}})
    chosen = ProviderRegistry(config).get(ImageKind.CONTAINER, OperatingSystem.LINUX)
    assert isinstance(chosen, FirecrackerProvider)
    assert provider_named("firecracker").name == "firecracker"
    assert "firecracker" in RETAINING_PROVIDERS
