"""The Docker Provider's GPUs: what nvidia-smi reports, admission to a whole or shared GPU,
and the watchdog that acts on a Sandbox past its share."""

from __future__ import annotations

import asyncio
import contextvars
import csv
import fcntl
import hashlib
import json
import logging
import os
import stat
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.support import TraceSink

import ale.run.providers.docker.provider as docker_provider
from ale.core.capability import GpuHold
from ale.core.errors import (
    GuestUnreachableError,
    ProviderCapabilityError,
    ProviderCapacityError,
    ProviderStartError,
)
from ale.core.sandbox import (
    GpuAllocation,
    GpuDevice,
    PreparedTaskImage,
    ResolvedImage,
    ResourceAllocation,
    SandboxRequest,
    SandboxState,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import gpu_sharing, subnets
from ale.run.providers.docker.gpu_sharing import GpuProcess, Holder, Overrun
from ale.run.providers.docker.provider import (
    DockerProvider,
    DockerSandbox,
    _eligible_gpus,
    _gpu_holders,
    _gpu_run_args,
    _parse_host_nvidia_smi,
    _parse_nvidia_smi,
    _Session,
    _verify_docker_gpu,
)
from ale.run.recording import execution_logging

pytestmark = pytest.mark.unit

UUID = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_nvidia_smi_rows_are_strict_and_normalized() -> None:
    (device,) = _parse_nvidia_smi(f"{UUID.upper()}, NVIDIA RTX 6000, 00000000:65:00.0, 570.1\n")
    assert device.id == UUID
    assert device.model == "NVIDIA RTX 6000"
    assert device.driver_version == "570.1"
    with pytest.raises(ProviderCapabilityError, match="nvidia-smi"):
        _parse_nvidia_smi("not,csv")


def test_host_nvidia_smi_rows_are_indexed_with_their_memory() -> None:
    devices = _parse_host_nvidia_smi(
        f"2, {UUID.upper()}, NVIDIA RTX 6000, 00000000:65:00.0, 570.1, 81559, 470, 52665\n"
        f"3, {OTHER}, NVIDIA RTX 6000, 00000000:66:00.0, 570.1, [N/A], [N/A], [N/A]\n"
    )
    assert devices[2].id == UUID
    assert devices[2].memory_reserved_mb == 470
    assert (devices[2].memory_total_mb, devices[2].memory_free_mb) == (81559, 52665)
    assert (devices[3].memory_total_mb, devices[3].memory_free_mb) == (None, None)
    assert devices[3].memory_reserved_mb is None
    with pytest.raises(ProviderCapabilityError, match="nvidia-smi"):
        _parse_host_nvidia_smi(f"2, {UUID}, NVIDIA RTX 6000, 00000000:65:00.0, 570.1\n")


def test_only_configured_gpus_of_a_model_the_task_accepts_are_leased() -> None:
    host = _parse_host_nvidia_smi(
        f"0, {UUID}, NVIDIA H100 80GB HBM3, 00000000:01:00.0, 580.1, 81559, 470, 81000\n"
        f"1, {OTHER}, NVIDIA RTX PRO 6000 Blackwell Server Edition, 00000000:02:00.0, 580.1,"
        " 97887, 470, 97000\n"
    )
    assert _eligible_gpus(host, (), Resources(gpus=1)) == (UUID, OTHER)
    rtx = Resources(gpus=1, gpu_types=("L40S", "RTX-PRO-6000"))
    assert _eligible_gpus(host, (), rtx) == (OTHER,)
    with pytest.raises(ProviderCapabilityError, match=r"Docker may use 1 NVIDIA H100 80GB HBM3$"):
        _eligible_gpus(host, (0,), rtx)
    # Too few without gpu_types is admission's to refuse, as it is when GPUs are busy.
    assert _eligible_gpus(host, (), Resources(gpus=3)) == (UUID, OTHER)
    with pytest.raises(ProviderCapabilityError, match="needs 2 GPUs of gpu_types"):
        _eligible_gpus(host, (), rtx.model_copy(update={"gpus": 2}))
    with pytest.raises(ProviderCapabilityError, match="not discovered: 7"):
        _eligible_gpus(host, (7,), rtx)


def test_docker_receives_only_selected_uuids() -> None:
    assert _gpu_run_args((UUID,)) == [
        "--gpus",
        f'"device={UUID}"',
        "--env",
        "NVIDIA_DRIVER_CAPABILITIES=all",
    ]


def test_a_device_list_reaches_docker_as_one_field() -> None:
    other = "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    value = _gpu_run_args((UUID, other))[1]
    # Docker parses the flag value as one CSV record (docker/cli opts/gpus.go).
    assert next(csv.reader([value])) == [f"device={UUID},{other}"]


def test_container_must_observe_exact_selected_uuids() -> None:
    """And the allocation records each as the Host does: nvidia-smi under gVisor's nvproxy
    reports no PCI address."""
    host = _parse_host_nvidia_smi(
        f"0, {OTHER}, RTX, 00000000:64:00.0, 570.1, 81559, 470, 1000\n"
        f"1, {UUID}, RTX, 00000000:65:00.0, 570.1, 81559, 470, 2000\n"
    )
    inside = _parse_nvidia_smi(f"{UUID}, RTX, [Unknown Error], 570.1\n")
    (recorded,) = _verify_docker_gpu((host[1],), inside)
    assert recorded == host[1] and recorded.pci_address == "00000000:65:00.0"
    assert recorded.memory_free_mb == 2000  # as admission read it
    with pytest.raises(ProviderCapabilityError, match="mismatch"):
        _verify_docker_gpu((host[0],), inside)


@pytest.mark.asyncio
async def test_what_ale_holds_is_read_from_its_running_containers_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Sandbox carries both labels, empty when unused; ALE's helpers, such as a QEMU
    runner, carry neither and hold nothing. A daemon that cannot list them admits nothing."""
    answer = (
        0,
        f"{'a' * 64} ale-whole {UUID},{OTHER}/\n"
        f"{'b' * 64} ale-shared {UUID}/8192\n"
        f"{'d' * 64} ale-cpu-only /\n"
        f"{'c' * 64} ale-qemu-runner /\n\n",
        "",
    )

    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        assert "label=ale.managed=true" in args and "--no-trunc" in args
        assert "-a" not in args  # a stopped container holds nothing
        return answer

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    assert await _gpu_holders() == [
        Holder("a" * 64, "ale-whole", (UUID, OTHER), None),
        Holder("b" * 64, "ale-shared", (UUID,), 8192),
    ]
    answer = (1, "", "Cannot connect to the Docker daemon")
    with pytest.raises(ProviderStartError, match="could not list ALE's Sandboxes: Cannot"):
        await _gpu_holders()


@pytest.mark.asyncio
async def test_retained_listing_includes_stale_state_and_cleanup_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        if args[:3] == ("ps", "-a", "-q"):
            return 0, "container-id\n", ""
        if args[0] == "images":
            return 0, "", ""
        assert args[0] == "inspect"
        return (
            0,
            json.dumps(
                [
                    {
                        "Name": "/ale-episode-solver",
                        "Image": "sha256:image",
                        "Config": {
                            "Labels": {
                                "ale.managed": "true",
                                "ale.retention": "keep",
                                "ale.episode": "episode",
                                "ale.role": "solver",
                                "ale.gpus": UUID,
                            }
                        },
                        "State": {"Running": False},
                    }
                ]
            ),
            "",
        )

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    assert await DockerProvider().list_retained() == [
        {
            "provider": "docker",
            "handle": "docker:ale-episode-solver",
            "episode": "episode",
            "role": "solver",
            "image": "sha256:image",
            "gpus": (UUID,),
            "running": False,
            "cleanup_command": "ale sandbox destroy docker:ale-episode-solver",
        }
    ]


@pytest.mark.asyncio
async def test_a_sandbox_another_run_destroys_mid_listing_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = {
        "Name": "/ale-kept",
        "Image": "sha256:image",
        "Config": {"Labels": {"ale.managed": "true", "ale.retention": "keep"}},
        "State": {"Running": True},
    }

    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        if args[:3] == ("ps", "-a", "-q"):
            return 0, "kept\ngone\n", ""
        if args[0] == "images":
            return 0, "removed\n", ""
        if args[:2] == ("image", "inspect"):
            return 1, "[]", "error: no such image: ale-snapshot:removed"
        assert args == ("inspect", "kept", "gone")
        return 1, json.dumps([kept]), "error: no such object: gone"

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    listed = await DockerProvider().list_retained()
    assert [record["handle"] for record in listed] == ["docker:ale-kept"]


@pytest.mark.asyncio
async def test_an_inspection_that_fails_otherwise_still_fails_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        if args[:3] == ("ps", "-a", "-q"):
            return 0, "kept\n", ""
        if args[0] == "images":
            return 0, "", ""
        return 1, "", "Cannot connect to the Docker daemon"

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderStartError, match="could not inspect ALE sandboxes"):
        await DockerProvider().list_retained()


@pytest.mark.asyncio
async def test_discard_destroys_any_ale_container_and_only_those(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container a crashed node agent created with retention destroy, by its handle."""
    labels = {"ale.managed": "true", "ale.retention": "destroy", "ale.role": "verifier"}
    record = {"Config": {"Labels": labels}, "NetworkSettings": {"Networks": {"ale-net-x": {}}}}
    calls: list[tuple[str, ...]] = []

    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        if args[0] == "inspect":
            if ("rm", "-f", "-v", "ale-e1") in calls:
                return 1, "", "No such object"
            return 0, json.dumps([record]), ""
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    await DockerProvider().discard("docker:ale-e1")
    assert ("rm", "-f", "-v", "ale-e1") in calls and ("network", "rm", "ale-net-x") in calls
    await DockerProvider().discard("docker:ale-e1")  # gone already
    calls.clear()
    labels["ale.managed"] = "false"
    with pytest.raises(ProviderCapabilityError, match="not an ALE Docker sandbox"):
        await DockerProvider().discard("docker:ale-e1")


@pytest.mark.asyncio
async def test_discard_fails_when_docker_cannot_say_the_container_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        return 1, "", "Cannot connect to the Docker daemon"

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderStartError, match="Cannot connect to the Docker daemon"):
        await DockerProvider().discard("docker:ale-e1")
    assert calls == [("inspect", "ale-e1")]


