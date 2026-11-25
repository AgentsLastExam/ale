"""The CPUs a Docker daemon's Host pins its Sandboxes to."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orbit.core.errors import ProviderCapabilityError
from orbit.core.taskspec import Resources
from orbit.run.providers import admission
from orbit.run.providers.admission import Admission, Grant, Topology, choose

pytestmark = pytest.mark.unit

#: Four cores of two threads each, numbered as Linux numbers an SMT machine's.
HOST = Topology(((0, 4), (1, 5), (2, 6), (3, 7)))


def test_cpu_lists_read_and_write_as_the_kernel_and_docker_do() -> None:
    assert admission.parse_cpu_list("0-3,8,10-11\n") == (0, 1, 2, 3, 8, 10, 11)
    assert admission.parse_cpu_list("") == ()
    assert admission.cpu_list([11, 0, 1, 2, 10, 8, 3]) == "0-3,8,10-11"


def test_the_topology_groups_each_core_s_threads(tmp_path: Path) -> None:
    (tmp_path / "online").write_text("0-3\n")
    for cpu, (package, core) in enumerate([(0, 0), (0, 1), (0, 0), (0, 1)]):
        place = tmp_path / f"cpu{cpu}" / "topology"
        place.mkdir(parents=True)
        (place / "physical_package_id").write_text(f"{package}\n")
        (place / "core_id").write_text(f"{core}\n")
    host = admission.topology.__wrapped__(tmp_path)
    assert host is not None and host.cores == ((0, 2), (1, 3))
    assert host.threads == 4
    # A Host without Linux's sysfs, such as a Mac, has none to pin by.
    assert admission.topology.__wrapped__(tmp_path / "none") is None


def test_a_sandbox_takes_whole_cores_no_other_sandbox_holds_a_thread_of() -> None:
    assert choose(HOST, set(), 4) == Grant((0, 1, 4, 5))
    assert choose(HOST, {0, 1, 4, 5}, 4) == Grant((2, 3, 6, 7))
    # A core one of whose threads is held is no one else's.
    assert choose(HOST, {0}, 4) == Grant((1, 2, 5, 6))
    assert choose(HOST, {0, 2, 7}, 4) is None


def test_a_grant_pins_the_container_and_labels_every_cpu_it_holds() -> None:
    assert admission.run_args(Grant((0, 1, 4, 5))) == [
        "--cpuset-cpus", "0-1,4-5", "--label", f"{admission.LABEL}=0-1,4-5",
    ]  # fmt: skip
    # An unpinned Sandbox is labelled too, so an image's label never counts as its hold.
    assert admission.run_args(None) == ["--label", f"{admission.LABEL}="]


class _Daemon:
    """A Docker daemon that applies cpusets, whose running containers hold ``held``."""

    def __init__(self, held: list[str], *, cpusets: bool = True) -> None:
        self.held = held
        self.cpusets = cpusets
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *argv: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:3] == ("info", "--format", "{{.ID}}"):
            return 0, "DAEMON\n", ""
        if argv[:3] == ("info", "--format", "{{.CPUSet}}"):
            return 0, f"{str(self.cpusets).lower()}\n", ""
        if argv[0] == "ps":
            return 0, "".join(f"{line}\n" for line in self.held), ""
        raise AssertionError(argv)


@pytest.mark.asyncio
async def test_a_sandbox_takes_what_no_running_container_holds_under_the_daemon_s_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission, "topology", lambda: HOST)
    daemon = _Daemon(["0-1,4-5", ""])
    async with Admission(daemon, tmp_path).admitted(Resources(cpus=4)) as grant:
        assert grant == Grant((2, 3, 6, 7))
        assert (tmp_path / "orbit-admission-DAEMON.lock").is_file()
    assert ("ps", "--filter", f"label={admission.LABEL}") == daemon.calls[-1][:3]


@pytest.mark.asyncio
async def test_a_sandbox_waits_while_too_few_cpus_are_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission, "topology", lambda: HOST)
    monkeypatch.setattr(admission, "_POLL_SEC", 0.01)
    daemon = _Daemon(["0-7"])

    async def frees() -> None:
        await asyncio.sleep(0.05)
        daemon.held = ["0-1,4-5"]

    freeing = asyncio.create_task(frees())
    async with Admission(daemon, tmp_path).admitted(Resources(cpus=4)) as grant:
        assert grant == Grant((2, 3, 6, 7))
    await freeing


@pytest.mark.asyncio
async def test_a_daemon_that_applies_no_cpuset_pins_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Admission(_Daemon([], cpusets=False), tmp_path).admitted(Resources(cpus=2)) as grant:
        assert grant is None
    # Nor on a Host whose CPUs it cannot place, nor under a runtime that does not hold a
    # Sandbox to its cpuset.
    monkeypatch.setattr(admission, "topology", lambda: None)
    async with Admission(_Daemon([]), tmp_path).admitted(Resources(cpus=2)) as grant:
        assert grant is None
    async with Admission(_Daemon([]), tmp_path).admitted(Resources(cpus=2), pins=False) as grant:
        assert grant is None


@pytest.mark.asyncio
async def test_a_sandbox_larger_than_the_host_is_refused_rather_than_kept_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission, "topology", lambda: HOST)
    with pytest.raises(ProviderCapabilityError, match="needs 10 CPUs; the Host has 8"):
        async with Admission(_Daemon([]), tmp_path).admitted(Resources(cpus=10)):
            pass
