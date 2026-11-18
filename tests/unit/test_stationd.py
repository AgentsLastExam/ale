"""ale-stationd's leases, resets and requests, with every Windows call answered by a fake."""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import threading
from pathlib import Path

import pytest

from ale.run.providers.station import stationd
from ale.run.providers.station.stationd import (
    IDLE,
    LEASED,
    QUARANTINED,
    RESETTING,
    Config,
    Deny,
    Entry,
    Request,
    Session,
    Station,
    StationError,
)

pytestmark = pytest.mark.unit

FRAMEWORK = "sha256:" + "f" * 64
GROUP = "S-1-5-21-1-2-3-1100"
SIDS = {"laneA": "S-1-5-21-1-2-3-1001", "laneB": "S-1-5-21-1-2-3-1002", "ALE Lanes": GROUP}
PROFILE_SDDL = (
    "O:S-1-5-21-1-2-3-1001D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-21-1-2-3-1001)"
)


def _baseline_entries() -> dict[str, Entry]:
    return {
        "": Entry(PROFILE_SDDL),
        "NTUSER.DAT": Entry(PROFILE_SDDL),
        "My Documents": Entry(PROFILE_SDDL, "junction:C:\\Users\\laneA\\Documents"),
        "Cookies": Entry(PROFILE_SDDL, "junction:C:\\Users\\laneA\\AppData\\Cookies"),
    }


def _recipe_sddl(path: str) -> str:
    for deny in stationd.recipe("C:", ["C:\\"]):
        if deny.path == path:
            flags = "OICI" if deny.inherited else ""
            return f"O:SYD:(A;;FA;;;SY)(D;{flags};{deny.rights:#x};;;{GROUP})"
    return "O:SYD:(A;;FA;;;SY)"


class FakeChild:
    def __init__(self, output: bytes = b"") -> None:
        self.output = io.BytesIO(output)
        self.received = b""
        self.killed = False
        self.input_closed = threading.Event()
        self.running = True

    def read(self, size: int) -> bytes:
        return self.output.read(size)

    def write(self, data: bytes) -> None:
        self.received += data

    def close_input(self) -> None:
        self.input_closed.set()

    def wait(self, timeout: float) -> bool:
        return True

    def alive(self) -> bool:
        return self.running

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def close(self) -> None:
        pass


PRIVATE = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


class FakeSocket:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.closed = False

    def fileno(self) -> int:
        return self.descriptor

    def close(self) -> None:
        self.closed = True


class FakeJob:
    closed = False

    def close(self) -> None:
        self.closed = True