@pytest.mark.asyncio
async def test_destroy_rejects_non_ale_container(monkeypatch: pytest.MonkeyPatch) -> None:
    async def docker(*args: str, **kwargs: object) -> tuple[int, str, str]:
        return 0, json.dumps([{"Config": {"Labels": {}}}]), ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    with pytest.raises(ProviderCapabilityError, match="not an ALE-retained"):
        await DockerProvider().destroy_retained("docker:someone-elses-container")


# --- sharing a GPU by memory -----------------------------------------------------------------

G0, G1, G2 = (f"GPU-{digit * 8}-0000-0000-0000-000000000000" for digit in "123")
#: Three H100s as the Host's nvidia-smi reports them: memory.total, then memory.free.
HOST = _parse_host_nvidia_smi(
    f"0, {G0}, NVIDIA H100 80GB HBM3, 00000000:04:00.0, 570.195.03, 81559, 470, 52665\n"
    f"1, {G1}, NVIDIA H100 80GB HBM3, 00000000:23:00.0, 570.195.03, 81559, 470, 30000\n"
    f"2, {G2}, NVIDIA H100 80GB HBM3, 00000000:43:00.0, 570.195.03, 81559, 470, 70000\n"
)
DEVICES = sorted(HOST.items())
A, B, C = ("a" * 64, "b" * 64, "c" * 64)


