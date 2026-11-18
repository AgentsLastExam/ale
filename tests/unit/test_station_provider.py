"""The station Provider's own logic, without a Station: what it asks ale-stationd and when,
what it refuses, and what it records."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, ClassVar, get_args

import pytest

from ale.core.config import ProviderId, RunConfig, StationProviderConfig
from ale.core.errors import (
    ConfigError,
    ProviderCapabilityError,
    ProviderCapacityError,
    ProviderStartError,
)
from ale.core.ids import content_hash
from ale.core.sandbox import (
    DesktopResolution,
    Identity,
    ImageKind,
    ImageRef,
    SandboxRequest,
    SandboxRole,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run import guestd
from ale.run.framework import station_framework_identity
from ale.run.lint import lint_repository
from ale.run.providers import ProviderRegistry
from ale.run.providers.station import provider as station_module
from ale.run.providers.station import stationd
from ale.run.providers.station.provider import (
    StationProvider,
    StationSandbox,
    _Lease,
    _Refused,
    _Stationd,
)
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit

RESOLUTION = DesktopResolution(width=1600, height=900)
MANIFEST = {"notepad": {}, "rhino-7": {"probe": "tasklist | find rhino"}}
LEASE = {
    "token": "laneB.3." + "t" * 43,
    "lane": "laneB",
    "user": "laneB",
    "home": "C:\\Users\\laneB",
    "generation": 3,
    "address": "127.44.3.9",
    "stage": "C:\\ProgramData\\ALE\\lanes\\laneB",
    "baseline": "sha256:" + "b" * 64,
}


def _hello(**overrides: Any) -> dict[str, Any]:
    return {
        "protocol": stationd.PROTOCOL,
        "stationd": hashlib.sha256(Path(stationd.__file__).read_bytes()).hexdigest(),
        "os": "10.0.20348",
        "framework": station_framework_identity(),
        "resolution": "1600x900",
        "software": MANIFEST,
        "lanes": [
            {"lane": "laneA", "state": "leased", "generation": 1, "session": "1600x900"},
            {"lane": "laneB", "state": "idle", "generation": 3, "session": "1600x900"},
        ],
    } | overrides


class FakeStationd:
    """Answers each request from a queue per operation; records what was asked."""

    destination = "ale-host@ws"

    def __init__(self, **answers: list[dict[str, Any] | Exception]) -> None:
        self.answers = answers
        self.requests: list[str] = []
        self.payloads: list[bytes | None] = []
        self.streams: list[str] = []

    def argv(self, request: str) -> list[str]:
        self.streams.append(request)
        return ["ssh", request]

    async def call(
        self, request: str, *, timeout_sec: float, payload: bytes | None = None
    ) -> dict[str, Any]:
        self.requests.append(request)
        self.payloads.append(payload)
        queue = self.answers[request.split()[0]]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return {"ok": True, **answer}


def provider(stationd_: FakeStationd | None = None, **settings: Any) -> StationProvider:
    station = StationProvider(
        StationProviderConfig(destination="ale-host@ws", **settings), RESOLUTION
    )
    if stationd_ is not None:
        station._stationd = stationd_  # type: ignore[assignment]
    return station


async def request(station: StationProvider, **overrides: Any) -> SandboxRequest:
    base: dict[str, Any] = {
        "episode_id": "episode-1",
        "os": OperatingSystem.WINDOWS,
        "prepared_image": station._resolve("notepad,rhino-7", MANIFEST),
        "resources": Resources(cpus=2, memory_mb=8192),
        "network": NetworkPolicy(mode=NetworkMode.OPEN),
        "desktop_resolution": RESOLUTION,
        "env": {"ALE_EPISODE_ID": "episode-1"},
    }
    return SandboxRequest(**(base | overrides))


# --- configuration and selection ---------------------------------------------------


def test_the_registry_builds_the_station_with_the_runs_resolution() -> None:
    assert "station" in get_args(ProviderId)
    config = RunConfig.model_validate(
        {
            "sandbox": {"providers": ["station"], "station": {"destination": "ale-host@ws"}},
            "desktop_resolution": "1600x900",
        }
    )
    built = ProviderRegistry(config).get("station")
    assert isinstance(built, StationProvider)
    assert built.desktop_resolution == RESOLUTION
    assert built.station == "ws"
    assert built.config.lane_wait_sec == 1800


def test_a_station_needs_its_ssh_destination() -> None:
    with pytest.raises(ValueError, match=r"sandbox\.station\.destination"):
        RunConfig.model_validate({"sandbox": {"providers": ["station"]}})
    with pytest.raises(ConfigError, match=r"sandbox\.station\.destination"):
        ProviderRegistry(RunConfig()).get("station")
    with pytest.raises(ValueError, match="should match pattern"):
        StationProviderConfig(destination="-oProxyCommand=evil")


async def test_a_station_serves_an_open_windows_solver_that_names_software() -> None:
    station = provider()
    solver = await request(station)
    assert await station.unserved(solver.needs, user="user", role=SandboxRole.SOLVER) == ()
    for overrides, reason in (
        ({"network": NetworkPolicy(mode=NetworkMode.BLOCK)}, "network block"),
        ({"resources": Resources(gpus=1)}, "attaches no GPUs"),
        ({"retention": "snapshot"}, "saves and boots no snapshots"),
        ({"os": OperatingSystem.LINUX}, "runs windows, not linux"),
        ({"resources": Resources(sudo=True), "sudo": True}, "never elevates"),
        ({"resources": Resources(storage_mb=1024)}, "limits no writable storage"),
        ({"role": SandboxRole.VERIFIER}, "not a verifier Sandbox"),
        ({"role": SandboxRole.SHARED}, "not a shared Sandbox"),
    ):
        refused = await request(station, **overrides)
        why = await station.unserved(refused.needs, user="user", role=refused.role)
        assert any(reason in item for item in why), (overrides, why)
        with pytest.raises(ProviderCapabilityError):
            await station.create(refused)


# --- preparation and preflight -----------------------------------------------------


async def test_software_resolves_against_the_manifest_into_the_hash_of_its_entries() -> None:
    fake = FakeStationd(hello=[_hello()])
    station = provider(fake)
    prepared = await station.prepare_image(ImageRef(kind=ImageKind.STATION, reference="notepad"))
    assert prepared.prepared_identity == content_hash({"software": {"notepad": {}}})
    assert (prepared.runtime_ref, prepared.resolved_reference) == ("notepad", "station:ws")
    both = await station.prepare_image(
        ImageRef(kind=ImageKind.STATION, reference="notepad,rhino-7")
    )
    assert both.prepared_identity == content_hash({"software": MANIFEST})
    assert await station.prepare_image(both) == both
    with pytest.raises(ProviderCapabilityError, match="has no autocad; its manifest offers"):
        await station.prepare_image(ImageRef(kind=ImageKind.STATION, reference="autocad"))
    with pytest.raises(ProviderCapabilityError, match="sorted and joined by commas"):
        await station.prepare_image(ImageRef(kind=ImageKind.STATION, reference="rhino-7,notepad"))
    with pytest.raises(ProviderCapabilityError, match="not a container image"):
        await station.prepare_image(ImageRef(kind=ImageKind.CONTAINER, reference="busybox"))
    assert fake.requests == ["hello"]  # the manifest is read once per Run


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(station_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(station_module, "station_framework_payload", lambda: b"framework tar")
    monkeypatch.setattr(station_module, "_FRAMEWORK_RETRY_SEC", 0)


@pytest.mark.parametrize(
    ("hello", "error", "message"),
    [
        ({"protocol": 2}, ProviderStartError, "speaks ale-stationd protocol 2"),
        ({"stationd": "0" * 64}, ProviderStartError, "runs another ale-stationd"),
        ({"resolution": "1366x768"}, ProviderCapabilityError, "Lanes are 1366x768"),
        (
            {"lanes": [{"lane": "laneA", "state": "quarantined", "generation": 1}]},
            ProviderStartError,
            "no Lane of the Station at ale-host@ws is in service",
        ),
    ],
)
async def test_preflight_refuses_a_station_this_engine_cannot_drive(
    ssh: None, hello: dict[str, Any], error: type[Exception], message: str
) -> None:
    fake = FakeStationd(hello=[_hello(**hello)])
    with pytest.raises(error, match=message):
        await provider(fake).preflight()
    assert fake.requests == ["hello"]


async def test_preflight_installs_this_engines_framework_when_the_station_runs_another(
    ssh: None,
) -> None:
    fake = FakeStationd(hello=[_hello(framework="sha256:" + "0" * 64)], framework=[{}])
    assert await provider(fake).preflight() is None
    identity = station_framework_identity()
    assert fake.requests == ["hello", f"framework {identity} {len(b'framework tar')}"]
    assert fake.payloads[-1] == b"framework tar"
    current = FakeStationd(hello=[_hello()])
    await provider(current).preflight()
    assert current.requests == ["hello"]


async def test_a_framework_push_waits_out_another_and_never_replaces_a_leased_ones(
    ssh: None,
) -> None:
    other = _hello(framework="sha256:" + "0" * 64)
    busy = _Refused("framework-busy", "another framework push is running")
    # Another Run of this engine pushed the same framework meanwhile.
    fake = FakeStationd(hello=[other, _hello()], framework=[busy])
    await provider(fake).preflight()
    assert [request.split()[0] for request in fake.requests] == ["hello", "framework", "hello"]
    leased = _Refused("lanes-leased", "laneA leased; push when no Lane is leased")
    with pytest.raises(ProviderStartError, match="laneA leased"):
        await provider(FakeStationd(hello=[other], framework=[leased])).preflight()


# --- leases -------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    async def exec(self, argv: Any, **options: Any) -> tuple[int, str, str, bool]:
        self.calls.append(("exec", (list(argv), options)))
        return 0, "ok", "", False

    async def write_file(self, path: str, data: bytes, *, run_as: str | None = None) -> None:
        self.calls.append(("write_file", (path, run_as)))

    async def upload_dir(self, source: str, target: str, *, run_as: str | None = None) -> None:
        self.calls.append(("upload_dir", (target, run_as)))

    async def close(self) -> None:
        self.closed = True


class FakeTunnel:
    started: ClassVar[list[list[str]]] = []

    def __init__(self, argv: Any, *, targets: list[tuple[str, int]]) -> None:
        self.argv = argv
        self.targets = targets
        self.closed = False

    async def start(self) -> None:
        FakeTunnel.started.append(await self.argv())

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A Lane whose guestd sessions, privileges and desktop answer as a Station's do."""
    seen: dict[str, Any] = {"clients": [], "identities": []}

    async def connect(stationd_: Any, lease: _Lease) -> FakeClient:
        stationd_.argv(f"guestd {lease.token}")
        client = FakeClient()
        seen["clients"].append(client)
        return client

    async def identities(client: Any, agent_user: str, *, sudo: bool) -> None:
        seen["identities"].append((agent_user, sudo))

    async def desktop(client: Any) -> None:
        return None

    async def resolution(client: Any, requested: DesktopResolution) -> DesktopResolution:
        return requested

    FakeTunnel.started = []
    monkeypatch.setattr(station_module, "_connect", connect)
    monkeypatch.setattr(station_module, "verify_windows_identities", identities)
    monkeypatch.setattr(station_module, "_await_desktop", desktop)
    monkeypatch.setattr(station_module, "require_resolution", resolution)
    monkeypatch.setattr(station_module, "Tunnel", FakeTunnel)
    monkeypatch.setattr(station_module, "_LEASE_RETRY_SEC", 0)
    return seen