class FakeWindows:
    """A Station's Windows as the service sees it; ``calls`` records what changed it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.sessions_now = [
            Session(2, "laneA", True, 1600, 900),
            Session(3, "laneB", True, 1600, 900),
        ]
        self.running: dict[str, list[int]] = {}
        self.hive_until_polls = 0
        self.profile_entries = {
            "laneA": dict(_baseline_entries()),
            "laneB": {k: Entry(v.security.replace("1001", "1002"), v.link.replace("laneA", "laneB"))
                      for k, v in _baseline_entries().items()},
        }  # fmt: skip
        self.owned_entries: dict[str, list[str]] = {}
        self.scheduled: dict[str, list[str]] = {}
        self.stage_files: dict[str, list[str]] = {}
        self.securities: dict[str, str] = {}
        self.robocopy_codes: list[int] = []
        self.probe_code = 0
        self.children: list[FakeChild] = []
        self.pipes: set[str] = set()
        self.stubborn_tasks = False
        self.next_session = 10
        self.jobs: list[FakeJob] = []
        self.path = ["C:\\Windows\\system32"]
        self.missing: set[str] = set()
        self.listeners: list[FakeSocket] = []
        self.retired: dict[str, dict[str, Entry]] = {}

    def _lane(self, sid: str) -> str:
        return next(name for name, value in SIDS.items() if value == sid)

    def sid(self, account: str) -> str:
        return SIDS[account]

    def profile(self, sid: str) -> str | None:
        return f"C:\\Users\\{self._lane(sid)}"

    def hive_loaded(self, sid: str) -> bool:
        self.calls.append(("hive", self._lane(sid)))
        self.hive_until_polls -= 1
        return self.hive_until_polls >= 0

    def set_enabled(self, account: str, enabled: bool) -> None:
        """An enabled Lane is signed in again by its keep-alive connection."""
        self.calls.append(("enabled", account, enabled))
        if enabled and not any(s.user == account for s in self.sessions_now):
            self.next_session += 1
            self.sessions_now.append(Session(self.next_session, account, True, 1600, 900))

    def sessions(self) -> list[Session]:
        return list(self.sessions_now)

    def logoff(self, session: int) -> None:
        self.calls.append(("logoff", session))
        self.sessions_now = [s for s in self.sessions_now if s.id != session]

    def processes(self, sid: str) -> list[int]:
        return list(self.running.get(self._lane(sid), []))

    def kill(self, pid: int) -> None:
        self.calls.append(("kill", pid))
        for pids in self.running.values():
            if pid in pids:
                pids.remove(pid)

    def environment(self, session: int) -> dict[str, str]:
        """The Lane's own environment, which only what runs as the Lane may have."""
        return {
            "USERPROFILE": "C:\\Users\\laneA",
            "APPDATA": "C:\\Users\\laneA\\AppData\\Roaming",
            "TEMP": "C:\\Users\\laneA\\AppData\\Local\\Temp",
            "Path": "C:\\Users\\laneA\\bin;C:\\Windows",
        }

    def job(self) -> FakeJob:
        job = FakeJob()
        self.jobs.append(job)
        return job

    def start_guestd(self, session, command, environment, output, job) -> FakeChild:
        assert job is self.jobs[-2]  # the lease's job for its Lane's session
        self.calls.append(("guestd", session, command, environment))
        return self._child()

    def start_tunnel(self, command, output, job, sockets) -> FakeChild:
        assert job is self.jobs[-1]  # its job for session 0: a job holds one session's
        self.calls.append(("tunnel", command, [listener.fileno() for listener in sockets]))
        return self._child()

    def listen(self, address: str, port: int) -> FakeSocket:
        self.calls.append(("listen", address, port))
        listener = FakeSocket(100 + len(self.listeners))
        self.listeners.append(listener)
        return listener

    def purge_credentials(self, session: int, since: float) -> list[str]:
        self.calls.append(("purge_credentials", session, since))
        return []

    def start_driver(self, session, command) -> FakeChild:
        self.calls.append(("driver", session, command))
        return self._child()

    def _child(self) -> FakeChild:
        child = FakeChild()
        self.children.append(child)
        return child

    def pipe_exists(self, name: str) -> bool:
        return name in self.pipes

    def run(self, command, timeout) -> tuple[int, str]:
        self.calls.append(("run", command))
        return self.probe_code, "license server down" if self.probe_code else ""

    def system_drive(self) -> str:
        return "C:"

    def volumes(self) -> list[str]:
        return ["C:\\"]

    def machine_path(self) -> list[str]:
        return list(self.path)

    def exists(self, path: str) -> bool:
        return path not in self.missing

    def listdir(self, path: str) -> list[str]:
        return self.stage_files.get(path, [])

    def security(self, path: str) -> str:
        if path in self.securities:
            return self.securities[path]
        if "lanes" in path:
            return "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        return _recipe_sddl(path)

    def apply_security(self, path: str, sddl: str) -> None:
        self.calls.append(("apply_security", path))
        lane, _, relative = path.removeprefix("C:\\Users\\").partition("\\")
        entries = self.profile_entries[lane]
        entries[relative] = Entry(sddl, entries[relative].link, entries[relative].streams)

    def snapshot(self, root: str) -> dict[str, Entry]:
        return dict(self.profile_entries[root.rsplit("\\", 1)[-1]])

    def owned(self, root: str, below: bool, skip: set[str], sid: str) -> list[str]:
        top = root.rstrip("\\")
        return [
            path
            for path in self.owned_entries.get(self._lane(sid), [])
            if (path.startswith(top + "\\") if below else path.rsplit("\\", 1)[0] == top)
        ]

    def remove(self, path: str) -> None:
        self.calls.append(("remove", path))
        if Path(path).is_absolute() and Path(path).exists():
            shutil.rmtree(path)
        for paths in self.owned_entries.values():
            if path in paths:
                paths.remove(path)
        for lane, entries in self.profile_entries.items():
            relative = path.removeprefix(f"C:\\Users\\{lane}\\")
            if relative != path:
                entries.pop(relative, None)

    def retire(self, path: str, deadline: float) -> str:
        """Move the profile aside: from now on nothing the Lane left is in it."""
        lane = path.removeprefix("C:\\Users\\")
        self.calls.append(("retire", lane))
        self.retired[lane] = self.profile_entries.pop(lane)
        return f"{path}.ale-old"

    def make_junction(self, path: str, target: str) -> None:
        self.calls.append(("junction", path, target))
        lane, _, relative = path.removeprefix("C:\\Users\\").partition("\\")
        self.profile_entries[lane][relative] = Entry("O:SYD:", f"junction:{target}")

    def make_private(self, path: str) -> None:
        self.calls.append(("private", path))
        lane = path.removeprefix("C:\\Users\\")
        if lane in self.retired:
            self.profile_entries[lane] = {"": Entry(PRIVATE)}
        self.stage_files.pop(path, None)

    def icacls(self, path: str, *arguments: str) -> None:
        self.calls.append(("icacls", path, *arguments))

    def lock_framework(self, root: str) -> None:
        self.calls.append(("lock_framework",))

    def robocopy(self, arguments: list[str]) -> int:
        self.calls.append(("robocopy", "/L" in arguments))
        profile = arguments[1]
        lane = profile.rsplit("\\", 1)[-1]
        if "/L" not in arguments and lane in self.retired:
            # Purging, robocopy deletes through a junction what it points to: a restore only
            # ever copies, into a profile nothing but the reset has opened.
            assert "/MIR" not in arguments and "/PURGE" not in arguments
            assert self.profile_entries[lane] == {"": Entry(PRIVATE)}
            record = json.loads((Path(arguments[0]).parent / "record.json").read_text())
            entries = stationd.entries_from_json(record["entries"])
            copied = {path: Entry(PRIVATE, "", e.streams) for path, e in entries.items()}
            self.profile_entries[lane] = {p: e for p, e in copied.items() if not entries[p].link}
        return self.robocopy_codes.pop(0) if self.robocopy_codes else 0

    def tasks(self, account: str, sid: str) -> list[str]:
        return list(self.scheduled.get(account, []))

    def delete_tasks(self, account: str, sid: str) -> None:
        self.calls.append(("delete_tasks", account))
        if not self.stubborn_tasks:
            self.scheduled.pop(account, None)

    def os_build(self) -> str:
        return "10.0.20348"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _write_baseline(root, lane: str, entries: dict[str, Entry]) -> None:
    folder = root / "station" / "baselines" / lane
    (folder / "profile").mkdir(parents=True)
    (folder / "profile" / "NTUSER.DAT").write_bytes(b"hive")
    record = {"profile": f"C:\\Users\\{lane}", "digest": "sha256:base", "captured": 900.0,
              "entries": stationd.entries_json(entries)}  # fmt: skip
    (folder / "record.json").write_text(json.dumps(record))
    (folder / "digest").write_text("sha256:base\n")