def _process(container: str, gpu: str, used_mb: int, pid: int = 4242) -> GpuProcess:
    return GpuProcess(pid, gpu, used_mb, container, "123456", 77, "python3")


def _admit(
    holders: list[Holder],
    processes: list[GpuProcess],
    *,
    count: int = 1,
    memory_mb: int | None = None,
    margin_mb: int = 4096,
) -> list[str]:
    admitted = gpu_sharing.admit(
        DEVICES, holders, processes, count=count, memory_mb=memory_mb, margin_mb=margin_mb
    )
    return [device.id for device in admitted]


def test_a_sandbox_that_takes_its_gpus_whole_takes_none_an_ale_sandbox_holds() -> None:
    holders = [Holder(A, "ale-shared", (G0,), 20000), Holder(B, "ale-whole", (G2,), None)]
    assert _admit(holders, []) == [G1]
    with pytest.raises(ProviderCapacityError) as refused:
        _admit(holders, [], count=2)
    assert str(refused.value) == (
        "the Sandbox needs 2 GPUs no ALE Sandbox holds; of the GPUs it may take here: "
        f"GPU 0 ({G0}): ale-shared share it; GPU 1 ({G1}): eligible with 30000 MiB of "
        f"headroom; GPU 2 ({G2}): ale-whole holds it whole"
    )


def test_a_shared_sandbox_needs_headroom_beyond_what_ale_reserved_and_a_margin() -> None:
    """Headroom is the memory free less what ALE's Sandboxes reserved and do not use yet:
    what they use is already not free, so it counts once."""
    holders = [
        Holder(A, "ale-a", (G0,), 20000),  # uses 8000 on G0: 12000 still to come
        Holder(B, "ale-b", (G0, G1), 4000),  # uses 6000 on G0, more than it reserved
        Holder(C, "ale-c", (G2,), None),
    ]
    used = [_process(A, G0, 5000), _process(A, G0, 3000, pid=4243), _process(B, G0, 6000)]
    # 52665 - 12000 = 40665 on G0; 30000 - 4000 on G1; G2 is held whole.
    assert _admit(holders, used, memory_mb=36569) == [G0]
    with pytest.raises(ProviderCapacityError) as refused:
        _admit(holders, used, memory_mb=36570)
    assert str(refused.value) == (
        "the Sandbox needs 36570 MiB on each of 1 GPU, and a margin of 4096 MiB; of the GPUs "
        f"it may take here: GPU 0 ({G0}): 52665 MiB free, 12000 MiB of it reserved by ALE "
        "Sandboxes that do not use it yet, so 40665 MiB of headroom; "
        f"GPU 1 ({G1}): 30000 MiB free, 4000 MiB of it reserved by ALE Sandboxes that do not "
        f"use it yet, so 26000 MiB of headroom; GPU 2 ({G2}): ale-c holds it whole"
    )
    # The margin is the operator's.
    assert _admit(holders, used, memory_mb=40665, margin_mb=0) == [G0]
    # With nothing in use yet, all of what ALE's Sandboxes reserved is still to come.
    assert _admit(holders, [], memory_mb=24569) == [G0]
    with pytest.raises(ProviderCapacityError, match="24000 MiB of it reserved"):
        _admit(holders, [], memory_mb=24570)


def test_a_shared_sandbox_goes_where_the_most_headroom_is_and_shares_with_its_kind() -> None:
    shared = [Holder(A, "ale-a", (G2,), 30000)]
    # G2 has 40000 left, G0 52665, G1 30000.
    assert _admit(shared, [], memory_mb=1000, count=3) == [G0, G2, G1]
    # A GPU an ALE Sandbox shares takes no Sandbox that wants it whole.
    assert _admit(shared, [], count=2) == [G0, G1]


def test_a_gpu_whose_free_memory_nvidia_smi_does_not_report_takes_no_shared_sandbox() -> None:
    host = _parse_host_nvidia_smi(
        f"0, {G0}, NVIDIA H100 80GB HBM3, 00000000:04:00.0, 570.195.03, [N/A], [N/A], [N/A]\n"
    )
    with pytest.raises(ProviderCapacityError, match="reports no free memory"):
        gpu_sharing.admit(sorted(host.items()), [], [], count=1, memory_mb=1, margin_mb=0)
    (whole,) = gpu_sharing.admit(sorted(host.items()), [], [], count=1, memory_mb=None, margin_mb=0)
    assert whole.id == G0


def _proc(root: Path, pid: int, cgroup: str, nspid: str = "4242\t77") -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cgroup").write_text(cgroup)
    # A name with a space and a parenthesis, as comm allows.
    fields = " ".join(["S", *(str(n) for n in range(4, 22)), "123456", "0"])
    (directory / "stat").write_text(f"{pid} (py thon)) {fields}\n")
    (directory / "status").write_text(f"Name:\tpython3\nNSpid:\t{nspid}\nNSpgid:\t1\n")
    (directory / "comm").write_text("python3\n")


@pytest.mark.parametrize(
    "cgroup",
    [
        f"0::/system.slice/docker-{A}.scope\n",  # systemd driver, rootful
        f"0::/user.slice/user-2026.slice/user@2026.service/user.slice/docker-{A}.scope\n",
        f"0::/system.slice/docker-{A}.scope/init.scope\n",  # a container's own cgroups
        f"12:memory:/docker/{A}\n11:cpu:/docker/{A}\n",  # cgroupfs, v1
    ],
)
def test_a_host_pid_names_its_container_and_its_pid_inside(tmp_path: Path, cgroup: str) -> None:
    _proc(tmp_path, 4242, cgroup)
    assert gpu_sharing.host_process(4242, G0, 600, tmp_path) == _process(A, G0, 600)