async def test_a_lease_becomes_a_sandbox_that_records_the_lane_it_ran_in(
    lane: dict[str, Any],
) -> None:
    fake = FakeStationd(lease=[LEASE], release=[{"released": True}])
    station = provider(fake)
    solver = await request(station, gateway_url="http://0.0.0.0:41000/v1")
    sandbox = await station.create(solver)
    assert isinstance(sandbox, StationSandbox)
    assert fake.requests == [
        f"lease {station_framework_identity()} 1600x900 notepad rhino-7",
    ]
    assert (sandbox.agent_user, sandbox.agent_home) == ("laneB", "C:\\Users\\laneB")
    assert sandbox.stage_root == "C:\\ProgramData\\ALE\\lanes\\laneB"
    assert sandbox.resolved_image.observed_ref == "station:ws/laneB@3"
    assert sandbox.resolved_image.observed_identity == content_hash(
        {"software": solver.prepared_image.prepared_identity, "baseline": LEASE["baseline"]}
    )
    assert sandbox.allocation.model_dump(exclude_none=True) == {
        "cpus": 2,
        "memory_mb": 8192,
        "sudo": False,
        "network_mode": NetworkMode.OPEN,
        "provider": "station",
        "leaf": "station",
        "desktop_resolution": {"width": 1600, "height": 900},
    }
    # The Gateway arrives at the lease's own loopback address, at the fixed port.
    assert sandbox.gateway_url == "http://127.44.3.9:7412/v1"
    assert FakeTunnel.started == [["ssh", f"tunnel {LEASE['token']} 7412:0"]]
    assert fake.streams.count(f"guestd {LEASE['token']}") == 2
    assert lane["identities"] == [("laneB", False)]
    # Every request the Provider sends is one ale-stationd parses.
    for line in [*fake.requests, *fake.streams]:
        stationd.parse_request(line)

    await sandbox.destroy()
    await sandbox.destroy()
    assert fake.requests[1:] == [f"release {LEASE['token']}"]
    assert all(client.closed for client in lane["clients"])