@pytest.fixture
def windows() -> FakeWindows:
    return FakeWindows()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def station(tmp_path, windows, clock) -> Station:
    config = Config(
        ("laneA", "laneB"), (1600, 900), {"rhino-7": {"probe": "lmstat"}, "notepad": {}}
    )
    (tmp_path / "framework").write_text(FRAMEWORK + "\n")
    for lane in config.lanes:
        _write_baseline(tmp_path, lane, windows.profile_entries[lane])
    station = Station(
        config, windows, tmp_path, digest="d" * 64, clock=clock, sleep=lambda seconds: None,
        background=lambda function: function(), log=lambda message: None, lease_wait=0,
    )  # fmt: skip
    station.recover()
    for lane in config.lanes:
        station.reset(lane, None)
    windows.calls.clear()
    return station


def _lease(station: Station, *software: str) -> dict:
    return station.lease(stationd.parse_request(f"lease {FRAMEWORK} 1600x900 {' '.join(software)}"))


# --- requests ---


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("hello", Request("hello")),
        (f"framework {FRAMEWORK} 4096", Request("framework", framework=FRAMEWORK, size=4096)),
        (
            f"lease {FRAMEWORK} 1600x900 rhino-7",
            Request("lease", framework=FRAMEWORK, resolution=(1600, 900), software=("rhino-7",)),
        ),
        (
            "tunnel laneA.3." + "a" * 43 + " 7412:0 7413:1",
            Request("tunnel", token="laneA.3." + "a" * 43, listeners=((7412, 0), (7413, 1))),
        ),
    ],
)
def test_requests_parse(line, expected):
    assert stationd.parse_request(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello extra",
        "format c:",
        f"framework {FRAMEWORK} -1",
        f"lease {FRAMEWORK} big",
        f"lease {FRAMEWORK} 1600x900 Rhino;calc",
        "guestd laneA.3.short",
        "release ../laneA.3." + "a" * 43,
        "tunnel laneA.3." + "a" * 43 + " 70000:0",
        "tunnel laneA.3." + "a" * 43,
    ],
)
def test_malformed_requests_are_refused(line):
    with pytest.raises(StationError) as refused:
        stationd.parse_request(line)
    assert refused.value.code == "bad-request"


# --- leases ---


def test_a_lease_takes_an_idle_lane_whose_session_is_active_at_the_size(station, windows):
    windows.sessions_now = [Session(3, "laneB", True, 1600, 900), Session(2, "laneA", False, 0, 0)]
    lease = _lease(station, "notepad")
    assert lease["lane"] == "laneB"
    assert lease["user"] == "laneB"
    assert lease["home"] == "C:\\Users\\laneB"
    assert lease["generation"] == 1
    assert re.fullmatch(r"127\.\d+\.\d+\.\d+", lease["address"])
    assert lease["baseline"] == "sha256:base"
    assert lease["stage"].endswith("laneB")
    assert lease["token"].startswith("laneB.1.")
    assert station.lanes["laneB"].state == LEASED
    driver = next(call for call in windows.calls if call[0] == "driver")
    assert driver[1] == 3  # in the Lane's own session
    assert re.search(r"--socket \\\\\.\\pipe\\ale-cua-driver-laneB-[0-9a-f]{16} ", driver[2])


def test_a_lease_first_deletes_credentials_written_since_the_baseline(station, windows):
    # Windows keeps a Lane's stored credentials in memory past its sign-out, so restoring
    # the profile's files does not take back one the last agent stored.
    session = next(s.id for s in windows.sessions_now if s.user == _lease(station)["lane"])
    order = [call[0] for call in windows.calls]
    assert windows.calls[order.index("purge_credentials")] == ("purge_credentials", session, 900.0)
    assert order.index("purge_credentials") < order.index("driver")


def test_a_lease_is_refused_what_the_station_cannot_give(station, windows):
    with pytest.raises(StationError, match="no adobe") as refused:
        _lease(station, "adobe")
    assert refused.value.code == "unknown-software"
    with pytest.raises(StationError) as refused:
        station.lease(stationd.parse_request(f"lease {FRAMEWORK} 1920x1080"))
    assert refused.value.code == "resolution"
    with pytest.raises(StationError) as refused:
        station.lease(stationd.parse_request(f"lease sha256:{'0' * 64} 1600x900"))
    assert refused.value.code == "framework-mismatch"
    windows.probe_code = 1
    with pytest.raises(StationError, match="license server down") as refused:
        _lease(station, "rhino-7")
    assert refused.value.code == "probe-failed"
    assert {lane.state for lane in station.lanes.values()} == {IDLE}


def test_a_lease_is_busy_when_no_lane_is_free(station):
    _lease(station)
    _lease(station)
    with pytest.raises(StationError) as refused:
        _lease(station)
    assert refused.value.code == "busy"


def _driver_pipe(windows: FakeWindows) -> str:
    command = [call for call in windows.calls if call[0] == "driver"][-1][2]
    return re.search(r"pipe\\(\S+) ", command)[1]


def test_each_lease_names_its_driver_pipe_at_random(station, windows):
    """No account can take a pipe name before the Driver does, and guestd is told it."""
    token = _lease(station)["token"]
    first = _driver_pipe(windows)
    station.attach(stationd.parse_request(f"guestd {token}"))
    guestd = next(call for call in windows.calls if call[0] == "guestd")
    assert guestd[3]["ALE_CUA_DRIVER_SOCKET"] == f"\\\\.\\pipe\\{first}"
    station.release(token)
    _lease(station)
    assert _driver_pipe(windows) != first
    assert _driver_pipe(windows).startswith("ale-cua-driver-laneA-")