def test_a_process_outside_every_container_or_gone_is_none(tmp_path: Path) -> None:
    _proc(tmp_path, 1, "0::/user.slice/user-2026.slice/session-1.scope\n")
    assert gpu_sharing.host_process(1, G0, 600, tmp_path) is None
    assert gpu_sharing.host_process(2, G0, 600, tmp_path) is None


@pytest.mark.asyncio
async def test_every_users_gpu_processes_are_read_and_those_in_containers_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _proc(tmp_path, 4242, f"0::/system.slice/docker-{A}.scope\n")
    _proc(tmp_path, 5000, "0::/user.slice/user-1001.slice/session-3.scope\n", "5000")

    async def run(*argv: str, timeout: float) -> tuple[int, str, str]:
        assert argv == (
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory,gpu_uuid",
            "--format=csv,noheader,nounits",
        )
        # Another user's job, a process gone since, ours, and one NVML gives no memory for.
        return 0, f"5000, 28416, {G0}\n4100, 512, {G0}\n4242, 600, {G1}\n4242, [N/A], {G2}\n", ""

    monkeypatch.setattr(gpu_sharing, "run", run)
    assert await gpu_sharing.read_processes(tmp_path) == [_process(A, G1, 600)]


@pytest.mark.parametrize(("used", "overran"), [(8192, False), (8193, True)])
def test_a_sandbox_past_its_share_is_an_overrun_and_one_at_it_is_not(
    used: int, overran: bool
) -> None:
    """A share holds every CUDA context, and the driver's own memory is in no row, so the
    rows' sum is compared with the share as it is."""
    processes = [
        _process(A, G0, used - 600),
        _process(A, G0, 600, pid=4243),
        _process(A, G1, 9000),  # each GPU on its own
        _process(B, G0, 8000),
        _process(C, G0, 99999),  # not watched
    ]
    found = gpu_sharing.overruns(processes, {A: 8192, B: 8192})
    expected = [(A, Overrun(G0, used, tuple(processes[:2])))] if overran else []
    expected.append((A, Overrun(G1, 9000, (processes[2],))))
    assert sorted(found, key=lambda item: item[1].gpu) == expected


def _watched_sandbox(
    hold: GpuHold, watchdog: gpu_sharing.Watchdog | None = None
) -> tuple[DockerSandbox, tuple[_Session, _Session]]:
    sessions = (_Session([], "ale-e1", 1024), _Session([], "ale-e1", 1024))
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.container = "ale-e1"
    sandbox.container_id = A
    sandbox._sessions = sessions
    sandbox._watcher = None
    sandbox._stopping = None
    sandbox._watchdog = watchdog or gpu_sharing.Watchdog()
    sandbox._storage = None
    sandbox._context = contextvars.copy_context()
    sandbox.state = SandboxState.READY
    sandbox.allocation = ResourceAllocation(
        cpus=1,
        memory_mb=1024,
        sudo=False,
        network_mode=NetworkMode.BLOCK,
        provider="docker:runc",
        leaf="docker:runc" if hold is GpuHold.WATCHED else "docker:gvisor",
        gpu=GpuAllocation(
            requested_count=1,
            provider_addresses=(G0,),
            lease_keys=(G0,),
            observed_devices=(),
            hold=hold,
            memory_mb=8192,
        ),
    )
    return sandbox, sessions


def _docker_calls(monkeypatch: pytest.MonkeyPatch, code: int = 0) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        calls.append(argv)
        return code, "", "process 77 is no longer the one that used the GPU" if code else ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    return calls


OVERRUN = Overrun(G0, 9000, (_process(A, G0, 600, pid=4241), _process(A, G0, 8400)))
#: How the helper kills process 77 of the Sandbox, which started at 123456.
KILL = (
    "run", "--rm", "--runtime", "runc", "--pid", f"container:{A}", "--network", "none",
    "--entrypoint", "sh", docker_provider.NETFILTER_IMAGE,
    "-c", gpu_sharing.KILL, "sh", "77", "123456",
)  # fmt: skip