async def test_a_busy_station_is_asked_again_until_the_lane_wait_runs_out(
    lane: dict[str, Any],
) -> None:
    busy = _Refused("busy", "no Lane is free at this size; try again")
    fake = FakeStationd(lease=[busy, busy, LEASE])
    station = provider(fake, lane_wait_sec=60)
    await station.create(await request(station))
    assert [request.split()[0] for request in fake.requests] == ["lease"] * 3

    waited = FakeStationd(lease=[busy])
    station = provider(waited, lane_wait_sec=0)
    with pytest.raises(ProviderCapacityError, match=r"lane_wait_sec \(0s\)"):
        await station.create(await request(station))
    assert waited.requests == [f"lease {station_framework_identity()} 1600x900 notepad rhino-7"]


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("unknown-software", ProviderCapabilityError),
        ("resolution", ProviderCapabilityError),
        ("framework-mismatch", ProviderStartError),
        ("probe-failed", ProviderStartError),
        ("failed", ProviderStartError),
    ],
)
async def test_a_refused_lease_fails_the_sandbox_with_the_stations_reason(
    lane: dict[str, Any], code: str, error: type[Exception]
) -> None:
    fake = FakeStationd(lease=[_Refused(code, "the Station's reason")])
    station = provider(fake)
    with pytest.raises(error, match="the Station's reason"):
        await station.create(await request(station))
    assert [request.split()[0] for request in fake.requests] == ["lease"]