def test_a_driver_that_does_not_start_again_ends_the_lease(station, windows):
    """A Driver's pipe that went free when it exited may be another account's by now."""
    _lease(station)
    windows.children[0].running = False
    windows.pipes.add(_driver_pipe(windows))  # taken meanwhile
    for _ in range(stationd.DRIVER_STARTS - 1):
        station.tick()
        assert station.lanes["laneA"].state == LEASED
    station.tick()
    assert station.lanes["laneA"].state == IDLE  # reset, a generation on
    assert station.lanes["laneA"].generation == 2


def test_a_lease_lives_while_a_stream_is_open_and_for_the_grace_after(station, windows, clock):
    token = _lease(station)["token"]
    lease, child = station.attach(stationd.parse_request(f"guestd {token}"))
    guestd = next(call for call in windows.calls if call[0] == "guestd")
    assert guestd[2].endswith('main.py" --stdio')
    assert guestd[3]["ALE_AGENT_USER"] == "laneA"
    assert guestd[3]["ALE_AGENT_HOME"] == "C:\\Users\\laneA"
    assert guestd[3]["ALE_AGENT_SUDO"] == "0"
    clock.now += 10 * stationd.GRACE_SEC
    station.tick()
    assert station.lanes["laneA"].state == LEASED
    station.detach(lease, child)
    clock.now += stationd.GRACE_SEC - 1
    station.tick()
    assert station.lanes["laneA"].state == LEASED
    clock.now += 1
    station.tick()
    assert station.lanes["laneA"].state == IDLE
    assert station.lanes["laneA"].generation == 2


def test_guestd_runs_with_the_services_environment_never_the_lanes(station, windows, tmp_path):
    """guestd runs as SYSTEM: with the Lane's APPDATA, PATH or TEMP, its Python would load a
    user site, and its programs files, from directories the Lane writes to."""
    token = _lease(station)["token"]
    station.attach(stationd.parse_request(f"guestd {token}"))
    environment = next(call for call in windows.calls if call[0] == "guestd")[3]
    lanes = windows.environment(2)
    assert not [key for key, value in environment.items() if lanes.get(key) == value]
    assert not [value for value in environment.values() if "laneA\\AppData" in value]
    temp = str(tmp_path / "temp" / "laneA")
    assert environment["TEMP"] == environment["TMP"] == temp
    assert ("private", temp) in windows.calls  # which no Lane enters


def test_a_tunnel_accepts_on_sockets_the_lease_holds(station, windows):
    """Bound once for the lease: between two tunnels no other account can take the port."""
    lease = _lease(station)
    for _ in range(2):  # the Provider starts a tunnel again when its SSH session drops
        station.attach(stationd.parse_request(f"tunnel {lease['token']} 7412:0 7413:1"))
    assert [call for call in windows.calls if call[0] == "listen"] == [
        ("listen", lease["address"], 7412),
        ("listen", lease["address"], 7413),
    ]
    tunnels = [call for call in windows.calls if call[0] == "tunnel"]
    assert all(call[1].endswith("--accept 100:0 --accept 101:1") for call in tunnels)
    assert all(call[2] == [100, 101] for call in tunnels)
    assert not any(listener.closed for listener in windows.listeners)
    station.release(lease["token"])
    assert all(listener.closed for listener in windows.listeners)


def test_leases_at_once_have_loopback_addresses_of_their_own(station):
    assert _lease(station)["address"] != _lease(station)["address"]


def test_a_token_dies_with_its_lease(station, windows):
    token = _lease(station)["token"]
    assert station.release(token) == {"released": True}
    assert station.lanes["laneA"].state == IDLE
    assert station.lanes["laneA"].generation == 2
    assert station.release(token) == {"released": False}
    with pytest.raises(StationError) as refused:
        station.attach(stationd.parse_request(f"guestd {token}"))
    assert refused.value.code == "unknown-lease"
    # The next lease of the same Lane has a token of the next generation.
    assert _lease(station)["token"].startswith("laneA.2.")


def test_a_forged_token_of_a_live_lease_is_refused(station):
    token = _lease(station)["token"]
    forged = token[:-1] + ("y" if token[-1] != "y" else "z")
    with pytest.raises(StationError):
        station.attach(stationd.parse_request(f"guestd {forged}"))
    assert station.release(forged) == {"released": False}
    assert station.lanes["laneA"].state == LEASED


def test_releasing_ends_what_the_lease_runs(station, windows):
    token = _lease(station)["token"]
    station.attach(stationd.parse_request(f"guestd {token}"))
    station.release(token)
    assert all(child.killed for child in windows.children)
    assert windows.jobs[-2].closed and windows.jobs[-1].closed  # what they started too


def test_a_dead_driver_is_started_again(station, windows):
    _lease(station)
    windows.children[0].running = False
    station.tick()
    assert sum(call[0] == "driver" for call in windows.calls) == 2


def test_the_probes_of_a_lease_share_one_budget(station, windows, clock):
    """The Provider waits for a lease as long as its probes and its wait may take."""
    station.config.software["notepad"]["probe"] = "where notepad"
    timeouts = []

    def run(command, timeout):
        timeouts.append(timeout)
        clock.now += 50
        return 0, ""

    windows.run = run
    _lease(station, "notepad", "rhino-7")
    assert timeouts == [stationd.PROBE_TIMEOUT_SEC, stationd.PROBE_TIMEOUT_SEC - 50]