@pytest.mark.asyncio
async def test_under_runc_the_watchdog_kills_the_largest_process_and_the_episode_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From a helper container in the Sandbox's PID namespace, once the PID's container and
    start time show it is the process that used the memory, as the OOM killer would kill a
    process: nothing in the Sandbox runs for it, its sessions stay, and the kill is logged
    where its episode's execution trace records what Providers do, in the phase of the
    command that ran when the watchdog found it."""
    rechecked: list[int] = []

    def host_process(pid: int, gpu: str, used_mb: int) -> GpuProcess:
        rechecked.append(pid)
        # Only its container and start time are compared: a renamed process is the same one.
        return GpuProcess(pid, gpu, used_mb + 5, A, "123456", 77, "renamed")

    monkeypatch.setattr(gpu_sharing, "host_process", host_process)
    sandbox, sessions = _watched_sandbox(GpuHold.WATCHED)
    sink = TraceSink()
    with execution_logging(sink, episode_id="e1", phase="agent", component="test"):
        sandbox._context = contextvars.copy_context()
    calls: list[tuple[str, ...]] = []

    async def docker(*argv: str, **_: object) -> tuple[int, str, str]:
        # The killed command returns, and the next phase's starts, while the helper's
        # container is being removed.
        calls.append(argv)
        with execution_logging(sink, episode_id="e1", phase="verify", component="test"):
            sandbox._context = contextvars.copy_context()
        return 0, "", ""

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", docker)
    await sandbox.overran(OVERRUN)
    assert rechecked == [4242]
    assert calls == [KILL]
    assert all(session.stopped is None for session in sessions)
    (logged,) = sink.events
    assert (logged.level, logged.phase) == ("warning", "agent")
    assert logged.message == (
        "ALE's GPU memory watchdog killed process 77 (python3), which held 8400 MiB: the "
        f"Sandbox used 9000 MiB of GPU {G0}, more than its gpu_memory_mb of 8192 MiB, which "
        "docker:runc does not enforce"
    )
    assert logged.data == {
        "process": 77,
        "name": "python3",
        "process_mb": 8400,
        "gpu": G0,
        "sandbox_mb": 9000,
        "gpu_memory_mb": 8192,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["reused", "ended"])
async def test_under_runc_a_process_that_is_no_longer_the_one_is_left_for_the_next_look(
    monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    calls = _docker_calls(monkeypatch)
    now = {"reused": GpuProcess(4242, G0, 8400, A, "999999", 77, "python3"), "ended": None}[why]
    monkeypatch.setattr(gpu_sharing, "host_process", lambda *_: now)
    sandbox, _ = _watched_sandbox(GpuHold.WATCHED)
    await sandbox.overran(OVERRUN)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("destroyed", [False, True])
async def test_a_failed_kill_is_reported_unless_the_episode_removed_the_sandbox_meanwhile(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, destroyed: bool
) -> None:
    """The helper refuses a PID that started at another time than the watchdog saw."""
    _docker_calls(monkeypatch, code=4)
    monkeypatch.setattr(gpu_sharing, "host_process", lambda *_: _process(A, G0, 8400))
    sandbox, _ = _watched_sandbox(GpuHold.WATCHED)
    if destroyed:
        sandbox.state = SandboxState.DESTROYED
    await sandbox.overran(OVERRUN)
    assert ("could not kill process 4242" in caplog.text) is not destroyed
    assert "no longer the one" in caplog.text or destroyed


@pytest.mark.asyncio
async def test_under_gvisor_the_watchdog_stops_the_whole_sandbox_and_its_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nvproxy should have refused the memory, so its accounting failed, which is ALE's
    failure: the Sandbox stops, and the end of its sessions says why."""
    calls = _docker_calls(monkeypatch)
    watchdog = gpu_sharing.Watchdog()
    sandbox, sessions = _watched_sandbox(GpuHold.LIMIT, watchdog)
    watchdog._watched[A] = gpu_sharing.Watched(A, 8192, sandbox.overran)
    await sandbox.overran(Overrun(G0, 9000, (_process(A, G0, 9000),)))
    assert calls == [("kill", "ale-e1")]
    assert A not in watchdog._watched
    ended = await sessions[0].explain(GuestUnreachableError("guest session closed"))
    assert str(ended) == (
        "the sandbox itself died: ALE's GPU memory watchdog stopped it: the Sandbox used 9000 "
        f"MiB of GPU {G0}, more than its gpu_memory_mb of 8192 MiB, past docker:gvisor's limit"
    )


@pytest.mark.asyncio
async def test_a_watched_runc_sandbox_is_killed_again_when_it_overruns_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog's own loop finds the overrun and the Sandbox stays watched after the kill,
    and at its share it is left alone."""
    calls = _docker_calls(monkeypatch)
    monkeypatch.setattr(gpu_sharing, "host_process", lambda pid, gpu, used: _process(A, gpu, used))
    rows = [[_process(A, G0, 8400)], [_process(A, G0, 8192)], [_process(A, G0, 9000)]]

    async def read() -> list[GpuProcess]:
        return rows.pop(0) if rows else []

    monkeypatch.setattr(gpu_sharing, "read_processes", read)
    watchdog = gpu_sharing.Watchdog()
    sandbox, _ = _watched_sandbox(GpuHold.WATCHED, watchdog)
    watchdog._watched[A] = gpu_sharing.Watched(A, 8192, sandbox.overran)
    for _ in range(3):
        await watchdog.check()
    assert calls == [KILL, KILL]
    assert A in watchdog._watched


@pytest.mark.asyncio
async def test_one_watchdog_acts_on_every_overrun_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow action delays no other, and one task serves every Sandbox until none is
    watched."""
    started: list[str] = []
    release = asyncio.Event()
    looks: list[int] = []

    async def read() -> list[GpuProcess]:
        looks.append(1)
        return [_process(A, G0, 9000), _process(B, G0, 9000), _process(C, G0, 100)]

    def act(container: str):  # type: ignore[no-untyped-def]
        async def acting(_overrun: Overrun) -> None:
            started.append(container)
            await release.wait()

        return acting

    monkeypatch.setattr(gpu_sharing, "read_processes", read)
    monkeypatch.setattr(gpu_sharing, "WATCH_SEC", 0.01)
    watchdog = gpu_sharing.Watchdog()
    for container in (A, B, C):
        watchdog.watch(gpu_sharing.Watched(container, 8192, act(container)))
    task = watchdog._task
    await asyncio.sleep(0.1)
    assert sorted(started) == [A, B]  # both under way, neither waiting for the other
    release.set()
    await asyncio.sleep(0.05)
    assert watchdog._task is task and not task.done()  # type: ignore[union-attr]
    for container in (A, B, C):
        watchdog.unwatch(container)
    await asyncio.sleep(0.05)
    assert task.done()  # type: ignore[union-attr]
    count = len(looks)
    await asyncio.sleep(0.05)
    assert len(looks) == count