async def test_a_start_that_fails_after_the_lease_releases_it(
    lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def limited(client: Any, agent_user: str, *, sudo: bool) -> None:
        raise ProviderCapabilityError("the Windows agent must run with a limited token")

    monkeypatch.setattr(station_module, "verify_windows_identities", limited)
    fake = FakeStationd(lease=[LEASE], release=[{"released": True}])
    station = provider(fake)
    with pytest.raises(ProviderCapabilityError, match="limited token"):
        await station.create(await request(station))
    assert fake.requests[-1] == f"release {LEASE['token']}"
    assert lane["clients"] and all(client.closed for client in lane["clients"])


async def test_a_release_that_does_not_arrive_is_left_to_the_leases_grace(
    lane: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeStationd(lease=[LEASE], release=[ProviderStartError("ssh: connection refused")])
    station = provider(fake)
    sandbox = await station.create(await request(station))
    with caplog.at_level(logging.WARNING, logger="ale.execution"):
        await sandbox.destroy()
    assert "could not release Lane laneB" in caplog.text
    assert f"{stationd.GRACE_SEC:g}s" in caplog.text


async def test_what_the_provider_writes_for_the_agent_no_other_lane_may_open(
    lane: dict[str, Any],
) -> None:
    fake = FakeStationd(lease=[LEASE], release=[{}])
    station = provider(fake)
    sandbox = await station.create(await request(station))
    client = lane["clients"][0]
    await sandbox.upload_dir(
        "/host/tree", "C:\\Users\\laneB\\.ale-mcp\\tool", identity=Identity.AGENT
    )
    await sandbox.upload_dir("/host/setup", "C:\\ProgramData\\ALE\\lanes\\laneB\\setup")
    await sandbox.write_file("C:\\Users\\laneB\\prompt.md", b"x", identity=Identity.AGENT)
    await sandbox.write_file("C:\\ProgramData\\ALE\\lanes\\laneB\\agent-episode", b"e")
    resets = [argv[-1] for kind, (argv, _) in client.calls if kind == "exec"]
    assert len(resets) == 3  # both uploads and the agent's file; the stage keeps its own
    for script in resets:
        assert "$private = $true" in script
    assert "$agent = 'laneB'" in resets[0] and "$agent = ''" in resets[1]
    assert "$agent = 'laneB'" in resets[2]
    await sandbox.destroy()


async def test_commands_see_the_requests_variables_and_the_gateway(lane: dict[str, Any]) -> None:
    fake = FakeStationd(lease=[LEASE], release=[{}])
    station = provider(fake)
    sandbox = await station.create(await request(station, gateway_url="http://127.0.0.1:41000"))
    await sandbox.exec(["python.exe", "-V"], env={"OWN": "1"}, identity=Identity.AGENT)
    _, (_, options) = lane["clients"][0].calls[-1]
    assert options["run_as"] == "laneB"
    assert options["env"] == {
        "ALE_EPISODE_ID": "episode-1",
        "ALE_GATEWAY_URL": "http://127.44.3.9:7412",
        "OWN": "1",
    }
    await sandbox.close_egress()  # open: nothing to seal
    await sandbox.destroy()


# --- the Host's end of ale-stationd, through real processes ---------------------------


class LocalStationd(_Stationd):
    """Runs ``script`` with the request as its argument where SSH would run the client."""

    def __init__(self, script: str) -> None:
        super().__init__("ale-host@ws")
        self.script = script

    def argv(self, request: str) -> list[str]:
        return [sys.executable, "-c", self.script, request]


def test_every_request_is_an_ssh_session_of_its_own_that_asks_nothing() -> None:
    argv = _Stationd("ale-host@ws").argv("hello")
    assert argv[:2] == ["ssh", "-T"] and argv[-2:] == ["ale-host@ws", "hello"]
    options = {argv[index + 1] for index, word in enumerate(argv) if word == "-o"}
    assert {"BatchMode=yes", "ControlPath=none", "ClearAllForwardings=yes"} <= options


async def test_a_reply_is_its_first_line_and_a_refusal_carries_its_code() -> None:
    answer = 'import json, sys; print(json.dumps({"ok": True, "asked": sys.argv[1]}))'
    assert (await LocalStationd(answer).call("hello", timeout_sec=30))["asked"] == "hello"
    refuse = 'print(\'{"ok": false, "error": "busy", "message": "no Lane"}\'); raise SystemExit(1)'
    with pytest.raises(_Refused) as refused:
        await LocalStationd(refuse).call("lease x 1600x900", timeout_sec=30)
    assert (refused.value.code, refused.value.message) == ("busy", "no Lane")
    count = 'import sys; print(\'{"ok": true, "read": %d}\' % len(sys.stdin.buffer.read()))'
    payload = b"\0" * 300_000
    reply = await LocalStationd(count).call("framework x 1", payload=payload, timeout_sec=30)
    assert reply["read"] == len(payload)


async def test_a_station_that_does_not_answer_says_why_without_the_token() -> None:
    silent = "import sys; sys.stderr.write('ale-stationd: ale-stationd is not running'); exit(2)"
    with pytest.raises(ProviderStartError, match=r"did not answer release: .*not running") as err:
        await LocalStationd(silent).call("release laneA.1.secret", timeout_sec=30)
    assert "secret" not in str(err.value)
    hung = "import time; time.sleep(30)"
    with pytest.raises(ProviderStartError, match=r"did not answer hello within 0\.5s"):
        await LocalStationd(hung).call("hello", timeout_sec=0.5)


async def test_an_ssh_that_timed_out_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Killed and waited for, so that no process outlives the event loop that started it."""
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def spy(*argv: str, **options: Any) -> asyncio.subprocess.Process:
        process = await spawn(*argv, **options)
        started.append(process)
        return process

    monkeypatch.setattr(station_module.asyncio, "create_subprocess_exec", spy)
    with pytest.raises(ProviderStartError):
        await LocalStationd("import time; time.sleep(30)").call("hello", timeout_sec=0.5)
    assert started[0].returncode is not None


def test_a_lease_request_waits_as_long_as_its_probes_and_the_stations_wait() -> None:
    """A slow probe on a busy Station is no infrastructure failure, and leaves no Lane
    leased that nobody holds."""
    budget = stationd.PROBE_TIMEOUT_SEC + stationd.LEASE_WAIT_SEC
    assert budget < station_module._LEASE_SEC


async def test_a_guestd_that_serves_another_account_is_refused() -> None:
    """guestd itself, on this machine: it serves this account on Linux, not the Lane."""

    class LocalGuestd(_Stationd):
        def argv(self, request: str) -> list[str]:
            return [sys.executable, "-I", str(guestd.SOURCE / "main.py"), "--stdio"]

    lease = _Lease.read(LEASE)
    with pytest.raises(ProviderStartError, match=r"not laneB at C:\\Users\\laneB"):
        await asyncio.wait_for(station_module._connect(LocalGuestd("ale-host@ws"), lease), 60)


def test_a_lease_reply_without_its_fields_is_refused() -> None:
    with pytest.raises(ProviderStartError, match="without 'stage'"):
        _Lease.read({key: value for key, value in LEASE.items() if key != "stage"})


def test_the_fixture_task_is_a_software_task_verified_in_a_linux_container() -> None:
    """``tests/fixtures/tasks/station-notepad``, which a live Station validates."""
    folder = Path(__file__).resolve().parents[1] / "fixtures/tasks/station-notepad"
    task = load_task_folder(folder)
    assert lint_repository(folder) == []
    assert (task.spec.image.kind, task.spec.image.software) == (ImageKind.STATION, ("notepad",))
    assert (task.spec.verifier_os, task.spec.network.mode) == (
        OperatingSystem.LINUX,
        NetworkMode.OPEN,
    )
    assert [artifact.source for artifact in task.spec.artifacts] == ["output"]