def test_one_lanes_failure_in_a_tick_stops_nothing(station, windows, clock):
    _lease(station)
    _lease(station)
    clock.now += stationd.GRACE_SEC
    saves = []

    def save():
        saves.append(1)
        if len(saves) == 1:
            raise PermissionError("the store is held open")

    station._save = save
    station.tick()  # does not raise
    assert len(saves) > 1  # the next Lane was ended all the same


def test_the_store_is_replaced_once_a_reader_lets_go(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{}")
    real, attempts = stationd.os.replace, []

    def replace(source, target):
        attempts.append(source)
        if len(attempts) < 3:
            raise PermissionError("sharing violation")
        real(source, target)

    monkeypatch.setattr(stationd.os, "replace", replace)
    stationd.save_state(path, {"laneA": stationd.Lane("laneA", 0)}, lambda seconds: None)
    assert len(attempts) == 3
    assert json.loads(path.read_text())["lanes"]["laneA"]["state"] == RESETTING


# --- the store and recovery ---


def test_the_store_survives_and_a_restart_resets_what_it_cannot_vouch_for(tmp_path, windows):
    config = Config(("laneA", "laneB", "laneC", "laneD", "laneE"), (1600, 900), {})
    (tmp_path / "station").mkdir()
    saved = {
        "laneA": {"state": LEASED, "generation": 4, "token": "x", "sessions": 2},
        "laneB": {"state": RESETTING, "generation": 7},
        "laneC": {"state": QUARANTINED, "generation": 1, "reason": "hive stayed loaded"},
        "laneD": {"state": IDLE, "generation": 9},
    }
    (tmp_path / "station" / "state.json").write_text(json.dumps({"lanes": saved}))
    station = Station(config, windows, tmp_path, digest="d", log=lambda message: None)
    assert station.recover() == ["laneA", "laneB", "laneE"]
    lanes = station.lanes
    assert (lanes["laneA"].state, lanes["laneA"].generation, lanes["laneA"].token) == (
        RESETTING, 4, "",
    )  # fmt: skip
    assert lanes["laneA"].sessions == 0
    assert (lanes["laneC"].state, lanes["laneC"].reason) == (QUARANTINED, "hive stayed loaded")
    assert (lanes["laneD"].state, lanes["laneD"].generation) == (IDLE, 9)
    stored = json.loads((tmp_path / "station" / "state.json").read_text())["lanes"]
    assert stored["laneA"]["state"] == RESETTING
    assert not (tmp_path / "station" / "state.json.new").exists()


# --- reset and check ---


def test_the_reset_makes_the_profile_anew_and_cleans_up_before_the_check(station, windows):
    lane = "laneA"
    entries = windows.profile_entries[lane]
    entries["AppData"] = Entry(PROFILE_SDDL.replace("(A;OICI;FA;;;BA)", "(A;OICI;FA;;;WD)"))
    entries["NTUSER.DAT"] = Entry("O:SYD:(A;;FA;;;WD)", "", (":hidden:$DATA",))
    entries["Desktop\\escape"] = Entry(PROFILE_SDDL, "junction:C:\\Windows")
    entries["Cookies"] = Entry(PROFILE_SDDL)  # a directory where a junction belongs
    del entries["My Documents"]
    windows.running[lane] = [101, 102]
    windows.hive_until_polls = 2
    windows.owned_entries[lane] = ["C:\\Windows\\Temp\\drop.txt", "C:\\$Recycle.Bin\\S-1-5-21"]
    windows.scheduled[lane] = ["\\persist"]
    _write = station._baseline_folder(lane) / "record.json"
    record = json.loads(_write.read_text())
    record["entries"]["AppData"] = [PROFILE_SDDL, "", []]
    _write.write_text(json.dumps(record))
    token = _lease(station)["token"]
    windows.calls.clear()

    station.release(token)

    calls = windows.calls
    order = [call[0] for call in calls]
    assert calls[0] == ("enabled", lane, False)
    assert order.index("logoff") < order.index("kill") < order.index("hive")
    assert ("kill", 101) in calls and ("kill", 102) in calls
    # The old profile is set aside unread and deleted whole once the new one stands: nothing
    # the Lane made in it, such as a junction, is walked, copied or written through.
    retire = calls.index(("retire", lane))
    assert order.index("hive") < retire < calls.index(("private", "C:\\Users\\laneA"))
    restore = calls.index(("robocopy", False))
    assert retire < restore
    assert not [call for call in calls[:restore] if call[0] in ("remove", "apply_security")]
    for path, target in (("My Documents", "Documents"), ("Cookies", "AppData\\Cookies")):
        junction = ("junction", f"C:\\Users\\laneA\\{path}", f"C:\\Users\\laneA\\{target}")
        assert calls.index(junction) > restore
    old = calls.index(("remove", "C:\\Users\\laneA.ale-old"))
    assert old > max(i for i, call in enumerate(calls) if call[0] == "apply_security")
    assert ("remove", "C:\\Windows\\Temp\\drop.txt") in calls
    assert ("remove", "C:\\$Recycle.Bin\\S-1-5-21") in calls
    assert ("delete_tasks", lane) in calls
    assert ("remove", f"C:\\$Recycle.Bin\\{SIDS[lane]}") in calls
    assert ("private", str(station._stage(lane))) in calls
    compare = calls.index(("robocopy", True))
    assert compare > restore
    assert calls[-1] == ("enabled", lane, True)
    assert station.lanes[lane].state == IDLE
    assert station.lanes[lane].generation == 2
    restored = stationd.entries_from_json(record["entries"])
    assert windows.profile_entries[lane] == restored


def _stubborn_task(windows: FakeWindows) -> None:
    windows.scheduled["laneA"] = ["\\again"]
    windows.stubborn_tasks = True


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_stubborn_task, "scheduled tasks"),
        (lambda w: setattr(w, "robocopy_codes", [0, 1]), "differs from its baseline"),
        (lambda w: setattr(w, "robocopy_codes", [8]), "could not restore"),
        (lambda w: w.securities.update({"C:\\Users\\Public": "O:SYD:(A;;FA;;;SY)"}),
         "no longer denies Lanes"),
        (lambda w: setattr(w, "hive_until_polls", 10_000), "hive of laneA stayed loaded"),
    ],
)  # fmt: skip
def test_a_failed_reset_or_check_quarantines_with_the_account_disabled(
    station, windows, clock, damage, reason
):
    token = _lease(station)["token"]
    damage(windows)
    station.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    windows.calls.clear()
    station.release(token)
    lane = station.lanes["laneA"]
    assert lane.state == QUARANTINED
    assert reason in lane.reason
    assert lane.generation == 1
    assert ("enabled", "laneA", False) in windows.calls
    assert ("enabled", "laneA", True) not in windows.calls
    _lease(station)  # laneB
    with pytest.raises(StationError) as refused:
        _lease(station)
    assert refused.value.code == "busy"