@pytest.mark.asyncio
async def test_a_watchdog_that_cannot_look_warns_once_and_once_more_when_it_can(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    outcomes = [False, False, False, True, True, False, True]

    async def read() -> list[GpuProcess]:
        if outcomes and not outcomes.pop(0):
            raise ProviderStartError("nvidia-smi could not list the GPUs' processes")
        return []

    monkeypatch.setattr(gpu_sharing, "read_processes", read)
    monkeypatch.setattr(gpu_sharing, "WATCH_SEC", 0.001)
    watchdog = gpu_sharing.Watchdog()
    with caplog.at_level(logging.WARNING, logger=gpu_sharing.__name__):
        watchdog.watch(gpu_sharing.Watched(A, 8192, _act_never))
        while outcomes:
            await asyncio.sleep(0.005)
        watchdog.unwatch(A)
        await asyncio.sleep(0.02)
    assert [record.getMessage() for record in caplog.records] == [
        "the GPU memory watchdog cannot look; it goes on trying",
        "the GPU memory watchdog looks again",
        "the GPU memory watchdog cannot look; it goes on trying",
        "the GPU memory watchdog looks again",
    ]


async def _act_never(_overrun: Overrun) -> None:
    raise AssertionError("nothing overran")


@pytest.mark.parametrize("same", [True, False])
def test_the_kill_script_kills_only_a_process_that_started_when_the_watchdog_saw(
    same: bool,
) -> None:
    """Run by the helper image's dash, as the helper runs it."""
    victim = subprocess.Popen(["sleep", "60"])
    try:
        stat = Path(f"/proc/{victim.pid}/stat").read_text()
        started = stat[stat.rindex(")") + 2 :].split()[19]
        result = subprocess.run(
            ["dash", "-c", gpu_sharing.KILL, "sh", str(victim.pid), started if same else "1"],
            capture_output=True,
            text=True,
            check=False,
        )
        if same:
            assert result.returncode == 0 and victim.wait(timeout=10) == -9
        else:
            assert result.returncode == 4 and "no longer the one" in result.stderr
            assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


# --- creating a Sandbox that shares GPUs -----------------------------------------------------

DIGEST = "sha256:" + "0" * 64
#: What ``docker inspect`` reports a Sandbox of each leaf ran under.
RUNTIME_NAMES = {"runc": "runc", "gvisor": "runsc"}


def _request(resources: Resources, episode: str = "e1") -> SandboxRequest:
    return SandboxRequest(
        episode_id=episode,
        prepared_image=PreparedTaskImage(
            kind="container",
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref="image@" + DIGEST,
            prepared_identity=DIGEST,
            resolved_reference="image@" + DIGEST,
        ),
        resources=resources,
        network=NetworkPolicy(mode=NetworkMode.OPEN),
    )


class Daemon:
    """A Docker daemon on a Host with ``HOST``'s GPUs, as ``create`` sees it: it lists the
    ALE containers running, ``holders`` and those it started, by their labels, and starts a
    Sandbox after ``starting`` returns, or, unless ``starts``, refuses it once the flags were
    read."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        holders: str = "",
        processes: list[GpuProcess] | None = None,
        starts: bool = False,
        starting: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        #: The runtime each container was started under, by its name.
        self.runtimes: dict[str, str] = {}
        self.holders = holders
        self.starts = starts
        self.starting = starting
        image = ResolvedImage(
            kind="container",
            prepared_identity=DIGEST,
            observed_identity=DIGEST,
            observed_ref="image@" + DIGEST,
        )

        async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
            return HOST

        async def read_processes() -> list[GpuProcess]:
            return processes or []

        async def resolved(_prepared: PreparedTaskImage) -> ResolvedImage:
            return image

        async def no_problems(_self: DockerProvider, _reference: str) -> list[str]:
            return []

        async def framed(
            _self: DockerProvider, found: ResolvedImage, _request: SandboxRequest
        ) -> ResolvedImage:
            return found

        async def bridge_mac(_network: str, _host_ip: str) -> str:
            return "02:42:aa:bb:cc:01"

        monkeypatch.setattr("ale.run.providers.docker.provider._docker", self.docker)
        monkeypatch.setattr(
            "ale.run.providers.docker.provider.resolve_prepared_container_image", resolved
        )
        monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
        monkeypatch.setattr(DockerProvider, "check_image", no_problems)
        monkeypatch.setattr(DockerProvider, "_framed", framed)
        monkeypatch.setattr(gpu_sharing, "read_processes", read_processes)
        monkeypatch.setattr("ale.run.providers.docker.provider._bridge_mac", bridge_mac)

    async def docker(self, *argv: str, **_: object) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:2] == ("network", "inspect"):
            return 0, "172.17.0.1\n", ""
        if argv[-len(subnets.ROUTES) :] == subnets.ROUTES:
            return 0, "[]", ""
        if argv[0] == "ps":
            return 0, self.holders, ""
        if argv[:2] == ("run", "-d"):
            if self.starting is not None:
                await self.starting()
            if not self.starts:
                return 1, "", "stopped after the flags were read"
            label = dict(
                argv[index + 1].split("=", 1)
                for index, flag in enumerate(argv)
                if flag == "--label"
            )
            name = argv[argv.index("--name") + 1]
            self.runtimes[name] = argv[argv.index("--runtime") + 1]
            container = hashlib.sha256(name.encode()).hexdigest()
            self.holders += f"{container} {name} {label['ale.gpus']}/{label['ale.gpu_memory_mb']}\n"
            return 0, container + "\n", ""
        if argv[0] == "inspect":
            return 0, f"{DIGEST} {self.runtimes.get(argv[-1], 'runc')}\n", ""
        if argv[:3] == ("info", "--format", "{{.ID}}"):
            return 0, "DAEMON-1\n", ""
        if argv[0] == "info":
            return 0, "29.9.3\n", ""
        return 0, "", ""

    def runs(self) -> list[tuple[str, ...]]:
        """Each Sandbox Docker was asked to start."""
        return [call for call in self.calls if call[:2] == ("run", "-d")]


def _starts(monkeypatch: pytest.MonkeyPatch, runtime: str) -> None:
    """A Sandbox that Docker started is driven through stand-ins: guestd, its contract, its
    GPUs (G2 as nvproxy shows it), its confinement and its memory watcher."""

    async def connect(_provider: DockerProvider, container: str, *_: object) -> _Session:
        session = _Session([], container, 1024)
        session.client = SimpleNamespace(close=_nothing)  # type: ignore[assignment]
        return session

    async def contract(_provider: DockerProvider, _client: object) -> tuple[str, bool]:
        return "/home/user", False

    async def container_gpus(_provider: DockerProvider, _client: object) -> tuple[GpuDevice, ...]:
        return _parse_nvidia_smi(f"{G2}, NVIDIA H100 80GB HBM3, [Unknown Error], 570.195.03\n")

    async def nothing(*_: object, **__: object) -> None:
        return None

    async def no_watcher(*_: object) -> None:
        """gVisor's memory watcher, which runs in the sandbox."""

    monkeypatch.setattr(DockerProvider, "_connect", connect)
    monkeypatch.setattr(DockerProvider, "_guest_contract", contract)
    monkeypatch.setattr(DockerProvider, "_container_gpus", container_gpus)
    monkeypatch.setattr("ale.run.providers.docker.provider.set_elevation", nothing)
    monkeypatch.setattr("ale.run.providers.docker.provider.verify_identities", nothing)
    monkeypatch.setattr("ale.run.providers.docker.provider._GvisorConfinement.wire", nothing)
    monkeypatch.setattr("ale.run.providers.docker.provider._watcher", no_watcher)
    # Inspected as Docker records the runtime the Sandbox was started under.
    _ = RUNTIME_NAMES[runtime]


def _free(lock: Path) -> bool:
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "gpus", "memory", "limit"),
    [
        (
            "gvisor",
            1,
            8192,
            ["--annotation", f"dev.gvisor.flag.nvproxy-gpu-memory-limit={8 << 30}"],
        ),
        ("runc", 1, 8192, []),  # nothing limits it; the watchdog watches it
        ("gvisor", 1, None, []),
        ("runc", 1, None, []),
        ("runc", 0, None, []),
    ],
)
async def test_every_sandbox_is_labelled_with_what_it_holds_and_limited_where_it_can_be(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: str,
    gpus: int,
    memory: int | None,
    limit: list[str],
) -> None:
    """Both labels on every Sandbox, empty where it holds nothing, so that an image's labels
    never stand in for ALE's record; the lock is held while Docker starts it."""
    provider = DockerProvider(runtime=runtime, gpu_lock_dir=tmp_path)
    lock = tmp_path / "ale-gpu-DAEMON-1.lock"
    held: list[bool] = []

    async def starting() -> None:
        held.append(lock.exists() and not _free(lock))

    daemon = Daemon(monkeypatch, starting=starting)
    with pytest.raises(
        (ProviderCapabilityError, ProviderStartError), match="stopped after the flags were read"
    ):
        await provider.create(_request(Resources(gpus=gpus, gpu_memory_mb=memory)))
    (run,) = daemon.runs()
    labels = [run[index + 1] for index, flag in enumerate(run) if flag == "--label"]
    # The most free memory: G2, whose 70000 MiB no ALE Sandbox reserved.
    assert f"ale.gpus={G2 if gpus else ''}" in labels
    assert f"ale.gpu_memory_mb={memory or ''}" in labels
    annotations = [
        run[index : index + 2] for index, flag in enumerate(run) if flag == "--annotation"
    ]
    assert [list(pair) for pair in annotations] == ([limit] if limit else [])
    # Held while Docker started it, and free again once the attempt failed; its own network
    # went with it.
    assert held == [bool(gpus)]
    assert not gpus or _free(lock)
    removed = ("network", "rm") in [call[:2] for call in daemon.calls]
    assert removed is (runtime == "gvisor")