def test_a_lane_without_a_baseline_keeps_its_account(station, windows, tmp_path):
    (tmp_path / "station" / "baselines" / "laneB" / "record.json").unlink()
    station.reset("laneB", None)
    assert station.lanes["laneB"].state == QUARANTINED
    assert "no baseline yet" in station.lanes["laneB"].reason
    assert not [call for call in windows.calls if call[0] == "enabled"]


def test_the_stage_directory_admits_the_framework_alone():
    framework = "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    assert stationd.stage_holds(framework)
    assert not stationd.stage_holds(framework.replace("D:PAI", "D:AI"))
    assert not stationd.stage_holds(framework + f"(A;OICI;0x1301bf;;;{SIDS['laneA']})")


def test_the_recipe_needs_each_deny_of_the_lanes_group():
    public = Deny("C:\\Users\\Public", 0x116, "(OI)(CI)(W)", True)
    held = f"O:SYD:AI(D;OICI;0x100116;;;{GROUP})(A;OICIID;FA;;;SY)"
    assert stationd.recipe_holds(held, public, GROUP)
    assert not stationd.recipe_holds(held.replace("OICI;0x", ";0x"), public, GROUP)
    assert not stationd.recipe_holds(held.replace("0x100116", "0x6"), public, GROUP)
    assert not stationd.recipe_holds(held, public, "S-1-5-21-9")
    create = Deny("C:\\", 0x6, "(AD,WD)", False)
    assert stationd.recipe_holds(f"O:SYD:(D;;DCLC;;;{GROUP})", create, GROUP)
    assert stationd.sddl_owner(f"O:{SIDS['laneA']}D:") == SIDS["laneA"]
    assert stationd.sddl_owner("O:BAD:") == stationd.ADMINISTRATORS_SID


def test_recipe_and_scan_cover_every_fixed_volume():
    denies = stationd.recipe("C:", ["C:\\", "D:\\", "E:"])
    assert [deny.path for deny in denies] == [
        "C:\\Users", "C:\\", "C:\\ProgramData", "C:\\Users\\Public", "D:\\", "E:\\",
    ]  # fmt: skip
    roots = dict(stationd.scan_roots("C:", ["C:\\", "D:\\"]))
    assert roots == {
        "C:\\": False,
        "C:\\$Recycle.Bin": False,
        "C:\\Windows\\Temp": True,
        "C:\\ProgramData": True,
        "C:\\Users\\Public": True,
        "D:\\": True,
    }


def test_robocopy_never_purges_and_stays_out_of_the_profiles_junctions():
    assert not stationd.copy_failed(0)
    assert not stationd.copy_failed(7)
    assert stationd.copy_failed(8)
    restore = stationd.restore_args("b", "p")
    assert "/E" in restore and "/MIR" not in restore and "/PURGE" not in restore
    compare = stationd.compare_args("b", "p", ["p\\J"])
    assert "/L" in compare and compare[compare.index("/XD") + 1] == "p\\J"
    # A skipped junction counts as an extra directory; the snapshot finds real extras.
    assert [stationd.compare_differs(code) for code in (0, 1, 2, 3, 4)] == [
        False,
        True,
        False,
        True,
        True,
    ]
    assert "/COPY:DAT" in stationd.capture_args("p", "b")  # never the Lane's own access lists


#: As measured on a Station: a folder made in the root of C:\\ inherits Users' right to add
#: files and folders below it; Windows' own and Program Files' grant Users read and run.
PYTHON312 = (
    "O:BAD:AI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;BU)(A;CIID;LC;;;BU)"
    "(A;CIID;DC;;;BU)(A;OICIIOID;GA;;;CO)"
)
SYSTEM32 = (
    "O:S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464D:PAI(A;OICIIO;GA;;;CO)"
    "(A;OICIIO;GA;;;SY)(A;;0x1301bf;;;SY)(A;OICIIO;GA;;;BA)(A;;0x1301bf;;;BA)"
    "(A;OICIIO;GXGR;;;BU)(A;;0x1200a9;;;BU)(A;;0x1200a9;;;AC)"
)
NODEJS = "O:BAD:P(A;OICI;0x1200a9;;;AU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"


def test_lanes_may_not_write_where_the_machine_finds_programs():
    lanes = {GROUP, SIDS["laneA"]}
    assert stationd.lanes_change(PYTHON312, lanes)
    assert not stationd.lanes_change(SYSTEM32, lanes)
    assert not stationd.lanes_change(NODEJS, lanes)
    assert stationd.lanes_change(f"O:{SIDS['laneA']}D:P(A;;FA;;;SY)", lanes)  # its owner
    assert stationd.lanes_change(f"O:BAD:(A;;0x2;;;{GROUP})", lanes)
    assert not stationd.lanes_change("O:BAD:(A;OICIIO;FA;;;BU)", lanes)  # for what it holds


def test_a_path_lanes_may_write_keeps_lanes_out_of_service(station, windows):
    windows.path = ["C:\\Python312\\Scripts", "C:\\Windows\\system32", "C:\\Tools\\bin"]
    windows.securities["C:\\Python312\\Scripts"] = PYTHON312
    windows.securities["C:\\Windows\\system32"] = SYSTEM32
    # A directory that is not there is as open as the nearest one above it.
    windows.missing |= {"C:\\Tools\\bin", "C:\\Tools"}
    windows.securities["C:\\"] = PYTHON312
    token = _lease(station)["token"]
    station.release(token)
    lane = station.lanes["laneA"]
    assert lane.state == QUARANTINED
    scripts = "C:\\Python312\\Scripts is on the machine's PATH and Lanes may write in it"
    assert scripts in lane.reason
    assert "C:\\ is on the machine's PATH" in lane.reason
    lines, ok = station.enroll()
    assert not ok
    assert any(line.startswith("C:\\Python312\\Scripts is on the machine's PATH") for line in lines)


# --- enrollment ---


def test_enrollment_captures_signed_in_lanes_and_returns_quarantined_ones(
    tmp_path, windows, station
):
    (tmp_path / "station" / "baselines" / "laneB" / "record.json").unlink()
    station.lanes["laneA"].state, station.lanes["laneA"].reason = QUARANTINED, "acl"
    station.reset("laneB", None)  # quarantined: it has no baseline yet
    station._save()
    original = windows.robocopy

    def capture(arguments):
        if "/L" not in arguments and arguments[0].endswith("laneB"):
            target = tmp_path / "station" / "baselines" / "laneB" / "profile"
            target.mkdir(parents=True, exist_ok=True)
            (target / "NTUSER.DAT").write_bytes(b"fresh hive")
        return original(arguments)

    windows.robocopy = capture
    windows.securities["C:\\Users\\Public"] = "O:SYD:(A;;FA;;;SY)"
    windows.calls.clear()
    lines, ok = station.enroll()
    assert ok
    assert lines[-2] == "laneA: baseline sha256:base; back from quarantine (acl)"
    assert lines[-1].startswith("laneB: captured baseline sha256:")
    assert lines[-1].endswith(
        "; back from quarantine (laneB has no baseline yet; run setup_station.py)"
    )
    assert {lane.state for lane in station.lanes.values()} == {RESETTING}
    assert ("enabled", "laneB", False) in windows.calls
    assert windows.calls[-1] == ("enabled", "laneB", True)
    record = json.loads((tmp_path / "station" / "baselines" / "laneB" / "record.json").read_text())
    assert record["profile"] == "C:\\Users\\laneB"
    # A deny the path lacks is added; one it has is not added again.
    denies = [call[1] for call in windows.calls if call[:1] == ("icacls",) and "/deny" in call]
    assert denies == ["C:\\Users\\Public"]
    assert ("private", str(station._stage("laneA"))) in windows.calls


# --- framework ---