@pytest.mark.asyncio
async def test_a_refused_sandbox_starts_nothing_and_says_why_for_each_gpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = DockerProvider(runtime="gvisor", gpu_lock_dir=tmp_path)
    holders = f"{A} ale-a {G0}/40000\n{B} ale-b {G1},{G2}/\n"
    daemon = Daemon(monkeypatch, holders=holders, processes=[_process(A, G0, 600)])
    with pytest.raises(ProviderCapacityError) as refused:
        await provider.create(_request(Resources(gpus=1, gpu_memory_mb=10000)))
    assert f"GPU 0 ({G0}): 52665 MiB free, 39400 MiB of it reserved" in str(refused.value)
    assert f"GPU 1 ({G1}): ale-b holds it whole" in str(refused.value)
    assert "a margin of 4096 MiB" in str(refused.value)
    assert not daemon.runs()
    # The Sandbox's own network goes with it, and the lock is free again.
    assert daemon.calls[-1][:2] == ("network", "rm")
    assert _free(tmp_path / "ale-gpu-DAEMON-1.lock")


@pytest.mark.asyncio
async def test_two_creates_through_one_daemon_take_turns_and_the_second_sees_the_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two processes, two Providers of one lock directory: the second admits only once the
    first's Sandbox runs, and finds it by its label."""
    _starts(monkeypatch, "runc")
    order: list[str] = []

    async def starting() -> None:
        order.append("run")
        await asyncio.sleep(0.3)

    daemon = Daemon(monkeypatch, starts=True, starting=starting)
    first, second = (DockerProvider(gpus=(2,), gpu_lock_dir=tmp_path) for _ in range(2))
    taking = asyncio.ensure_future(first.create(_request(Resources(gpus=1), "first")))
    await asyncio.sleep(0.05)
    with pytest.raises(ProviderCapacityError, match=r"ale-first-[0-9a-f]+ holds it whole"):
        await second.create(_request(Resources(gpus=1), "second"))
    sandbox = await taking
    assert len(daemon.runs()) == 1 and order == ["run"]
    await sandbox.destroy()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["timed out", "cancelled"])
async def test_a_docker_run_that_never_returns_leaves_the_lock_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    lock = tmp_path / "ale-gpu-DAEMON-1.lock"

    async def starting() -> None:
        if how == "timed out":
            raise ProviderStartError("docker run -d --name timed out after 300s")
        await asyncio.Event().wait()

    daemon = Daemon(monkeypatch, starting=starting)
    provider = DockerProvider(runtime="gvisor", gpu_lock_dir=tmp_path)
    creating = asyncio.ensure_future(provider.create(_request(Resources(gpus=1))))
    await asyncio.sleep(0.1)
    if how == "cancelled":
        assert not _free(lock)
        creating.cancel()
    with pytest.raises((ProviderStartError, asyncio.CancelledError)):
        await creating
    assert _free(lock)
    # What it began is removed, its network too.
    assert ("rm", "-f", "-v") in [call[:3] for call in daemon.calls]
    assert ("network", "rm") in [call[:2] for call in daemon.calls]


def test_the_lock_file_is_one_every_user_can_lock_and_is_created_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Created readable by all whatever the umask, opened read-only when it exists, since
    fs.protected_regular refuses O_CREAT on another user's file in a sticky /tmp, and opened
    as another's when that process created it between the two opens."""
    path = tmp_path / "ale-gpu-DAEMON-1.lock"
    previous = os.umask(0o077)
    try:
        descriptor = gpu_sharing._open_lock(path)
    finally:
        os.umask(previous)
    os.close(descriptor)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o444)  # as another user's file looks: no write needed
    descriptor = gpu_sharing._open_lock(path)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(descriptor)
    # Another process creates it between this one's two opens.
    raced = tmp_path / "raced.lock"
    real = os.open
    attempts: list[int] = []

    def opening(name: object, flags: int, mode: int = 0o777) -> int:
        attempts.append(flags)
        if len(attempts) == 1:
            raise FileNotFoundError(name)
        if flags & os.O_EXCL:
            real(raced, os.O_RDONLY | os.O_CREAT, 0o644)
            raise FileExistsError(name)
        return real(name, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(gpu_sharing.os, "open", opening)
    os.close(gpu_sharing._open_lock(raced))
    assert attempts == [os.O_RDONLY, os.O_RDONLY | os.O_CREAT | os.O_EXCL, os.O_RDONLY]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "hold"), [("runc", GpuHold.WATCHED), ("gvisor", GpuHold.LIMIT)]
)
async def test_a_shared_sandbox_records_its_reservation_and_is_watched_while_a_run_holds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime: str, hold: GpuHold
) -> None:
    """The RunLock's allocation says what each GPU had at admission, what the Sandbox
    reserved on it and how it is held to that. It is watched until it is destroyed, kept or
    snapshotted, and again once a later Run reattaches to it."""
    provider = DockerProvider(runtime=runtime, gpu_lock_dir=tmp_path)
    _starts(monkeypatch, runtime)
    daemon = Daemon(monkeypatch, starts=True)
    monkeypatch.setattr(gpu_sharing, "WATCH_SEC", 0.01)
    sandboxes = []
    for episode in ("kept", "snapshotted", "destroyed"):
        sandbox = await provider.create(_request(Resources(gpus=1, gpu_memory_mb=8192), episode))
        assert isinstance(sandbox, DockerSandbox)
        sandboxes.append(sandbox)
        assert provider._watchdog._watched[sandbox.container_id].memory_mb == 8192
    gpu = sandboxes[0].allocation.gpu
    assert gpu is not None
    assert (gpu.hold, gpu.memory_mb, gpu.provider_addresses) == (hold, 8192, (G2,))
    (device,) = gpu.observed_devices
    assert (device.memory_total_mb, device.memory_reserved_mb, device.memory_free_mb) == (
        81559,
        470,
        70000,
    )
    kept, snapshotted, destroyed = sandboxes
    await kept.retain(roles=("solver",), reason="kept")
    await snapshotted.snapshot(roles=("solver",), reason="kept")
    await destroyed.destroy()
    assert provider._watchdog._watched == {}
    await asyncio.sleep(0.05)
    assert provider._watchdog._task is not None and provider._watchdog._task.done()
    # A later Run reattaches to the kept one, whose recorded allocation says what it shares.
    record = {
        "Id": kept.container_id,
        "Image": DIGEST,
        "Config": {
            "Labels": {
                "ale.managed": "true",
                "ale.role": "solver",
                "ale.network": "open",
                "ale.bridge-mac": "02:42:aa:bb:cc:01",
            }
        },
        "State": {"Running": True},
        "HostConfig": {"Runtime": RUNTIME_NAMES[runtime]},
        "NetworkSettings": {"Networks": {"ale-net-x": {}} if runtime == "gvisor" else {}},
    }
    docker = daemon.docker

    async def inspecting(*argv: str, **kwargs: object) -> tuple[int, str, str]:
        if argv[:2] == ("inspect", kept.container):
            return 0, json.dumps([record]), ""
        return await docker(*argv, **kwargs)

    monkeypatch.setattr("ale.run.providers.docker.provider._docker", inspecting)
    again = await provider.reattach(kept.handle, kept.request, kept.allocation, kept.resolved_image)
    assert provider._watchdog._watched[again.container_id].memory_mb == 8192
    await again.destroy()


async def _nothing() -> None:
    return None


def test_an_allocation_reserves_memory_exactly_when_it_shares_its_gpus() -> None:
    base = {"requested_count": 1, "provider_addresses": (G0,), "lease_keys": (G0,)}
    GpuAllocation(**base, observed_devices=(), hold=GpuHold.LIMIT, memory_mb=1)  # type: ignore[arg-type]
    GpuAllocation(**base, observed_devices=())  # type: ignore[arg-type]
    for hold, memory in ((GpuHold.WATCHED, None), (GpuHold.WHOLE, 1)):
        with pytest.raises(ValueError, match="exactly when it is shared"):
            GpuAllocation(**base, observed_devices=(), hold=hold, memory_mb=memory)  # type: ignore[arg-type]