def _framework_tar(*parts: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as bundle:
        for part in parts:
            data = f"{part} file".encode()
            info = tarfile.TarInfo(f"{part}/file.txt")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def test_a_framework_push_replaces_the_framework_layout(station, tmp_path, windows):
    (tmp_path / "guestd").mkdir()
    (tmp_path / "guestd" / "stale.py").write_text("old")
    payload = _framework_tar(*stationd.FRAMEWORK_PARTS)
    identity = "sha256:" + "1" * 64
    assert station.install_framework(identity, io.BytesIO(payload).read, len(payload)) == {
        "framework": identity
    }
    assert station.framework() == identity
    assert (tmp_path / "cua-driver" / "file.txt").read_text() == "cua-driver file"
    assert not (tmp_path / "guestd" / "stale.py").exists()
    assert ("lock_framework",) in windows.calls
    assert not [path.name for path in tmp_path.iterdir() if path.name.startswith(".framework")]


def test_a_framework_push_waits_for_no_lane_to_be_leased(station):
    _lease(station)
    payload = _framework_tar(*stationd.FRAMEWORK_PARTS)
    with pytest.raises(StationError) as refused:
        station.install_framework("sha256:" + "1" * 64, io.BytesIO(payload).read, len(payload))
    assert refused.value.code == "lanes-leased"
    assert station.framework() == FRAMEWORK


def test_a_framework_push_waits_for_no_lane_to_be_resetting(station):
    """A reset ends what the lease ran from the framework only as it starts."""
    station.lanes["laneB"].state = RESETTING
    payload = _framework_tar(*stationd.FRAMEWORK_PARTS)
    with pytest.raises(StationError) as refused:
        station.install_framework("sha256:" + "1" * 64, io.BytesIO(payload).read, len(payload))
    assert refused.value.code == "framework-busy"
    assert station.framework() == FRAMEWORK


def test_a_framework_push_that_fails_part_way_keeps_the_installed_one(station, tmp_path, windows):
    for part in stationd.FRAMEWORK_PARTS:
        (tmp_path / part).mkdir()
        (tmp_path / part / "file.txt").write_text("installed")

    def lock(root):
        raise OSError("icacls failed")

    windows.lock_framework = lock
    payload = _framework_tar(*stationd.FRAMEWORK_PARTS)
    with pytest.raises(StationError, match="icacls failed"):
        station.install_framework("sha256:" + "1" * 64, io.BytesIO(payload).read, len(payload))
    assert station.framework() == FRAMEWORK
    for part in stationd.FRAMEWORK_PARTS:
        assert (tmp_path / part / "file.txt").read_text() == "installed"
    assert not [path.name for path in tmp_path.iterdir() if path.name.startswith(".framework")]


def test_a_framework_push_takes_only_the_framework(station):
    payload = _framework_tar("guestd", "python", "cua-driver", "Windows")
    with pytest.raises(StationError, match="holds"):
        station.install_framework("sha256:" + "1" * 64, io.BytesIO(payload).read, len(payload))
    assert station.framework() == ""  # nothing runs until a push succeeds


# --- the wire ---


class FakeStream:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = io.BytesIO(incoming)
        self.sent = b""

    def read(self, size: int) -> bytes:
        return self.incoming.read(size)

    def write(self, data: bytes) -> None:
        self.sent += data

    def shutdown(self) -> None:
        pass


def test_a_request_is_answered_with_one_json_line(station):
    stream = FakeStream(b"hello\n")
    stationd.serve_connection(station, stream)
    reply = json.loads(stream.sent)
    assert reply["ok"] is True
    assert reply["protocol"] == stationd.PROTOCOL
    assert reply["framework"] == FRAMEWORK
    assert reply["software"] == {"rhino-7": {"probe": "lmstat"}, "notepad": {}}
    assert reply["lanes"][0] == {"lane": "laneA", "state": IDLE, "generation": 1,
                                 "session": "1600x900"}  # fmt: skip
    stream = FakeStream(b"release nonsense\n")
    stationd.serve_connection(station, stream)
    assert json.loads(stream.sent) == {
        "ok": False, "error": "bad-request", "message": "malformed lease token",
    }  # fmt: skip


def test_a_guestd_stream_relays_both_ways_after_its_status_line(station, windows):
    token = _lease(station)["token"]
    windows._child = lambda: FakeChild(b'{"id":1,"ok":true}\n')
    stream = FakeStream(f"guestd {token}\n".encode() + b'{"id":1,"op":"health"}\n')
    stationd.serve_connection(station, stream)
    status, guestd = stream.sent.split(b"\n", 1)
    assert json.loads(status) == {"ok": True}
    assert guestd == b'{"id":1,"ok":true}\n'
    assert station.lanes["laneA"].sessions == 0
    assert station.lanes["laneA"].state == LEASED


def test_a_framework_stream_is_read_from_after_its_request_line(station):
    payload = _framework_tar(*stationd.FRAMEWORK_PARTS)
    identity = "sha256:" + "2" * 64
    stream = FakeStream(f"framework {identity} {len(payload)}\n".encode() + payload)
    stationd.serve_connection(station, stream)
    assert json.loads(stream.sent) == {"ok": True, "framework": identity}
    assert station.framework() == identity


# --- setup_station.py ---


def _setup():
    import importlib.util

    script = Path(__file__).parents[2] / "scripts" / "setup_station.py"
    spec = importlib.util.spec_from_file_location("setup_station", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_writes_the_station_json_ale_stationd_reads(tmp_path):
    setup = _setup()
    (tmp_path / "lanes.password").write_text("Lane-Pass-1\n")
    (tmp_path / "station.toml").write_text(
        'lanes = 28\nresolution = "1600x900"\n'
        'host_keys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbc ale@host"]\n'
        f'lane_password_file = "{tmp_path / "lanes.password"}"\n'
        "[software.rhino-7]\nprobe = 'lmutil lmstat'\n[software.notepad]\n"
    )
    station, keys, password = setup.read_config(tmp_path / "station.toml")
    (tmp_path / "station.json").write_text(json.dumps(station))
    config = Config.read(tmp_path / "station.json")
    assert config.lanes[:2] == ("laneA", "laneB")
    assert config.lanes[25:] == ("laneZ", "laneAA", "laneAB")
    assert config.software == {"rhino-7": {"probe": "lmutil lmstat"}, "notepad": {}}
    assert password == "Lane-Pass-1"
    assert keys == ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbc ale@host"]


def test_setup_refuses_a_key_with_options(tmp_path):
    setup = _setup()
    (tmp_path / "station.toml").write_text(
        'lanes = 2\nresolution = "1600x900"\n'
        "host_keys = ['command=\"cmd\" ssh-ed25519 AAAA ale@host']\n"
        'lane_password_file = "unused"\n'
    )
    with pytest.raises(setup.SetupError, match="without options"):
        setup.read_config(tmp_path / "station.toml")


def test_setup_installs_what_the_windows_base_and_framework_pin():
    """The Station's Python is the framework's Windows build, and its agent runtimes are the
    Windows base image's, so one bump of either reaches both."""
    from ale.run import framework

    setup = _setup()
    assert framework._PYTHON["windows"] == setup.PYTHON_RELEASE
    base = Path(__file__).parents[2] / "images" / "base" / "vm-windows10"
    recipe = (base / "prepare.ps1").read_text() + (base / "tools.ps1").read_text()
    for pin in ('"3.12.10"', '"24.15.0"', '"2.53.0"', "chocolatey/2.4.3",
                "d4998ca928a85a484507dcaa39c30948a6516de0d1469b0511931d44a53456c3"):  # fmt: skip
        assert pin.strip('"') in setup.ENROLL
        assert pin in recipe
