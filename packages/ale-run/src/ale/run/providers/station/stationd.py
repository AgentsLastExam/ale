r"""ale-stationd: the trusted service of a Windows Station, and the client a Host reaches it by.

A Station is a Windows Server machine its operator owns, with licensed software installed
for every account. Each Lane is an ordinary account with a Remote Desktop session of its
own, which the operator's keep-alive box holds connected from outside the machine; a Lane
serves one Sandbox at a time. This file runs on the Station, on the service's own Python
``C:\ProgramData\ALE\station\python``, in one of three modes:

``service``
    The Windows service ``ALEStation``, as LocalSystem. It leases Lanes, starts guestd, the
    Gateway tunnel and Cua Driver in them, and resets and checks a Lane once its lease ends.
``client``
    The forced command of the ``ale-host`` account's SSH keys. It hands the request in
    ``SSH_ORIGINAL_COMMAND`` to the service over ``\\.\pipe\ale-stationd`` and splices its
    standard input and output to that pipe, so SSH carries the request's bytes both ways.
``enroll``
    Run by ``scripts/setup_station.py`` as an administrator while the service is stopped:
    makes each Lane's stage root, applies the isolation recipe, captures the profile
    baseline of each Lane that has signed in, and sends quarantined Lanes back to the reset.

A request is one line, and every reply starts with one JSON line: ``{"ok": true, ...}`` or
``{"ok": false, "error": <code>, "message": <text>}``.

``hello``
    The protocol, this file's SHA-256, the Windows build, the installed framework, the
    Lanes' resolution, the software manifest, and each Lane's state, generation and session.
``status``
    ``hello`` with each Lane's lease and quarantine details.
``framework <identity> <bytes>``
    A tar of ``guestd/``, ``python/`` and ``cua-driver/`` of that many bytes follows; it
    replaces those of ``C:\ProgramData\ALE``, and only while no Lane is leased.
``lease <framework> <W>x<H> [<software>...]``
    Runs the probes of the named software, within ``PROBE_TIMEOUT_SEC`` in all, waits up to
    ``LEASE_WAIT_SEC`` for an idle Lane whose session is active at that size, starts the
    Lane's Cua Driver, and replies with the lease's token, Lane, home, generation, loopback
    address, stage root and baseline.
``guestd <token>``
    After the reply, the stream is a guestd ``--stdio`` session running as SYSTEM in the
    Lane's session.
``tunnel <token> <port>:<target>...``
    After the reply, the stream is ``guestd/tunnel.py``'s, accepting on each port at the
    lease's loopback address, which the service holds bound until the lease ends.
``release <token>``
    Ends the lease; the Lane is reset and checked in the background.

A lease lives while any of its ``guestd`` or ``tunnel`` streams is connected, and for
``GRACE_SEC`` after the last one closes. Its token names the Lane's generation, which every
passed check advances, so a token outlives neither its lease nor the reset after it.

The Provider ships this file as source and compares the copy the service runs with its
own. Like guestd it imports nothing from the engine and nothing outside the standard
library, and it runs guestd's files rather than importing them.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import ntpath
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes as w
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PROTOCOL = 1
SERVICE = "ALEStation"
PIPE = r"\\.\pipe\ale-stationd"
#: The account the Host signs in as; its SSH keys run this file's ``client`` mode only.
HOST_ACCOUNT = "ale-host"
#: The local group of every Lane account, which the isolation recipe denies.
LANE_GROUP = "ALE Lanes"
ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ALE"
#: What a framework push replaces in ``ROOT``; the rest of ``ROOT`` is the Station's own.
FRAMEWORK_PARTS = ("guestd", "python", "cua-driver")

IDLE, LEASED, RESETTING, QUARANTINED = "idle", "leased", "resetting", "quarantined"

#: How long a lease outlives its last stream.
GRACE_SEC = 120.0
#: How long a ``lease`` request waits for a Lane to come free before it answers ``busy``.
LEASE_WAIT_SEC = 60.0
#: How long the probes of one ``lease`` request may take together.
PROBE_TIMEOUT_SEC = 120.0
#: How long the reset waits for a Lane's processes to end, for its hive to unload, and for
#: its old profile to be free to move aside.
STOP_WAIT_SEC = 60.0
HIVE_WAIT_SEC = 120.0
#: How many times in a row a lease's Cua Driver may fail to start again before the lease
#: ends: guestd would otherwise call a pipe nobody serves, or one another account took.
DRIVER_STARTS = 3
LINE_BYTES = 4096
CHUNK_BYTES = 64 * 1024
FRAMEWORK_MAX_BYTES = 2 * 1024**3

SYSTEM_SID, ADMINISTRATORS_SID = "S-1-5-18", "S-1-5-32-544"
AUTHENTICATED_USERS_SID = "S-1-5-11"
FILE_LIST_DIRECTORY, FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY = 0x1, 0x2, 0x4
FILE_WRITE_EA, FILE_WRITE_ATTRIBUTES = 0x10, 0x100
_WRITES = FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | 0x40000000 | 0x10000000  # | GW | GA

_NAME = r"[A-Za-z][A-Za-z0-9]{0,19}"
_LANE = re.compile(_NAME)
_TOKEN = re.compile(rf"({_NAME})\.(\d{{1,9}})\.[A-Za-z0-9_-]{{43}}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:+_-]{0,199}")
_SOFTWARE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESOLUTION = re.compile(r"([1-9]\d{2,4})x([1-9]\d{2,4})")
_LISTENER = re.compile(r"(\d{1,5}):(\d{1,3})")
_SID = re.compile(r"S-1(?:-\d+)+")


class StationError(Exception):
    """A request the service refuses, with a code the Provider acts on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResetError(Exception):
    """A reset step that failed; the Lane is quarantined with this message."""


# --- requests ------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    op: str
    token: str = ""
    framework: str = ""
    size: int = 0
    resolution: tuple[int, int] = (0, 0)
    software: tuple[str, ...] = ()
    listeners: tuple[tuple[int, int], ...] = ()

    @property
    def streams(self) -> bool:
        """Whether the connection becomes a process's standard input and output."""
        return self.op in ("guestd", "tunnel")


def parse_request(line: str) -> Request:
    """One request line, or ``StationError("bad-request")`` saying what is wrong with it."""
    words = line.split()
    if not words:
        raise StationError("bad-request", "empty request")
    op, args = words[0], words[1:]

    def arity(low: int, high: int, usage: str) -> None:
        if not low <= len(args) <= high:
            raise StationError("bad-request", f"usage: {usage}")

    if op in ("hello", "status"):
        arity(0, 0, op)
        return Request(op)
    if op == "framework":
        arity(2, 2, "framework <identity> <bytes>")
        size = int(args[1]) if args[1].isdigit() else -1
        if not _IDENTITY.fullmatch(args[0]) or not 0 < size <= FRAMEWORK_MAX_BYTES:
            raise StationError("bad-request", "framework takes an identity and a byte count")
        return Request(op, framework=args[0], size=size)
    if op == "lease":
        arity(2, 18, "lease <framework> <W>x<H> [<software>...]")
        resolution = _RESOLUTION.fullmatch(args[1])
        if not _IDENTITY.fullmatch(args[0]) or resolution is None:
            raise StationError("bad-request", "lease takes a framework identity and a WxH size")
        software = tuple(args[2:])
        if bad := [name for name in software if not _SOFTWARE.fullmatch(name)]:
            raise StationError("bad-request", f"not a software id: {bad[0]!r}")
        size = (int(resolution[1]), int(resolution[2]))
        return Request(op, framework=args[0], resolution=size, software=software)
    if op in ("guestd", "release"):
        arity(1, 1, f"{op} <token>")
        return Request(op, token=_token(args[0]))
    if op == "tunnel":
        arity(2, 9, "tunnel <token> <port>:<target>...")
        listeners = []
        for spec in args[1:]:
            match = _LISTENER.fullmatch(spec)
            if match is None or not 0 < int(match[1]) < 65536 or int(match[2]) > 255:
                raise StationError("bad-request", f"not a <port>:<target> listener: {spec!r}")
            listeners.append((int(match[1]), int(match[2])))
        return Request(op, token=_token(args[0]), listeners=tuple(listeners))
    raise StationError("bad-request", f"unknown request {op!r}")


def _token(text: str) -> str:
    if not _TOKEN.fullmatch(text):
        raise StationError("bad-request", "malformed lease token")
    return text


def new_token(lane: str, generation: int) -> str:
    return f"{lane}.{generation}.{secrets.token_urlsafe(32)}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _line(fields: dict[str, object]) -> bytes:
    return json.dumps(fields, separators=(",", ":")).encode() + b"\n"


# --- configuration and the lease store --------------------------------------------------


@dataclass(frozen=True)
class Config:
    """``station.json``, which ``setup_station.py`` writes from the operator's file."""

    lanes: tuple[str, ...]
    resolution: tuple[int, int]
    #: Each software id with its manifest entry, which may name a ``probe`` command.
    software: dict[str, dict[str, str]]

    @classmethod
    def read(cls, path: Path) -> Config:
        data = json.loads(path.read_text(encoding="utf-8"))
        lanes = tuple(data["lanes"])
        resolution = _RESOLUTION.fullmatch(str(data["resolution"]))
        software = {str(name): dict(entry) for name, entry in data.get("software", {}).items()}
        if not 0 < len(lanes) <= 254 or len({lane.casefold() for lane in lanes}) != len(lanes):
            raise ValueError("station.json must name between 1 and 254 distinct Lanes")
        if bad := [lane for lane in lanes if not _LANE.fullmatch(lane)]:
            raise ValueError(f"not a Lane account name: {bad[0]!r}")
        if resolution is None:
            raise ValueError("station.json's resolution must be WxH")
        for name, entry in software.items():
            if not _SOFTWARE.fullmatch(name) or set(entry) - {"probe"}:
                raise ValueError(f"bad software entry {name!r}")
        return cls(lanes, (int(resolution[1]), int(resolution[2])), software)


@dataclass
class Lane:
    """One Lane's record in the lease store."""

    name: str
    index: int
    state: str = RESETTING
    generation: int = 0
    #: SHA-256 of the current lease's token.
    token: str = ""
    sessions: int = 0
    leased_at: float = 0.0
    last_close: float = 0.0
    #: Why the Lane is quarantined.
    reason: str = ""

    def stored(self) -> dict[str, object]:
        return {
            "state": self.state,
            "generation": self.generation,
            "token": self.token,
            "sessions": self.sessions,
            "leased_at": self.leased_at,
            "last_close": self.last_close,
            "reason": self.reason,
        }


def load_state(path: Path, config: Config) -> dict[str, Lane]:
    """The configured Lanes as the store last saw them; a Lane it does not know is resetting."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))["lanes"]
    except FileNotFoundError:
        saved = {}
    lanes = {}
    for index, name in enumerate(config.lanes):
        lane = Lane(name, index)
        for key, value in saved.get(name, {}).items():
            if key in lane.stored() and type(value) is type(getattr(lane, key)):
                setattr(lane, key, value)
        if lane.state not in (IDLE, LEASED, RESETTING, QUARANTINED):
            lane.state = RESETTING
        lanes[name] = lane
    return lanes


def save_state(
    path: Path, lanes: dict[str, Lane], sleep: Callable[[float], None] = time.sleep
) -> None:
    """Replace the store in one step, so a crash leaves the old one or the new one.

    Another process reading the store, such as setup, may hold it open without letting it
    be replaced for a moment; the replacement is tried again meanwhile."""
    partial = path.with_name(path.name + ".new")
    data = json.dumps({"lanes": {name: lane.stored() for name, lane in lanes.items()}}, indent=1)
    with partial.open("w", encoding="utf-8") as stream:
        stream.write(data + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(50):
        try:
            os.replace(partial, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            sleep(0.1)


# --- the isolation recipe and the reset's comparisons -----------------------------------


@dataclass(frozen=True)
class Deny:
    """A deny entry of the isolation recipe for the ``ALE Lanes`` group."""

    path: str
    #: The rights the entry must deny.
    rights: int
    #: The same in icacls's notation.
    icacls: str
    #: Whether the entry must also apply to everything below ``path``.
    inherited: bool


def recipe(system_drive: str, volumes: Sequence[str]) -> list[Deny]:
    """Where no Lane may list or create entries, so no Lane hands another one data there.

    Each Lane's profile admits only it, and its stage directory only the framework; these
    are the shared places a standard account could otherwise write to, or list other
    accounts' homes in.
    """
    create = FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY
    write = create | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES
    drive = system_drive.rstrip("\\") + "\\"
    denies = [
        Deny(drive + "Users", FILE_LIST_DIRECTORY, "(RD)", False),
        Deny(drive, create, "(AD,WD)", False),
        Deny(drive + "ProgramData", create, "(AD,WD)", False),
        Deny(drive + r"Users\Public", write, "(OI)(CI)(W)", True),
    ]
    return denies + [Deny(volume, create, "(AD,WD)", False) for volume in _others(drive, volumes)]


def scan_roots(system_drive: str, volumes: Sequence[str]) -> list[tuple[str, bool]]:
    r"""Where the reset looks for entries a Lane owns, and whether it looks below each.

    These are the shared places a Lane could still write to: the system drive's root and
    recycle bin, ``Windows\Temp``, ``ProgramData`` and the public profile, and every other
    fixed volume.
    """
    drive = system_drive.rstrip("\\") + "\\"
    roots = [
        (drive, False),
        (drive + "$Recycle.Bin", False),
        (drive + r"Windows\Temp", True),
        (drive + "ProgramData", True),
        (drive + r"Users\Public", True),
    ]
    return roots + [(volume, True) for volume in _others(drive, volumes)]


def _others(drive: str, volumes: Sequence[str]) -> list[str]:
    return [
        volume.rstrip("\\") + "\\"
        for volume in volumes
        if volume.rstrip("\\").casefold() != drive.rstrip("\\").casefold()
    ]


_SID_ALIASES = {
    "SY": SYSTEM_SID,
    "BA": ADMINISTRATORS_SID,
    "BU": "S-1-5-32-545",
    "AU": AUTHENTICATED_USERS_SID,
    "WD": "S-1-1-0",
    "CO": "S-1-3-0",
    "OW": "S-1-3-4",
    "IU": "S-1-5-4",
    "AN": "S-1-5-7",
    "PS": "S-1-5-10",
    "LS": "S-1-5-19",
    "NS": "S-1-5-20",
    "RD": "S-1-5-32-555",
    "AC": "S-1-15-2-1",
}
_RIGHTS = {
    "GA": 0x10000000,
    "GX": 0x20000000,
    "GW": 0x40000000,
    "GR": 0x80000000,
    "SD": 0x10000,
    "RC": 0x20000,
    "WD": 0x40000,
    "WO": 0x80000,
    "CC": 0x1,
    "DC": 0x2,
    "LC": 0x4,
    "SW": 0x8,
    "RP": 0x10,
    "WP": 0x20,
    "DT": 0x40,
    "LO": 0x80,
    "CR": 0x100,
    "FA": 0x1F01FF,
    "FR": 0x120089,
    "FW": 0x120116,
    "FX": 0x1200A0,
}


@dataclass(frozen=True)
class Ace:
    kind: str
    flags: frozenset[str]
    rights: int
    sid: str


def sddl_dacl(sddl: str) -> tuple[frozenset[str], list[Ace]]:
    """The DACL of an SDDL string: its control flags (``P``, ``AI``...) and its entries."""
    match = re.search(r"D:((?:P|AI|AR)*)((?:\([^)]*\))*)", sddl)
    if match is None:
        return frozenset(), []
    control = frozenset(re.findall(r"P|AI|AR", match[1]))
    aces = []
    for body in re.findall(r"\(([^)]*)\)", match[2]):
        kind, flags, rights, _, _, sid = (body.split(";") + [""] * 6)[:6]
        aces.append(Ace(kind, frozenset(re.findall("..", flags)), _rights(rights), _alias(sid)))
    return control, aces


def sddl_owner(sddl: str) -> str:
    match = re.match(r"O:(S-1(?:-\d+)+|[A-Z]{2})", sddl)
    return _alias(match[1]) if match else ""


def _rights(text: str) -> int:
    if text.lower().startswith("0x"):
        return int(text, 16)
    return sum(_RIGHTS.get(token, 0) for token in re.findall("..", text))


def _alias(sid: str) -> str:
    return _SID_ALIASES.get(sid, sid)


def recipe_holds(sddl: str, deny: Deny, group: str) -> bool:
    """Whether ``sddl`` denies the ``ALE Lanes`` group what ``deny`` names."""
    _, aces = sddl_dacl(sddl)
    return any(
        ace.kind == "D"
        and ace.sid == group
        and ace.rights & deny.rights == deny.rights
        and "IO" not in ace.flags
        and (not deny.inherited or {"OI", "CI"} <= ace.flags)
        for ace in aces
    )


def stage_holds(sddl: str) -> bool:
    """Whether a stage directory admits SYSTEM and Administrators, and no one else: it is the
    framework's, as ``/opt/ale`` is root's, and its Lane reads none of it."""
    control, aces = sddl_dacl(sddl)
    allowed = {ace.sid for ace in aces if ace.kind == "A"}
    return "P" in control and allowed == {SYSTEM_SID, ADMINISTRATORS_SID}


def broad_write(sddl: str, sid: str) -> bool:
    """Whether ``sddl`` itself (not by inheritance) lets ``sid`` create entries."""
    _, aces = sddl_dacl(sddl)
    return any(
        ace.kind == "A" and ace.sid == sid and "ID" not in ace.flags and ace.rights & _WRITES
        for ace in aces
    )


#: The groups every Lane's token holds: Everyone, LOCAL, INTERACTIVE, Authenticated Users,
#: REMOTE INTERACTIVE LOGON, This Organization, Local account, Users, Remote Desktop Users.
EVERY_LANE = frozenset({
    "S-1-1-0", "S-1-2-0", "S-1-5-4", "S-1-5-11", "S-1-5-14", "S-1-5-15", "S-1-5-113",
    "S-1-5-32-545", "S-1-5-32-555",
})  # fmt: skip
#: What lets an account put a program in a directory or take the directory over: adding or
#: deleting entries, changing its access list or owner, or deleting it.
_CHANGES = FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | 0x40 | 0x10000 | 0x40000 | 0x80000
_CHANGES |= 0x40000000 | 0x10000000  # GW | GA


def lanes_change(sddl: str, lanes: set[str]) -> bool:
    """Whether ``sddl`` lets a Lane add or change entries: a Lane's account or group in
    ``lanes``, or a group every Lane holds, owns it or is granted such a right, inherited or
    not. A deny is not weighed: a directory a program runs from grants Lanes nothing."""
    if sddl_owner(sddl) in lanes:
        return True
    _, aces = sddl_dacl(sddl)
    return any(
        ace.kind == "A"
        and "IO" not in ace.flags
        and ace.rights & _CHANGES
        and (ace.sid in lanes or ace.sid in EVERY_LANE)
        for ace in aces
    )


@dataclass(frozen=True)
class Entry:
    """One entry of a profile as the reset compares it."""

    #: Owner and DACL, as SDDL.
    security: str
    #: For a reparse point: ``junction:<target>``, ``symlink:<target>`` or ``reparse:<tag>``.
    link: str = ""
    #: Its named data streams, such as ``:Zone.Identifier:$DATA``.
    streams: tuple[str, ...] = ()


def entries_json(entries: dict[str, Entry]) -> dict[str, list[object]]:
    return {path: [e.security, e.link, list(e.streams)] for path, e in sorted(entries.items())}


def entries_from_json(data: dict[str, list[object]]) -> dict[str, Entry]:
    return {
        path: Entry(str(security), str(link), tuple(map(str, streams)))  # type: ignore[call-overload]
        for path, (security, link, streams) in data.items()
    }


def junctions(recorded: dict[str, Entry]) -> list[tuple[str, str]]:
    """The junctions of a baseline and their targets, which robocopy neither copies nor
    enters, so that the reset makes them itself."""
    return [
        (path, entry.link.removeprefix("junction:"))
        for path, entry in sorted(recorded.items())
        if entry.link.startswith("junction:")
    ]


def snapshot_differences(current: dict[str, Entry], recorded: dict[str, Entry]) -> list[str]:
    differences = [f"{path or '.'} is missing" for path in sorted(recorded.keys() - current)]
    differences += [f"{path} is new" for path in sorted(current.keys() - recorded)]
    for path in sorted(current.keys() & recorded.keys()):
        now, then = current[path], recorded[path]
        for name in ("security", "link", "streams"):
            if getattr(now, name) != getattr(then, name):
                differences.append(f"{path or '.'} has other {name}")
    return differences


def baseline_digest(files: Path, entries: dict[str, Entry]) -> str:
    """The baseline's identity: every file's path and content, and every entry's security."""
    digest = hashlib.sha256()
    for top, directories, names in os.walk(files):
        directories.sort()
        for name in sorted(names):
            path = Path(top, name)
            digest.update(path.relative_to(files).as_posix().encode() + b"\0")
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    digest.update(json.dumps(entries_json(entries), sort_keys=True).encode())
    return f"sha256:{digest.hexdigest()}"


#: robocopy's exit codes from 8 up mean some file could not be copied.
def copy_failed(code: int) -> bool:
    return code >= 8


_QUIET = ("/R:1", "/W:1", "/NP", "/NFL", "/NDL", "/NJH", "/NJS")


def capture_args(profile: str, baseline: str) -> list[str]:
    """Copy a profile's content, not its security: the copy must admit no Lane."""
    return [profile, baseline, "/MIR", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/B", *_QUIET]


def restore_args(baseline: str, profile: str) -> list[str]:
    """Copy the baseline into a new, empty profile: ``/E``, never ``/MIR``, so that robocopy
    deletes nothing; purging, it deletes through a junction what the junction points to."""
    copy = ["/E", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/B"]
    return [baseline, profile, *copy, *_QUIET]


def compare_args(baseline: str, profile: str, links: Sequence[str]) -> list[str]:
    """Compare the baseline with the profile, listing only (``/L``) and staying out of the
    profile's junctions (``/XD``)."""
    return [baseline, profile, "/MIR", "/L", "/XJ", "/B", *_skip(links), *_QUIET]


def _skip(links: Sequence[str]) -> list[str]:
    return ["/XD", *links] if links else []


def compare_differs(code: int) -> bool:
    """Whether a comparison found a file to copy or a mismatch. robocopy counts a junction
    it skips as an extra directory, so its "extra" bit says nothing; the profile's snapshot
    finds what is extra."""
    return bool(code & ~0x2)


def guestd_environment(
    base: dict[str, str], user: str, home: str, pipe: str, temp: str
) -> dict[str, str]:
    """guestd's environment: the service's own, never the Lane's, plus what guestd reads
    about the agent's account and its Cua Driver, and a temporary directory of its own.

    guestd and what it starts as SYSTEM would otherwise take their Python's user site, their
    programs' search path and their temporary files from directories the Lane writes to."""
    return {
        **base,
        "ALE_AGENT_USER": user,
        "ALE_AGENT_HOME": home,
        "ALE_AGENT_SUDO": "0",
        "ALE_CUA_DRIVER_SOCKET": f"\\\\.\\pipe\\{pipe}",
        "TEMP": temp,
        "TMP": temp,
    }


def loopback(taken: set[str]) -> str:
    """A loopback address no lease holds, drawn at random, so that no account can bind it
    before the lease's tunnel does: Lanes share the machine's loopback."""
    while True:
        octets = (secrets.randbelow(254) + 1, secrets.randbelow(256), secrets.randbelow(254) + 1)
        address = "127.{}.{}.{}".format(*octets)
        if address not in taken:
            return address


# --- the service's state machine -------------------------------------------------------


@dataclass(frozen=True)
class Session:
    id: int
    user: str
    active: bool
    width: int
    height: int


@dataclass(eq=False)
class Lease:
    """What one lease runs; it lives only as long as the service does."""

    lane: str
    generation: int
    session: int
    #: The loopback address the lease's tunnel accepts at.
    address: str
    #: The pipe the lease's Cua Driver serves, named at random for the lease, so that no
    #: account can take the name before the Driver does.
    pipe: str
    home: str = ""
    #: guestd runs in one job and the tunnel in another, since a job holds the processes of
    #: one session only; whatever they start runs in it too, as any account.
    job: Job | None = None
    tunnel_job: Job | None = None
    children: list[Child] = field(default_factory=list)
    driver: Child | None = None
    #: Starts of the Driver that failed since it last started.
    driver_failures: int = 0
    #: The tunnel's listening sockets by port, bound for the rest of the lease once a tunnel
    #: asks for them: between two tunnels, another account could otherwise bind the port.
    listeners: dict[int, socket.socket] = field(default_factory=dict)


@dataclass(frozen=True)
class Baseline:
    sid: str
    profile: str
    files: Path
    entries: dict[str, Entry]

    @property
    def junctions(self) -> list[str]:
        """The full paths of the profile's own junctions."""
        return [
            _join(self.profile, path)
            for path, entry in sorted(self.entries.items())
            if entry.link.startswith("junction:")
        ]


def _in_thread(function: Callable[[], None]) -> None:
    threading.Thread(target=function, daemon=True).start()


class Station:
    """The Lanes and their leases. Every Windows call goes through ``windows``."""

    def __init__(
        self,
        config: Config,
        windows: Windows,
        root: Path,
        *,
        digest: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        background: Callable[[Callable[[], None]], None] = _in_thread,
        log: Callable[[str], None] = print,
        lease_wait: float = LEASE_WAIT_SEC,
        grace: float = GRACE_SEC,
    ) -> None:
        self.config = config
        self.windows = windows
        self.root = root
        self.home = root / "station"
        self.digest = digest
        self.clock = clock
        self.sleep = sleep
        self.background = background
        self.log = log
        self.lease_wait = lease_wait
        self.grace = grace
        self.lanes: dict[str, Lane] = {}
        self._leases: dict[str, Lease] = {}
        self._accounts: dict[str, tuple[str, str]] = {}
        self._captures: dict[str, float] = {}
        self._changed = threading.Condition()
        self._installing = False

    # --- paths ---

    @property
    def _state(self) -> Path:
        return self.home / "state.json"

    def _baseline_folder(self, name: str) -> Path:
        return self.home / "baselines" / name

    def _stage(self, name: str) -> Path:
        return self.root / "lanes" / name

    def _temp(self, name: str) -> Path:
        """The temporary directory of what runs as SYSTEM for the Lane, which no Lane enters."""
        return self.root / "temp" / name

    def _output(self, name: str) -> Path:
        return self.home / "logs" / f"{name}.log"

    def framework(self) -> str:
        try:
            return (self.root / "framework").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    # --- lifecycle ---

    def recover(self) -> list[str]:
        """Read the store as the service starts. A Lane that was leased or resetting is reset
        again, since nothing it ran survived; a quarantined Lane stays out of service."""
        with self._changed:
            self.lanes = load_state(self._state, self.config)
            dirty = [name for name, lane in self.lanes.items() if lane.state in (LEASED, RESETTING)]
            for name in dirty:
                self._mark_resetting(self.lanes[name])
            self._save()
        return dirty

    def start(self) -> None:
        for name in self.recover():
            self.log(f"{name}: resetting, as the service started")
            self.background(lambda name=name: self.reset(name, None))

    def stop(self) -> None:
        """End what the leases run; the next start resets their Lanes."""
        with self._changed:
            leases = list(self._leases.values())
        for lease in leases:
            self._end_processes(lease)

    def tick(self) -> None:
        """End leases whose grace has run out, and start a Lane's Cua Driver again if it
        exited; a lease whose Driver does not start ``DRIVER_STARTS`` times in a row ends.

        Each Lane is handled alone: one Lane's failure must not stop the service."""
        now = self.clock()
        expired, drivers = [], []
        with self._changed:
            for name, lane in self.lanes.items():
                lease = self._leases.get(name)
                if lane.state != LEASED or lease is None:
                    continue
                if lane.sessions == 0 and now - lane.last_close >= self.grace:
                    expired.append(name)
                elif (lease.driver is not None and not lease.driver.alive()) or (
                    lease.driver is None and lease.driver_failures
                ):
                    drivers.append(lease)
        for name in expired:
            try:
                self._end(name, "its lease expired")
            except Exception as error:
                self.log(f"{name}: its lease could not end: {type(error).__name__}: {error}")
        for lease in drivers:
            try:
                self._start_driver(lease)
                lease.driver_failures = 0
                self.log(f"{lease.lane}: Cua Driver started again")
            except Exception as error:
                lease.driver_failures += 1
                self.log(f"{lease.lane}: Cua Driver did not start again: {error}")
                if lease.driver_failures >= DRIVER_STARTS:
                    with contextlib.suppress(Exception):
                        why = f"its Cua Driver did not start again: {error}"
                        self._end(lease.lane, why, lease)

    # --- requests ---

    def hello(self) -> dict[str, object]:
        sessions = self._sessions()
        with self._changed:
            lanes = [
                {"lane": name, "state": lane.state, "generation": lane.generation,
                 "session": _size(sessions.get(name.casefold()))}
                for name, lane in self.lanes.items()
            ]  # fmt: skip
        width, height = self.config.resolution
        return {
            "protocol": PROTOCOL,
            "stationd": self.digest,
            "os": self.windows.os_build(),
            "framework": self.framework(),
            "resolution": f"{width}x{height}",
            "software": self.config.software,
            "lanes": lanes,
        }

    def status(self) -> dict[str, object]:
        answer = self.hello()
        with self._changed:
            for entry in answer["lanes"]:  # type: ignore[attr-defined]
                lane = self.lanes[entry["lane"]]
                lease = self._leases.get(entry["lane"])
                entry.update(
                    address=lease.address if lease else None,
                    sessions=lane.sessions,
                    leased_at=lane.leased_at,
                    last_close=lane.last_close,
                    reason=lane.reason,
                )
            answer["installing"] = self._installing
        return answer

    def lease(self, request: Request) -> dict[str, object]:
        if unknown := [name for name in request.software if name not in self.config.software]:
            raise StationError("unknown-software", f"this Station has no {', '.join(unknown)}")
        if request.resolution != self.config.resolution:
            width, height = self.config.resolution
            raise StationError("resolution", f"this Station's Lanes are {width}x{height}")
        with self._changed:
            self._ready(request.framework)
        # The probes share one budget, which the Provider's wait for the reply counts on.
        probes = self.clock() + PROBE_TIMEOUT_SEC
        for name in request.software:
            self._probe(name, probes - self.clock())
        deadline = self.clock() + self.lease_wait
        with self._changed:
            while True:
                self._ready(request.framework)
                lane, session = self._idle(request.resolution)
                if lane is not None and session is not None:
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise StationError("busy", "no Lane is free at this size; try again")
                # Sessions come and go without a notification, so look again now and then.
                self._changed.wait(min(remaining, 5.0))
            token = new_token(lane.name, lane.generation)
            lane.state, lane.token = LEASED, _digest(token)
            lane.sessions, lane.leased_at, lane.last_close = 0, self.clock(), self.clock()
            taken = {lease.address for lease in self._leases.values()}
            pipe = f"ale-cua-driver-{lane.name}-{secrets.token_hex(8)}"
            lease = Lease(lane.name, lane.generation, session.id, loopback(taken), pipe)
            self._leases[lane.name] = lease
            self._save()
        self.log(f"{lane.name}: leased at generation {lane.generation}")
        try:
            lease.home = self._account(lane.name)[1]
            if dropped := self.windows.purge_credentials(session.id, self._captured(lane.name)):
                self.log(f"{lane.name}: deleted credentials written since its baseline: {dropped}")
            lease.job, lease.tunnel_job = self.windows.job(), self.windows.job()
            temp = str(self._temp(lane.name))
            self.windows.remove(temp)
            self.windows.make_private(temp)
            self._output(lane.name).parent.mkdir(parents=True, exist_ok=True)
            self._output(lane.name).write_bytes(b"")
            self._start_driver(lease)
        except OSError as error:
            self._end(lane.name, f"its lease could not start: {error}")
            raise StationError("failed", f"{lane.name} could not start: {error}") from error
        return {
            "token": token,
            "lane": lane.name,
            "user": lane.name,
            "home": lease.home,
            "generation": lane.generation,
            "address": lease.address,
            "stage": str(self._stage(lane.name)),
            "baseline": self._baseline_digest(lane.name),
        }

    def attach(self, request: Request) -> tuple[Lease, Child]:
        """Start the process a ``guestd`` or ``tunnel`` stream runs, counted as a session."""
        with self._changed:
            lane = self._leased(request.token)
            lease = self._leases[lane.name]
            lane.sessions += 1
            self._save()
        python = f'"{self.root / "python" / "python.exe"}" -I -S'
        guestd, output = self.root / "guestd", str(self._output(lane.name))
        try:
            if request.op == "guestd":
                environment = guestd_environment(
                    dict(os.environ), lane.name, lease.home, lease.pipe, str(self._temp(lane.name))
                )
                child = self.windows.start_guestd(
                    lease.session,
                    f'{python} "{guestd / "main.py"}" --stdio',
                    environment,
                    output,
                    lease.job,
                )
            else:
                sockets = [(self._listener(lease, p), target) for p, target in request.listeners]
                accept = " ".join(f"--accept {s.fileno()}:{target}" for s, target in sockets)
                tunnel = f'{python} "{guestd / "tunnel.py"}" {accept}'
                child = self.windows.start_tunnel(
                    tunnel, output, lease.tunnel_job, [s for s, _ in sockets]
                )
        except OSError as error:
            self.detach(lease, None)
            raise StationError("failed", f"{request.op} did not start: {error}") from error
        with self._changed:
            if self._leases.get(lane.name) is lease:
                lease.children.append(child)
                return lease, child
        child.kill()
        child.close()
        self._close_listeners(lease)
        raise StationError("unknown-lease", "the lease ended while its stream started")

    def detach(self, lease: Lease, child: Child | None) -> None:
        if child is not None:
            child.kill()
            child.close()
        with self._changed:
            if child in lease.children:
                lease.children.remove(child)
            lane = self.lanes[lease.lane]
            if self._leases.get(lease.lane) is lease:
                lane.sessions = max(0, lane.sessions - 1)
                if lane.sessions == 0:
                    lane.last_close = self.clock()
                self._save()

    def release(self, token: str) -> dict[str, object]:
        """End a lease; a token whose lease already ended releases nothing and is not an error."""
        with self._changed:
            try:
                lane = self._leased(token)
            except StationError:
                return {"released": False}
        return {"released": self._end(lane.name, "it was released")}

    def install_framework(
        self, identity: str, read: Callable[[int], bytes], size: int
    ) -> dict[str, object]:
        with self._changed:
            if self._installing:
                raise StationError("framework-busy", "another framework push is running")
            if leased := [name for name, lane in self.lanes.items() if lane.state == LEASED]:
                raise StationError(
                    "lanes-leased", f"{', '.join(leased)} leased; push when no Lane is leased"
                )
            # A Lane's reset ends what its lease ran from the framework only as it starts.
            if busy := [name for name, lane in self.lanes.items() if lane.state == RESETTING]:
                raise StationError("framework-busy", f"{', '.join(busy)} resetting; try again")
            self._installing = True
        try:
            self._install(identity, read, size)
        finally:
            with self._changed:
                self._installing = False
                self._changed.notify_all()
        self.log(f"framework {identity} installed")
        return {"framework": identity}

    # --- the reset ---

    def reset(self, name: str, lease: Lease | None) -> None:
        """Return a Lane to its baseline and check it: idle and one generation on if the check
        passes, quarantined with its account disabled if anything fails.

        A Lane without a baseline keeps its account, so that it can sign in for the first
        time and setup can take the baseline then.
        """
        started = self.clock()
        if lease is not None:
            self._end_processes(lease)
        try:
            baseline = self._baseline(name)
        except Exception as error:
            failures = [str(error)]
        else:
            try:
                self.windows.set_enabled(name, False)
                self._vacate(name, baseline.sid)
                self._restore(name, baseline)
                failures = self.check(name, baseline)
            except Exception as error:  # a bug too must leave the Lane quarantined
                failures = [f"{type(error).__name__}: {error}"]
        if not failures:
            try:
                self.windows.set_enabled(name, True)
            except OSError as error:
                failures = [f"its account could not be enabled: {error}"]
        with self._changed:
            lane = self.lanes[name]
            if failures:
                lane.state, lane.reason = QUARANTINED, "; ".join(failures)[:4000]
            else:
                lane.state, lane.reason, lane.generation = IDLE, "", lane.generation + 1
            self._save()
            self._changed.notify_all()
        took = self.clock() - started
        if failures:
            self.log(f"{name}: quarantined after {took:.0f}s: {lane.reason}")
        else:
            self.log(f"{name}: idle at generation {lane.generation} after {took:.0f}s")

    def check(self, name: str, baseline: Baseline) -> list[str]:
        """What keeps the Lane out of service, after its reset and before its account is back."""
        windows, failures = self.windows, []
        if pids := windows.processes(baseline.sid):
            failures.append(f"processes {pids[:5]} still run as {name}")
        code = windows.robocopy(
            compare_args(str(baseline.files), baseline.profile, baseline.junctions)
        )
        if compare_differs(code):
            failures.append(f"its profile differs from its baseline (robocopy exit {code})")
        current = windows.snapshot(baseline.profile)
        if differences := snapshot_differences(current, baseline.entries):
            failures.append("its profile differs from its baseline: " + "; ".join(differences[:5]))
        stage = str(self._stage(name))
        try:
            if windows.listdir(stage):
                failures.append("its stage directory is not empty")
            if not stage_holds(windows.security(stage)):
                failures.append("its stage directory admits an account other than the framework")
        except OSError as error:
            failures.append(f"its stage directory cannot be read: {error}")
        if tasks := windows.tasks(name, baseline.sid):
            failures.append(f"it still has scheduled tasks {tasks[:5]}")
        if owned := self._owned(baseline.sid):
            failures.append(f"it still owns {owned[:5]}")
        group = windows.sid(LANE_GROUP)
        for deny in recipe(windows.system_drive(), windows.volumes()):
            if not recipe_holds(windows.security(deny.path), deny, group):
                failures.append(f"{deny.path} no longer denies Lanes {deny.icacls}")
        failures += [
            f"{path} is on the machine's PATH and Lanes may write in it"
            for path in self._open_path()
        ]
        return failures

    # --- enrollment ---

    def enroll(self) -> tuple[list[str], bool]:
        """Prepare every Lane while the service is stopped: what was done, and whether all of
        it succeeded."""
        windows, lines, ok = self.windows, [], True
        group = windows.sid(LANE_GROUP)
        system_drive, volumes = windows.system_drive(), windows.volumes()
        for volume in _others(system_drive, volumes):
            if broad_write(windows.security(volume), AUTHENTICATED_USERS_SID):
                windows.icacls(volume, "/grant:r", f"*{AUTHENTICATED_USERS_SID}:(OI)(CI)(RX)")
                lines.append(f"{volume} is no longer writable by every signed-in account")
        for deny in recipe(system_drive, volumes):
            # icacls adds an entry each time it is asked, even one the path already has.
            if not recipe_holds(windows.security(deny.path), deny, group):
                windows.icacls(deny.path, "/deny", f"*{group}:{deny.icacls}")
        lines.append("isolation recipe applied")
        # What runs as SYSTEM finds programs on this PATH, so no Lane may put one there.
        for path in self._open_path():
            lines.append(f"{path} is on the machine's PATH and Lanes may write in it; fix that")
            ok = False
        with self._changed:
            self.lanes = load_state(self._state, self.config)
            for name, lane in self.lanes.items():
                quarantined = lane.reason if lane.state == QUARANTINED else None
                windows.make_private(str(self._stage(name)))
                if (self._baseline_folder(name) / "record.json").exists():
                    line = f"{name}: baseline {self._baseline_digest(name)}"
                else:
                    try:
                        digest = self._capture(name)
                    except (OSError, ResetError) as error:
                        line, ok = f"{name}: no baseline: {error}", False
                    else:
                        if digest:
                            line = f"{name}: captured baseline {digest}"
                            self._mark_resetting(lane)
                        else:
                            line = f"{name}: has not signed in yet; start its keep-alive connection"
                if quarantined is not None:
                    line += f"; back from quarantine ({quarantined})"
                    self._mark_resetting(lane)
                lines.append(line)
            self._save()
        return lines, ok

    # --- inside ---

    def _save(self) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        save_state(self._state, self.lanes, self.sleep)

    def _mark_resetting(self, lane: Lane) -> None:
        lane.state, lane.token, lane.sessions, lane.reason = RESETTING, "", 0, ""

    def _sessions(self) -> dict[str, Session]:
        return {s.user.casefold(): s for s in self.windows.sessions() if s.user and s.active}

    def _idle(self, resolution: tuple[int, int]) -> tuple[Lane | None, Session | None]:
        sessions = self._sessions()
        for name, lane in self.lanes.items():
            session = sessions.get(name.casefold())
            if lane.state == IDLE and session and (session.width, session.height) == resolution:
                return lane, session
        return None, None

    def _ready(self, framework: str) -> None:
        if self._installing:
            raise StationError("framework-busy", "a framework push is running")
        installed = self.framework()
        if installed != framework:
            raise StationError(
                "framework-mismatch", f"the Station runs framework {installed or 'none'}"
            )

    def _probe(self, name: str, timeout: float) -> None:
        probe = self.config.software[name].get("probe")
        if not probe:
            return
        code, output = self.windows.run(f'"{_cmd()}" /d /s /c "{probe}"', max(timeout, 1.0))
        if code != 0:
            raise StationError(
                "probe-failed", f"the probe of {name} failed (exit {code}): {output[-500:]}"
            )

    def _leased(self, token: str) -> Lane:
        match = _TOKEN.fullmatch(token)
        lane = self.lanes.get(match[1]) if match else None
        if (
            match is None
            or lane is None
            or lane.state != LEASED
            or lane.generation != int(match[2])
            or not hmac.compare_digest(lane.token, _digest(token))
        ):
            raise StationError("unknown-lease", "no lease has this token; it ended or never was")
        return lane

    def _end(self, name: str, why: str, only: Lease | None = None) -> bool:
        """End the Lane's lease, or only the lease ``only`` if that is still its lease."""
        with self._changed:
            lane = self.lanes[name]
            if lane.state != LEASED or (only is not None and self._leases.get(name) is not only):
                return False
            lease = self._leases.pop(name, None)
            self._mark_resetting(lane)
            try:
                self._save()
            except OSError as error:  # the reset saves again as it ends
                self.log(f"{name}: the lease store could not be saved: {error}")
        self.log(f"{name}: resetting, as {why}")
        self.background(lambda: self.reset(name, lease))
        return True

    def _end_processes(self, lease: Lease) -> None:
        for child in [*lease.children, *([lease.driver] if lease.driver else [])]:
            child.kill()
            child.close()
        for job in (lease.job, lease.tunnel_job):
            if job is not None:
                job.close()
        self._close_listeners(lease)

    def _listener(self, lease: Lease, port: int) -> socket.socket:
        """The lease's listening socket at ``port``, bound the first time a tunnel asks."""
        with self._changed:
            if port not in lease.listeners:
                lease.listeners[port] = self.windows.listen(lease.address, port)
            return lease.listeners[port]

    def _close_listeners(self, lease: Lease) -> None:
        with self._changed:
            listeners, lease.listeners = list(lease.listeners.values()), {}
        for listener in listeners:
            listener.close()

    def _account(self, name: str) -> tuple[str, str]:
        if name not in self._accounts:
            sid = self.windows.sid(name)
            profile = self.windows.profile(sid)
            if not profile:
                raise OSError(f"{name} has no profile")
            self._accounts[name] = (sid, profile)
        return self._accounts[name]

    def _start_driver(self, lease: Lease) -> None:
        """Start the Lane's Cua Driver as its user, on the lease's pipe, which no other process
        may hold first: whoever serves it sees and drives that Lane's desktop. guestd checks
        who serves the pipe before each call, since a name freed when the Driver exits can be
        taken by another account before it starts again."""
        if lease.driver is not None:
            lease.driver.close()
            lease.driver = None
        if self.windows.pipe_exists(lease.pipe):
            raise OSError(f"another process already serves {lease.pipe}")
        driver = self.root / "cua-driver" / "cua-driver.exe"
        started = self.windows.start_driver(
            lease.session,
            f'"{driver}" serve --socket \\\\.\\pipe\\{lease.pipe} --permission-mode unrestricted '
            "--dangerously-bypass-approvals",
        )
        with self._changed:
            if self._leases.get(lease.lane) is lease:
                lease.driver = started
                return
        started.kill()  # the lease ended meanwhile
        started.close()

    def _baseline_digest(self, name: str) -> str:
        try:
            return (self._baseline_folder(name) / "digest").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _captured(self, name: str) -> float:
        """When the Lane's baseline was taken: a stored credential written later is not the
        baseline's."""
        if name not in self._captures:
            record = self._baseline_folder(name) / "record.json"
            try:
                self._captures[name] = float(
                    json.loads(record.read_text(encoding="utf-8"))["captured"]
                )
            except (OSError, ValueError, KeyError) as error:
                raise OSError(
                    f"{name}'s baseline records no capture time; take it again"
                ) from error
        return self._captures[name]

    def _baseline(self, name: str) -> Baseline:
        sid = self.windows.sid(name)
        profile = self.windows.profile(sid)
        folder = self._baseline_folder(name)
        try:
            record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ResetError(f"{name} has no baseline yet; run setup_station.py") from None
        if not profile or profile.casefold() != str(record["profile"]).casefold():
            raise ResetError(f"the profile of {name} is not the one its baseline was taken from")
        return Baseline(sid, profile, folder / "profile", entries_from_json(record["entries"]))

    def _vacate(self, name: str, sid: str) -> None:
        """Sign the Lane out and wait until nothing of it runs and its hive is unloaded."""
        windows = self.windows
        for session in windows.sessions():
            if session.user.casefold() == name.casefold():
                windows.logoff(session.id)
        deadline = self.clock() + STOP_WAIT_SEC
        while pids := windows.processes(sid):
            if self.clock() > deadline:
                raise ResetError(f"processes {pids[:5]} of {name} would not stop")
            for pid in pids:
                windows.kill(pid)
            self.sleep(0.5)
        deadline = self.clock() + HIVE_WAIT_SEC
        while windows.hive_loaded(sid):
            if self.clock() > deadline:
                raise ResetError(f"the registry hive of {name} stayed loaded")
            self.sleep(1.0)

    def _restore(self, name: str, baseline: Baseline) -> None:
        """Make the Lane's profile anew from its baseline, and delete what the Lane left
        elsewhere.

        The old profile is moved aside whole and deleted last, never written into: the Lane
        may have opened a directory of it to another Lane, which then holds a handle that no
        access list takes back and can put a junction in it at any time. robocopy, and a
        walk by paths, would follow such a junction; the new profile is one only the reset
        has ever opened. Every deletion opens its entry by handle and goes ahead only if no
        directory on the way to it is a link (``Windows.remove``)."""
        windows, profile = self.windows, baseline.profile
        old = windows.retire(profile, self.clock() + STOP_WAIT_SEC)
        windows.make_private(profile)
        code = windows.robocopy(restore_args(str(baseline.files), profile))
        if copy_failed(code):
            raise ResetError(f"robocopy could not restore the profile (exit {code})")
        for path, target in junctions(baseline.entries):
            windows.make_junction(_join(profile, path), target)
        current = windows.snapshot(profile)
        for path, entry in baseline.entries.items():
            if path in current and current[path].security != entry.security:
                windows.apply_security(_join(profile, path), entry.security)
        windows.remove(old)
        stage, temp = str(self._stage(name)), str(self._temp(name))
        windows.remove(stage)
        windows.make_private(stage)
        windows.remove(temp)
        # What the Lane deleted waits in its recycle bin on each volume.
        drives = {v.rstrip("\\").upper() for v in (windows.system_drive(), *windows.volumes())}
        for drive in sorted(drives):
            windows.remove(f"{drive}\\$Recycle.Bin\\{baseline.sid}")
        windows.delete_tasks(name, baseline.sid)
        for path in self._owned(baseline.sid):
            windows.remove(path)

    def _open_path(self) -> list[str]:
        """The directories of the machine's PATH that a Lane may write in; for one that does
        not exist, the nearest directory above it, where a Lane could make it."""
        windows = self.windows
        lanes = {windows.sid(LANE_GROUP), *(windows.sid(name) for name in self.config.lanes)}
        found = []
        for directory in windows.machine_path():
            existing = directory.rstrip("\\") or directory
            while not windows.exists(existing) and ntpath.dirname(existing) != existing:
                existing = ntpath.dirname(existing)
            try:
                writable = lanes_change(windows.security(existing), lanes)
            except OSError:
                continue
            if writable and existing not in found:
                found.append(existing)
        return found

    def _owned(self, sid: str) -> list[str]:
        skip = {str(self.root).casefold()}
        found: list[str] = []
        for path, below in scan_roots(self.windows.system_drive(), self.windows.volumes()):
            found += self.windows.owned(path, below, skip, sid)
        return found

    def _capture(self, name: str) -> str:
        """Take the Lane's profile as its baseline, with the Lane signed out; its digest, or
        "" for a Lane that has no profile yet."""
        windows = self.windows
        sid = windows.sid(name)
        profile = windows.profile(sid)
        if not profile:
            return ""
        folder = self._baseline_folder(name)
        captured = self.clock()
        windows.set_enabled(name, False)
        try:
            self._vacate(name, sid)
            windows.remove(str(folder))
            folder.mkdir(parents=True)
            code = windows.robocopy(capture_args(profile, str(folder / "profile")))
            if copy_failed(code):
                raise ResetError(f"robocopy could not copy the profile (exit {code})")
            entries = windows.snapshot(profile)
            digest = baseline_digest(folder / "profile", entries)
            record = {
                "profile": profile,
                "digest": digest,
                "captured": captured,
                "entries": entries_json(entries),
            }
            (folder / "record.json").write_text(json.dumps(record), encoding="utf-8")
            (folder / "digest").write_text(digest + "\n", encoding="utf-8")
        finally:
            windows.set_enabled(name, True)
        return digest

    def _install(self, identity: str, read: Callable[[int], bytes], size: int) -> None:
        """Unpack the framework beside the installed one, then swap the two; a swap that fails
        part way puts the installed framework back, so the Station keeps serving it."""
        marker = self.root / "framework"
        before = self.framework()
        marker.unlink(missing_ok=True)
        staging = self.root / f".framework-{secrets.token_hex(4)}"
        archive, replaced = staging.with_suffix(".tar"), staging.with_suffix(".old")
        moved: list[str] = []
        try:
            with archive.open("wb") as stream:
                remaining = size
                while remaining:
                    chunk = read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise StationError("bad-request", "the framework ended early")
                    stream.write(chunk)
                    remaining -= len(chunk)
            staging.mkdir()
            with tarfile.open(archive) as bundle:
                parts = {PurePosixPath(member.name).parts[0] for member in bundle.getmembers()}
                if parts != set(FRAMEWORK_PARTS):
                    raise StationError(
                        "bad-request", f"a framework holds {', '.join(FRAMEWORK_PARTS)} only"
                    )
                bundle.extractall(staging, filter="data")
            replaced.mkdir()
            for part in FRAMEWORK_PARTS:
                if (self.root / part).exists():
                    (self.root / part).rename(replaced / part)
                    moved.append(part)
            for part in FRAMEWORK_PARTS:
                (staging / part).rename(self.root / part)
            self.windows.lock_framework(str(self.root))
            marker.write_text(identity + "\n", encoding="utf-8")
        except (OSError, tarfile.TarError) as error:
            if moved:
                with contextlib.suppress(OSError):
                    for part in FRAMEWORK_PARTS:
                        if (self.root / part).exists() and (replaced / part).exists():
                            (self.root / part).rename(staging / f"{part}.new")
                    for part in moved:
                        (replaced / part).rename(self.root / part)
                    if before:
                        marker.write_text(before + "\n", encoding="utf-8")
            message = f"the framework could not be installed: {error}"
            raise StationError("failed", message) from error
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(replaced, ignore_errors=True)


def _system32(name: str) -> str:
    """A program of Windows itself, by its full path: the machine's PATH also names other
    directories, which are not the system's."""
    return ntpath.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", name)


def _cmd() -> str:
    return _system32("cmd.exe")


def _size(session: Session | None) -> str | None:
    return f"{session.width}x{session.height}" if session else None


def _join(profile: str, path: str) -> str:
    return f"{profile}\\{path}" if path else profile


# --- one connection ----------------------------------------------------------------------


def read_line(stream: Stream) -> tuple[str, bytes]:
    """The request line, and what came after it in the same reads."""
    buffer = b""
    while b"\n" not in buffer:
        if len(buffer) > LINE_BYTES:
            raise StationError("bad-request", "the request line is too long")
        chunk = stream.read(LINE_BYTES)
        if not chunk:
            raise StationError("bad-request", "the connection ended before a request")
        buffer += chunk
    line, _, rest = buffer.partition(b"\n")
    try:
        return line.decode("utf-8").strip(), rest
    except UnicodeDecodeError:
        raise StationError("bad-request", "the request line is not UTF-8") from None


def serve_connection(station: Station, stream: Stream) -> None:
    """Answer one request, and for a stream request relay the process it starts."""
    try:
        line, head = read_line(stream)
        request = parse_request(line)
        if request.streams:
            lease, child = station.attach(request)
            try:
                stream.write(_line({"ok": True}))
                relay(stream, child, head)
            finally:
                station.detach(lease, child)
            return
        if request.op == "framework":
            pending = [head]

            def read(size: int) -> bytes:
                if pending and pending[0]:
                    chunk, pending[0] = pending[0][:size], pending[0][size:]
                    return chunk
                return stream.read(size)

            answer = station.install_framework(request.framework, read, request.size)
        elif request.op == "lease":
            answer = station.lease(request)
        elif request.op == "release":
            answer = station.release(request.token)
        elif request.op == "status":
            answer = station.status()
        else:
            answer = station.hello()
        reply = {"ok": True, **answer}
    except StationError as error:
        reply = {"ok": False, "error": error.code, "message": str(error)}
    except Exception as error:
        station.log(f"request failed: {type(error).__name__}: {error}")
        reply = {"ok": False, "error": "failed", "message": f"{type(error).__name__}: {error}"}
    with contextlib.suppress(OSError):
        stream.write(_line(reply))


def relay(stream: Stream, child: Child, head: bytes) -> None:
    """Splice ``stream`` to the child's standard input and output until either side ends."""

    def inbound() -> None:
        try:
            if head:
                child.write(head)
            while data := stream.read(CHUNK_BYTES):
                child.write(data)
        except OSError:
            pass
        finally:
            child.close_input()
            # The Host has gone; a process that does not end at its input's end is ended.
            if not child.wait(5.0):
                child.kill()

    thread = threading.Thread(target=inbound, daemon=True)
    thread.start()
    try:
        while data := child.read(CHUNK_BYTES):
            stream.write(data)
    except OSError:
        pass
    finally:
        stream.shutdown()
        child.kill()
        thread.join(10.0)


# --- Windows ------------------------------------------------------------------------------

_INVALID = ctypes.c_void_p(-1).value
_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND = 2, 3
_ERROR_HANDLE_EOF, _ERROR_BROKEN_PIPE, _ERROR_PIPE_BUSY = 38, 109, 231
_ERROR_PIPE_NOT_CONNECTED, _ERROR_PIPE_CONNECTED = 233, 535
_ERROR_OPERATION_ABORTED, _ERROR_IO_PENDING = 995, 997
_ERROR_NOT_FOUND, _ERROR_ALREADY_EXISTS, _ERROR_NO_DATA = 1168, 183, 232
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_DELETE, _READ_CONTROL, _WRITE_DAC, _WRITE_OWNER = 0x10000, 0x20000, 0x40000, 0x80000
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS, _OPEN_REPARSE_POINT, _OVERLAPPED = 0x02000000, 0x00200000, 0x40000000
_SQOS_IDENTIFICATION = 0x00100000 | 0x00010000
_OWNER_AND_DACL = 0x1 | 0x4
_SE_DACL_AUTO_INHERIT_REQ, _SE_DACL_AUTO_INHERITED = 0x0100, 0x0400
_SE_KERNEL_OBJECT = 6
_REPARSE_JUNCTION, _REPARSE_SYMLINK = 0xA0000003, 0xA000000C
_PIPE_ACCESS_DUPLEX, _FIRST_PIPE_INSTANCE = 0x3, 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x8
_WAIT_TIMEOUT = 0x102
_PROCESS_TERMINATE = 0x1
_HANDLE_FLAG_INHERIT = 0x1
_TOKEN_ALL_ACCESS, _MAXIMUM_ALLOWED = 0xF01FF, 0x02000000
_TOKEN_SESSION_ID, _TOKEN_PRIMARY, _SECURITY_IMPERSONATION = 12, 1, 2
_CREATE_UNICODE_ENVIRONMENT, _CREATE_NEW_CONSOLE = 0x400, 0x10
_EXTENDED_STARTUPINFO_PRESENT = 0x80000
_STARTF_USESHOWWINDOW, _STARTF_USESTDHANDLES = 0x1, 0x100
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST, _PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x20002, 0x2000D
_JOB_EXTENDED_LIMITS, _JOB_KILL_ON_CLOSE = 9, 0x2000
_WTS_USER_NAME, _WTS_CLIENT_DISPLAY, _WTS_ACTIVE = 5, 15, 0
_WTS_ANY_SESSION = 0xFFFFFFFE
_FILE_BASIC_INFO, _FILE_DISPOSITION_INFO, _FILE_DISPOSITION_INFO_EX = 0, 4, 21
_FILE_WRITE_ATTRIBUTES, _HIDDEN, _SYSTEM = FILE_WRITE_ATTRIBUTES, 0x2, 0x4
#: What a directory only the framework enters admits: SYSTEM and Administrators.
_PRIVATE = f"O:BAD:P(A;OICI;FA;;;{SYSTEM_SID})(A;OICI;FA;;;{ADMINISTRATORS_SID})"
#: Delete, at once even while open (POSIX semantics), read-only or not.
_DISPOSE = 0x1 | 0x2 | 0x10
_DRIVE_FIXED = 3


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class _OverlappedIO(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", w.DWORD),
        ("offset_high", w.DWORD),
        ("event", w.HANDLE),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("reserved", w.LPWSTR),
        ("desktop", w.LPWSTR),
        ("title", w.LPWSTR),
        ("x", w.DWORD),
        ("y", w.DWORD),
        ("x_size", w.DWORD),
        ("y_size", w.DWORD),
        ("x_chars", w.DWORD),
        ("y_chars", w.DWORD),
        ("fill", w.DWORD),
        ("flags", w.DWORD),
        ("show", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_bytes", w.LPBYTE),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", _StartupInfo), ("attributes", w.LPVOID)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class _SessionInfo(ctypes.Structure):
    _fields_ = [("id", w.DWORD), ("station", w.LPWSTR), ("state", ctypes.c_int)]


class _ClientDisplay(ctypes.Structure):
    _fields_ = [("width", w.DWORD), ("height", w.DWORD), ("depth", w.DWORD)]


class _Credential(ctypes.Structure):
    """CREDENTIALW."""

    _fields_ = [
        ("flags", w.DWORD),
        ("type", w.DWORD),
        ("target", w.LPWSTR),
        ("comment", w.LPWSTR),
        ("written", w.FILETIME),
        ("blob_size", w.DWORD),
        ("blob", w.LPBYTE),
        ("persist", w.DWORD),
        ("attribute_count", w.DWORD),
        ("attributes", w.LPVOID),
        ("alias", w.LPWSTR),
        ("user", w.LPWSTR),
    ]


def _unix(stamp: w.FILETIME) -> float:
    """A FILETIME (100 ns since 1601) as seconds since 1970."""
    return ((stamp.dwHighDateTime << 32) | stamp.dwLowDateTime) / 1e7 - 11644473600


class _ProcessInfoEx(ctypes.Structure):
    _fields_ = [
        ("session", w.DWORD),
        ("pid", w.DWORD),
        ("name", w.LPWSTR),
        ("sid", w.LPVOID),
        ("threads", w.DWORD),
        ("handles", w.DWORD),
        ("pagefile", w.DWORD),
        ("peak_pagefile", w.DWORD),
        ("working_set", w.DWORD),
        ("peak_working_set", w.DWORD),
        ("user_time", ctypes.c_longlong),
        ("kernel_time", ctypes.c_longlong),
    ]


class _Luid(ctypes.Structure):
    _fields_ = [("low", w.DWORD), ("high", w.LONG)]


class _Privileges(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("luid", _Luid), ("attributes", w.DWORD)]


class _BasicInfo(ctypes.Structure):
    """FILE_BASIC_INFO; a time of 0 is left as it is."""

    _fields_ = [
        ("created", ctypes.c_longlong),
        ("accessed", ctypes.c_longlong),
        ("written", ctypes.c_longlong),
        ("changed", ctypes.c_longlong),
        ("attributes", w.DWORD),
    ]


class _StreamData(ctypes.Structure):
    _fields_ = [("size", ctypes.c_longlong), ("name", ctypes.c_wchar * 296)]


class _JobLimits(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, with its basic limits laid out inline."""

    _fields_ = [
        ("per_process_time", ctypes.c_longlong),
        ("per_job_time", ctypes.c_longlong),
        ("flags", w.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("processes", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
        ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Native:
    """The Windows functions this file calls, declared once."""

    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)  # type: ignore[attr-defined]
        userenv = ctypes.WinDLL("userenv", use_last_error=True)  # type: ignore[attr-defined]
        H, D, B, P, S = w.HANDLE, w.DWORD, w.BOOL, w.LPVOID, w.LPCWSTR
        ref = ctypes.POINTER
        for dll, name, result, arguments in [
            (kernel, "CloseHandle", B, [H]),
            (kernel, "CreateFileW", H, [S, D, D, P, D, D, H]),
            (kernel, "CreateDirectoryW", B, [S, P]),
            (kernel, "GetFinalPathNameByHandleW", D, [H, w.LPWSTR, D, D]),
            (kernel, "CreateNamedPipeW", H, [S, D, D, D, D, D, D, P]),
            (kernel, "ConnectNamedPipe", B, [H, P]),
            (kernel, "WaitNamedPipeW", B, [S, D]),
            (kernel, "CreateEventW", H, [P, B, B, S]),
            (kernel, "ReadFile", B, [H, P, D, P, P]),
            (kernel, "WriteFile", B, [H, P, D, P, P]),
            (kernel, "GetOverlappedResult", B, [H, P, ref(D), B]),
            (kernel, "CancelIoEx", B, [H, P]),
            (kernel, "FlushFileBuffers", B, [H]),
            (kernel, "WaitForSingleObject", D, [H, D]),
            (kernel, "CreatePipe", B, [ref(H), ref(H), P, D]),
            (kernel, "SetHandleInformation", B, [H, D, D]),
            (kernel, "InitializeProcThreadAttributeList", B, [P, D, D, ref(ctypes.c_size_t)]),
            (kernel, "UpdateProcThreadAttribute", B, [P, D, ctypes.c_size_t, P,
                                                      ctypes.c_size_t, P, P]),
            (kernel, "DeleteProcThreadAttributeList", None, [P]),
            (kernel, "GetCurrentProcess", H, []),
            (kernel, "OpenProcess", H, [D, B, D]),
            (kernel, "TerminateProcess", B, [H, w.UINT]),
            (kernel, "LocalFree", P, [P]),
            (kernel, "GetDriveTypeW", w.UINT, [S]),
            (kernel, "SetFileInformationByHandle", B, [H, ctypes.c_int, P, D]),
            (kernel, "FindFirstStreamW", H, [S, ctypes.c_int, P, D]),
            (kernel, "FindNextStreamW", B, [H, P]),
            (kernel, "FindClose", B, [H]),
            (kernel, "CreateJobObjectW", H, [P, S]),
            (kernel, "SetInformationJobObject", B, [H, ctypes.c_int, P, D]),
            (kernel, "TerminateJobObject", B, [H, w.UINT]),
            (advapi, "OpenProcessToken", B, [H, D, ref(H)]),
            (advapi, "DuplicateTokenEx", B, [H, D, P, ctypes.c_int, ctypes.c_int, ref(H)]),
            (advapi, "SetTokenInformation", B, [H, ctypes.c_int, P, D]),
            (advapi, "CreateProcessAsUserW", B, [H, S, w.LPWSTR, P, P, B, D, P, S, P, P]),
            (advapi, "LookupAccountNameW", B, [S, S, P, ref(D), w.LPWSTR, ref(D), ref(D)]),
            (advapi, "LookupPrivilegeValueW", B, [S, S, ref(_Luid)]),
            (advapi, "AdjustTokenPrivileges", B, [H, B, P, D, P, P]),
            (advapi, "ConvertSidToStringSidW", B, [P, ref(w.LPWSTR)]),
            (advapi, "GetSecurityInfo", D, [H, ctypes.c_int, D, ref(P), ref(P), P, P, ref(P)]),
            (advapi, "GetKernelObjectSecurity", B, [H, D, P, D, ref(D)]),
            (advapi, "SetKernelObjectSecurity", B, [H, D, P]),
            (advapi, "SetSecurityDescriptorControl", B, [P, w.WORD, w.WORD]),
            (advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", B,
             [P, D, D, ref(w.LPWSTR), P]),
            (advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", B,
             [S, D, ref(P), P]),
            (wts, "WTSEnumerateSessionsW", B, [H, D, D, ref(P), ref(D)]),
            (wts, "WTSQuerySessionInformationW", B, [H, D, ctypes.c_int, ref(P), ref(D)]),
            (wts, "WTSEnumerateProcessesExW", B, [H, ref(D), D, ref(P), ref(D)]),
            (wts, "WTSFreeMemoryExW", B, [ctypes.c_int, P, D]),
            (wts, "WTSFreeMemory", None, [P]),
            (wts, "WTSLogoffSession", B, [H, D, B]),
            (wts, "WTSQueryUserToken", B, [w.ULONG, ref(H)]),
            (userenv, "CreateEnvironmentBlock", B, [ref(P), H, B]),
            (userenv, "DestroyEnvironmentBlock", B, [P]),
            (advapi, "ImpersonateLoggedOnUser", B, [H]),
            (advapi, "RevertToSelf", B, []),
            (advapi, "CredEnumerateW", B, [S, D, ref(D), ref(P)]),
            (advapi, "CredDeleteW", B, [S, D, D]),
            (advapi, "CredFree", None, [P]),
        ]:  # fmt: skip
            function = getattr(dll, name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)

    def check(self, ok: object) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    def opened(self, handle: int | None) -> int:
        if handle in (None, 0, _INVALID):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        return handle  # type: ignore[return-value]

    def sid_string(self, sid: int) -> str:
        text = w.LPWSTR()
        self.check(self.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return str(text.value)
        finally:
            self.LocalFree(text)

    def owner(self, handle: int, kind: int) -> str:
        owner, descriptor = w.LPVOID(), w.LPVOID()
        error = self.GetSecurityInfo(
            handle, kind, 0x1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
        )
        if error:
            raise ctypes.WinError(error)  # type: ignore[attr-defined]
        try:
            return self.sid_string(owner.value)  # type: ignore[arg-type]
        finally:
            self.LocalFree(descriptor)


class _Pipe:
    """One end of a named pipe connection, opened for overlapped I/O so that one thread can
    read while another writes; a synchronous pipe handle serializes the two."""

    def __init__(self, native: _Native, handle: int) -> None:
        self._native = native
        self._handle = handle
        self._reading = native.opened(native.CreateEventW(None, True, False, None))
        self._writing = native.opened(native.CreateEventW(None, True, False, None))
        self._down = False
        self._closed = False

    def read(self, size: int) -> bytes:
        if self._down:
            return b""
        buffer = ctypes.create_string_buffer(size)
        count = self._io(self._native.ReadFile, buffer, size, self._reading)
        return buffer.raw[:count]

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self._down:
                raise BrokenPipeError("the connection is shut down")
            chunk = bytes(view[:CHUNK_BYTES])
            count = self._io(self._native.WriteFile, chunk, len(chunk), self._writing)
            if count == 0:
                raise BrokenPipeError("the connection closed")
            view = view[count:]

    def shutdown(self) -> None:
        """Cancel what is pending, from any thread; every later read finds the end."""
        self._down = True
        self._native.CancelIoEx(self._handle, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._down:
            # Let the other side read the last reply before the pipe is gone.
            self._native.FlushFileBuffers(self._handle)
        for handle in (self._handle, self._reading, self._writing):
            self._native.CloseHandle(handle)

    def _io(self, function: Callable[..., int], buffer: object, size: int, event: int) -> int:
        native = self._native
        overlapped = _OverlappedIO(event=event)
        done = w.DWORD()
        if not function(self._handle, buffer, size, None, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED):
                return 0
            if error != _ERROR_IO_PENDING:
                raise ctypes.WinError(error)  # type: ignore[attr-defined]
        if not native.GetOverlappedResult(
            self._handle, ctypes.byref(overlapped), ctypes.byref(done), True
        ):
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_OPERATION_ABORTED):
                return 0
            raise ctypes.WinError(error)  # type: ignore[attr-defined]
        return done.value


class _Child:
    """A process the service started, with the service's ends of its standard streams.

    Each stream has its own lock, held while it is read or written, so that closing it from
    another thread never leaves a read or write on a descriptor number a new file reuses;
    ``kill`` comes first, so a blocked read or write ends at once.
    """

    def __init__(self, native: _Native, process: int, pid: int, stdin: int, stdout: int) -> None:
        self._native = native
        self._process = process
        self.pid = pid
        self._stdin = stdin
        self._stdout = stdout
        self._input = threading.Lock()
        self._output = threading.Lock()
        self._handle = threading.Lock()

    def read(self, size: int) -> bytes:
        with self._output:
            return os.read(self._stdout, size) if self._stdout >= 0 else b""

    def write(self, data: bytes) -> None:
        with self._input:
            if self._stdin < 0:
                raise BrokenPipeError("the process's input is closed")
            view = memoryview(data)
            while view:
                view = view[os.write(self._stdin, view) :]

    def close_input(self) -> None:
        with self._input:
            if self._stdin >= 0:
                os.close(self._stdin)
                self._stdin = -1

    def alive(self) -> bool:
        with self._handle:
            return bool(self._process) and (
                self._native.WaitForSingleObject(self._process, 0) == _WAIT_TIMEOUT
            )

    def wait(self, timeout: float) -> bool:
        with self._handle:
            process = self._process
        return not process or self._native.WaitForSingleObject(process, int(timeout * 1000)) == 0

    def kill(self) -> None:
        with self._handle:
            if self._process:
                self._native.TerminateProcess(self._process, 1)

    def close(self) -> None:
        self.kill()
        self.close_input()
        with self._output:
            if self._stdout >= 0:
                os.close(self._stdout)
                self._stdout = -1
        with self._handle:
            if self._process:
                self._native.CloseHandle(self._process)
                self._process = 0


class _Job:
    """A job that ends its processes, and what they started, when it is closed, or when the
    service holding it ends."""

    def __init__(self, native: _Native) -> None:
        self._native = native
        self.handle = native.opened(native.CreateJobObjectW(None, None))
        limits = _JobLimits(flags=_JOB_KILL_ON_CLOSE)
        native.check(native.SetInformationJobObject(
            self.handle, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
        ))  # fmt: skip

    def close(self) -> None:
        if self.handle:
            self._native.TerminateJobObject(self.handle, 1)
            self._native.CloseHandle(self.handle)
            self.handle = 0


Child = _Child
Job = _Job
Stream = _Pipe


class Windows:
    """What the service does to Windows. The unit tests replace it with a fake."""

    def __init__(self, native: _Native) -> None:
        self._native = native

    def enable_privileges(self) -> None:
        """Backup and restore let the reset read and set any entry's security, and delete it,
        whatever access list a Lane gave it; the rest start processes in other sessions."""
        native = self._native
        token = w.HANDLE()
        native.check(
            native.OpenProcessToken(native.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token))
        )
        try:
            for name in ("SeBackupPrivilege", "SeRestorePrivilege", "SeTakeOwnershipPrivilege",
                         "SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege",
                         "SeIncreaseQuotaPrivilege"):  # fmt: skip
                privilege = _Privileges(count=1, attributes=0x2)
                if native.LookupPrivilegeValueW(None, name, ctypes.byref(privilege.luid)):
                    native.AdjustTokenPrivileges(
                        token, False, ctypes.byref(privilege), 0, None, None
                    )
        finally:
            native.CloseHandle(token)

    # --- accounts and sessions ---

    def sid(self, account: str) -> str:
        native = self._native
        size, domain_size, use = w.DWORD(0), w.DWORD(0), w.DWORD()
        native.LookupAccountNameW(
            None, account, None, ctypes.byref(size), None, ctypes.byref(domain_size),
            ctypes.byref(use),
        )  # fmt: skip
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        sid = ctypes.create_string_buffer(size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value)
        native.check(
            native.LookupAccountNameW(
                None, account, sid, ctypes.byref(size), domain, ctypes.byref(domain_size),
                ctypes.byref(use),
            )
        )  # fmt: skip
        return native.sid_string(ctypes.addressof(sid))

    def profile(self, sid: str) -> str | None:
        import winreg

        key = rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as opened:
                value, _ = winreg.QueryValueEx(opened, "ProfileImagePath")
        except FileNotFoundError:
            return None
        return os.path.expandvars(str(value))

    def hive_loaded(self, sid: str) -> bool:
        import winreg

        for name in (sid, f"{sid}_Classes"):
            try:
                winreg.OpenKey(winreg.HKEY_USERS, name).Close()
                return True
            except FileNotFoundError:
                continue
        return False

    def set_enabled(self, account: str, enabled: bool) -> None:
        active = f"/active:{'yes' if enabled else 'no'}"
        self._checked([_system32("net.exe"), "user", account, active])

    def sessions(self) -> list[Session]:
        native, found = self._native, []
        table, count = w.LPVOID(), w.DWORD()
        native.check(native.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(table),
                                                  ctypes.byref(count)))  # fmt: skip
        try:
            infos = ctypes.cast(table, ctypes.POINTER(_SessionInfo))
            for index in range(count.value):
                info = infos[index]
                user = self._session_value(info.id, _WTS_USER_NAME)
                display = self._session_value(info.id, _WTS_CLIENT_DISPLAY)
                width, height = (display.width, display.height) if display else (0, 0)
                active = info.state == _WTS_ACTIVE
                found.append(Session(info.id, user or "", active, width, height))
        finally:
            native.WTSFreeMemory(table)
        return found

    def _session_value(self, session: int, kind: int):  # type: ignore[no-untyped-def]
        native = self._native
        buffer, size = w.LPVOID(), w.DWORD()
        if not native.WTSQuerySessionInformationW(
            None, session, kind, ctypes.byref(buffer), ctypes.byref(size)
        ):
            return None
        try:
            if kind == _WTS_CLIENT_DISPLAY:
                display = ctypes.cast(buffer, ctypes.POINTER(_ClientDisplay)).contents
                return _ClientDisplay(display.width, display.height, display.depth)
            return ctypes.wstring_at(buffer.value)  # type: ignore[arg-type]
        finally:
            native.WTSFreeMemory(buffer)

    def logoff(self, session: int) -> None:
        # A session may end on its own meanwhile; what still runs is killed next.
        self._native.WTSLogoffSession(None, session, True)

    def processes(self, sid: str) -> list[int]:
        native, found = self._native, []
        level, table, count = w.DWORD(1), w.LPVOID(), w.DWORD()
        native.check(native.WTSEnumerateProcessesExW(
            None, ctypes.byref(level), _WTS_ANY_SESSION, ctypes.byref(table), ctypes.byref(count)
        ))  # fmt: skip
        try:
            infos = ctypes.cast(table, ctypes.POINTER(_ProcessInfoEx))
            for index in range(count.value):
                info = infos[index]
                if info.sid and native.sid_string(info.sid) == sid:
                    found.append(int(info.pid))
        finally:
            native.WTSFreeMemoryExW(1, table, count)
        return found

    def kill(self, pid: int) -> None:
        native = self._native
        handle = native.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            native.TerminateProcess(handle, 1)
            native.CloseHandle(handle)

    def environment(self, session: int) -> dict[str, str]:
        """The environment Windows gives the session's user."""
        native = self._native
        token, block = self._user_token(session), w.LPVOID()
        try:
            native.check(native.CreateEnvironmentBlock(ctypes.byref(block), token, False))
        finally:
            native.CloseHandle(token)
        values: dict[str, str] = {}
        try:
            address = block.value or 0
            while entry := ctypes.wstring_at(address):
                key, _, value = entry.partition("=")
                if key:
                    values[key] = value
                address += (len(entry) + 1) * 2
        finally:
            native.DestroyEnvironmentBlock(block)
        return values

    # --- processes ---

    def job(self) -> Job:
        return _Job(self._native)

    def start_guestd(
        self, session: int, command: str, environment: dict[str, str], output: str, job: Job
    ) -> Child:
        """guestd as SYSTEM on the session's desktop: SYSTEM's own token, moved to the session."""
        token = self._own_token()
        try:
            moved = w.DWORD(session)
            self._native.check(self._native.SetTokenInformation(
                token, _TOKEN_SESSION_ID, ctypes.byref(moved), ctypes.sizeof(moved)
            ))  # fmt: skip
            return self._create(token, command, environment, "winsta0\\default", output, job)
        finally:
            self._native.CloseHandle(token)

    def start_tunnel(
        self, command: str, output: str, job: Job, sockets: Sequence[socket.socket]
    ) -> Child:
        """The tunnel as SYSTEM in session 0, inheriting the listening ``sockets`` it accepts on."""
        token = self._own_token()
        try:
            shared = [listener.fileno() for listener in sockets]
            return self._create(token, command, dict(os.environ), None, output, job, shared)
        finally:
            self._native.CloseHandle(token)

    def listen(self, address: str, port: int) -> socket.socket:
        """A socket listening at ``address`` and ``port`` that no other socket may bind as
        well, which only the processes it is handed to inherit."""
        listener = socket.socket()
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            listener.bind((address, port))
            listener.listen(64)
            listener.set_inheritable(True)
        except BaseException:
            listener.close()
            raise
        return listener

    def purge_credentials(self, session: int, since: float) -> list[str]:
        """Delete, as the session's user, every credential its Credential Manager holds that
        was written after ``since``; the targets deleted.

        The service asks for them on the user's behalf, impersonating the session's token:
        Windows keeps them in memory while any logon session of the user lives, beyond what
        the profile's files say."""
        native = self._native
        token = self._user_token(session)
        try:
            native.check(native.ImpersonateLoggedOnUser(token))
            try:
                count, table = w.DWORD(), w.LPVOID()
                if not native.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(table)):
                    error = ctypes.get_last_error()  # type: ignore[attr-defined]
                    if error == _ERROR_NOT_FOUND:
                        return []
                    raise ctypes.WinError(error)  # type: ignore[attr-defined]
                try:
                    pointers = ctypes.cast(table, ctypes.POINTER(ctypes.POINTER(_Credential)))
                    newer = [
                        (str(credential.target), credential.type)
                        for credential in (pointers[i].contents for i in range(count.value))
                        if _unix(credential.written) > since
                    ]
                finally:
                    native.CredFree(table)
                for target, kind in newer:
                    if not native.CredDeleteW(target, kind, 0):
                        error = ctypes.get_last_error()  # type: ignore[attr-defined]
                        if error != _ERROR_NOT_FOUND:
                            raise ctypes.WinError(error)  # type: ignore[attr-defined]
                return [target for target, _ in newer]
            finally:
                native.check(native.RevertToSelf())
        finally:
            native.CloseHandle(token)

    def start_driver(self, session: int, command: str) -> Child:
        """Cua Driver as the session's user, whose own pipe admits that user alone."""
        token = self._user_token(session)
        try:
            return self._create(token, command, self.environment(session), "winsta0\\default", None)
        finally:
            self._native.CloseHandle(token)

    def pipe_exists(self, name: str) -> bool:
        return name.casefold() in {entry.casefold() for entry in os.listdir("\\\\.\\pipe\\")}

    def run(self, command: str | list[str], timeout: float) -> tuple[int, str]:
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, errors="replace", timeout=timeout,
                cwd="C:\\", check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return -1, f"timed out after {timeout:g}s"
        return done.returncode, (done.stdout + done.stderr).strip()

    def _own_token(self) -> int:
        native = self._native
        own, primary = w.HANDLE(), w.HANDLE()
        process = native.GetCurrentProcess()
        native.check(native.OpenProcessToken(process, _TOKEN_ALL_ACCESS, ctypes.byref(own)))
        try:
            native.check(native.DuplicateTokenEx(
                own, _MAXIMUM_ALLOWED, None, _SECURITY_IMPERSONATION, _TOKEN_PRIMARY,
                ctypes.byref(primary),
            ))  # fmt: skip
        finally:
            native.CloseHandle(own)
        return primary.value  # type: ignore[return-value]

    def _user_token(self, session: int) -> int:
        token = w.HANDLE()
        self._native.check(self._native.WTSQueryUserToken(session, ctypes.byref(token)))
        return token.value  # type: ignore[return-value]

    def _create(
        self,
        token: int,
        command: str,
        environment: dict[str, str],
        desktop: str | None,
        output: str | None,
        job: Job | None = None,
        shared: Sequence[int] = (),
    ) -> Child:
        """Start ``command`` hidden, with ``token``, in ``job`` if one is given; with an
        ``output`` file it gets pipes for standard input and output and appends its errors to
        the file, without one it gets the null device. It inherits those handles and the
        inheritable ones ``shared`` names, which stay the caller's, and no other."""
        import msvcrt

        native = self._native
        inheritable = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        ours: list[int] = []
        theirs: list[int] = []
        try:
            if output is not None:
                stdin_read, stdin_write = w.HANDLE(), w.HANDLE()
                stdout_read, stdout_write = w.HANDLE(), w.HANDLE()
                for reader, writer in ((stdin_read, stdin_write), (stdout_read, stdout_write)):
                    native.check(native.CreatePipe(
                        ctypes.byref(reader), ctypes.byref(writer), ctypes.byref(inheritable), 0
                    ))  # fmt: skip
                ours += [stdin_write.value, stdout_read.value]  # type: ignore[list-item]
                theirs += [stdin_read.value, stdout_write.value]  # type: ignore[list-item]
                errors = native.opened(native.CreateFileW(
                    output, 0x4 | 0x100000, _SHARE_ALL, ctypes.byref(inheritable), 4, 0x80, None
                ))  # FILE_APPEND_DATA | SYNCHRONIZE, OPEN_ALWAYS  # fmt: skip
                theirs.append(errors)
                stdio = (theirs[0], theirs[1], errors)
            else:
                null = native.opened(native.CreateFileW(
                    "NUL", _GENERIC_READ | _GENERIC_WRITE, 0x3, ctypes.byref(inheritable),
                    _OPEN_EXISTING, 0, None,
                ))  # fmt: skip
                theirs.append(null)
                stdio = (null, null, null)
            for handle in ours:
                native.check(native.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0))
            inherited = [*theirs, *shared]
            process = self._spawn(token, command, environment, desktop, stdio, inherited, job)
        except BaseException:
            for handle in ours:
                native.CloseHandle(handle)
            raise
        finally:
            for handle in theirs:
                native.CloseHandle(handle)
        stdin, stdout = (
            (msvcrt.open_osfhandle(ours[0], os.O_WRONLY), msvcrt.open_osfhandle(ours[1], 0))
            if ours
            else (-1, -1)
        )
        return _Child(native, process.process, process.pid, stdin, stdout)

    def _spawn(
        self,
        token: int,
        command: str,
        environment: dict[str, str],
        desktop: str | None,
        stdio: tuple[int, int, int],
        inherited: list[int],
        job: Job | None,
    ) -> _ProcessInfo:
        native = self._native
        size, count = ctypes.c_size_t(), 1 if job is None else 2
        native.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        native.check(
            native.InitializeProcThreadAttributeList(attributes, count, 0, ctypes.byref(size))
        )
        try:
            handles = (w.HANDLE * len(inherited))(*inherited)
            native.check(native.UpdateProcThreadAttribute(
                attributes, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                ctypes.sizeof(handles), None, None,
            ))  # fmt: skip
            jobs = (w.HANDLE * 1)(job.handle if job is not None else 0)
            if job is not None:
                native.check(native.UpdateProcThreadAttribute(
                    attributes, 0, _PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs, ctypes.sizeof(jobs),
                    None, None,
                ))  # fmt: skip
            startup = _StartupInfoEx()
            startup.info.cb = ctypes.sizeof(_StartupInfoEx)
            startup.info.desktop = desktop
            startup.info.flags = _STARTF_USESHOWWINDOW | _STARTF_USESTDHANDLES
            startup.info.show = 0  # SW_HIDE
            startup.info.stdin, startup.info.stdout, startup.info.stderr = stdio
            startup.attributes = ctypes.cast(attributes, w.LPVOID)
            ordered = sorted(environment.items(), key=lambda item: item[0].upper())
            block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in ordered) + "\0")
            process = _ProcessInfo()
            native.check(native.CreateProcessAsUserW(
                token, None, ctypes.create_unicode_buffer(command), None, None, True,
                _CREATE_UNICODE_ENVIRONMENT | _CREATE_NEW_CONSOLE | _EXTENDED_STARTUPINFO_PRESENT,
                block, "C:\\", ctypes.byref(startup), ctypes.byref(process),
            ))  # fmt: skip
        finally:
            native.DeleteProcThreadAttributeList(attributes)
        native.CloseHandle(process.thread)
        return process

    # --- files ---

    def system_drive(self) -> str:
        return os.environ.get("SYSTEMDRIVE", "C:")

    def volumes(self) -> list[str]:
        drives = os.listdrives()  # type: ignore[attr-defined]
        return [drive for drive in drives if self._native.GetDriveTypeW(drive) == _DRIVE_FIXED]

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def machine_path(self) -> list[str]:
        """The directories of the machine's PATH, which what runs as SYSTEM searches."""
        import winreg

        key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as opened:
            value, _ = winreg.QueryValueEx(opened, "Path")
        return [
            ntpath.normpath(winreg.ExpandEnvironmentStrings(part))
            for part in str(value).split(";")
            if part.strip()
        ]

    def security(self, path: str) -> str:
        """An entry's owner and DACL as SDDL, read with backup semantics: whatever its access
        list says, and of a link itself rather than its target."""
        native = self._native
        handle = self._open(path, _READ_CONTROL)
        try:
            needed = w.DWORD()
            native.GetKernelObjectSecurity(handle, _OWNER_AND_DACL, None, 0, ctypes.byref(needed))
            descriptor = ctypes.create_string_buffer(needed.value)
            native.check(native.GetKernelObjectSecurity(
                handle, _OWNER_AND_DACL, descriptor, needed, ctypes.byref(needed)
            ))  # fmt: skip
            text = w.LPWSTR()
            native.check(native.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, _OWNER_AND_DACL, ctypes.byref(text), None
            ))  # fmt: skip
            try:
                return str(text.value)
            finally:
                native.LocalFree(text)
        finally:
            native.CloseHandle(handle)

    def apply_security(self, path: str, sddl: str) -> None:
        """Set exactly this owner and DACL, inherited entries included, on this entry alone.

        A DACL marked auto-inherited (``AI``) keeps the mark only when the descriptor also
        asks for it; it then takes its inherited entries from the descriptor as well."""
        native = self._native
        descriptor = w.LPVOID()
        native.check(native.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ))  # fmt: skip
        try:
            if "AI" in sddl_dacl(sddl)[0]:
                inherited = _SE_DACL_AUTO_INHERIT_REQ | _SE_DACL_AUTO_INHERITED
                native.check(native.SetSecurityDescriptorControl(descriptor, inherited, inherited))
            handle = self._open(path, _WRITE_DAC | _WRITE_OWNER)
            try:
                native.check(native.SetKernelObjectSecurity(handle, _OWNER_AND_DACL, descriptor))
            finally:
                native.CloseHandle(handle)
        finally:
            native.LocalFree(descriptor)

    def snapshot(self, root: str) -> dict[str, Entry]:
        """Every entry below ``root``, by path relative to it, without following links. A
        directory closed to SYSTEM is opened to it, so that no link hides from the reset."""
        entries = {"": self._entry(root, os.lstat(root))}
        pending = [(root, "")]
        while pending:
            directory, relative = pending.pop()
            for item, info in self._scan(directory, unlock=True):
                path = f"{relative}\\{item.name}" if relative else item.name
                entries[path] = self._entry(item.path, info)
                if stat.S_ISDIR(info.st_mode) and not _reparse(info):
                    pending.append((item.path, path))
        return entries

    def owned(self, root: str, below: bool, skip: set[str], sid: str) -> list[str]:
        """Entries in ``root`` that ``sid`` owns; with ``below``, in its subdirectories too.
        A directory the Lane owns is reported whole."""
        found: list[str] = []
        pending = [root]
        while pending:
            for item, info in self._scan(pending.pop()):
                if item.path.casefold() in skip:
                    continue
                try:
                    owner = sddl_owner(self.security(item.path))
                except OSError:
                    continue
                if owner == sid:
                    found.append(item.path)
                elif below and stat.S_ISDIR(info.st_mode) and not _reparse(info):
                    pending.append(item.path)
        return found

    def remove(self, path: str) -> None:
        """Delete a file, a link (not its target) or a directory tree, whatever access list a
        Lane gave it. Never robocopy: emptying a tree, it deletes through the junctions in
        it what they point to, with the backup privilege that ignores every access list."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        found = [path]
        pending = [path] if stat.S_ISDIR(info.st_mode) and not _reparse(info) else []
        while pending:
            for item, entry in self._scan(pending.pop(), unlock=True):
                found.append(item.path)
                if stat.S_ISDIR(entry.st_mode) and not _reparse(entry):
                    pending.append(item.path)
        # Every directory comes before what it holds, so in reverse each is empty in turn.
        for each in reversed(found):
            self._delete(each)

    def retire(self, path: str, deadline: float) -> str:
        """Move the directory ``path`` aside whole, to ``<path>.ale-old``, and say where.

        Its own access list first becomes SYSTEM's and Administrators', so that nothing opens
        it anew; until ``deadline`` (``time.time``) the move is tried again while a process
        still holds an entry of it open. What an earlier reset left there is deleted first,
        unless that reset failed before the profile was made anew."""
        old = path + ".ale-old"
        if not os.path.lexists(path):
            return old
        self.remove(old)
        self.apply_security(path, _PRIVATE)
        while True:
            try:
                os.rename(path, old)
                return old
            except PermissionError as error:
                if time.time() > deadline:
                    raise OSError(f"{path} could not be moved aside: {error}") from error
                time.sleep(1.0)

    def make_junction(self, path: str, target: str) -> None:
        """A junction, hidden and a system file, as Windows makes a profile's own."""
        self._checked([_cmd(), "/d", "/c", "mklink", "/J", path, target])
        native = self._native
        handle = self._open(path, _FILE_WRITE_ATTRIBUTES)
        try:
            basic = _BasicInfo(attributes=_HIDDEN | _SYSTEM)
            native.check(native.SetFileInformationByHandle(
                handle, _FILE_BASIC_INFO, ctypes.byref(basic), ctypes.sizeof(basic)
            ))  # fmt: skip
        finally:
            native.CloseHandle(handle)

    def make_private(self, path: str) -> None:
        """A directory only SYSTEM and Administrators may enter: made so, or, if it exists,
        given that access list, whatever an existing one granted."""
        native = self._native
        os.makedirs(ntpath.dirname(path), exist_ok=True)
        descriptor = w.LPVOID()
        native.check(native.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _PRIVATE, 1, ctypes.byref(descriptor), None
        ))  # fmt: skip
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if native.CreateDirectoryW(path, ctypes.byref(attributes)):
                return
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error != _ERROR_ALREADY_EXISTS:
                raise ctypes.WinError(error)  # type: ignore[attr-defined]
        finally:
            native.LocalFree(descriptor)
        self.apply_security(path, _PRIVATE)

    def icacls(self, path: str, *arguments: str) -> None:
        # Long: a change to an inherited entry is carried to everything below the path.
        self._checked([_system32("icacls.exe"), path, *arguments, "/C", "/Q"], timeout=3600)

    def lock_framework(self, root: str) -> None:
        """Every account may run Cua Driver, as the agent's ``cua-desktop`` tool does; the
        rest of the framework keeps the root's SYSTEM and Administrators only."""
        self.icacls(os.path.join(root, "cua-driver"), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)")

    def robocopy(self, arguments: list[str]) -> int:
        done = subprocess.run([_system32("robocopy.exe"), *arguments], capture_output=True,
                              timeout=3600, check=False)  # fmt: skip
        return done.returncode

    def tasks(self, account: str, sid: str) -> list[str]:
        code, output = self.run(self._task_script(account, sid, "$_.TaskPath + $_.TaskName"), 120)
        if code != 0:
            raise OSError(f"scheduled tasks could not be listed: {output[-300:]}")
        return [line for line in output.splitlines() if line.startswith("\\")]

    def delete_tasks(self, account: str, sid: str) -> None:
        script = self._task_script(account, sid, "Unregister-ScheduledTask -InputObject $_ "
                                   "-Confirm:$false")  # fmt: skip
        code, output = self.run(script, 300)
        if code != 0:
            raise OSError(f"scheduled tasks could not be deleted: {output[-300:]}")

    def os_build(self) -> str:
        version = sys.getwindowsversion()  # type: ignore[attr-defined]
        return f"{version.major}.{version.minor}.{version.build}"

    def _task_script(self, account: str, sid: str, action: str) -> list[str]:
        if not _LANE.fullmatch(account) or not _SID.fullmatch(sid):
            raise ValueError(f"not an account and SID: {account!r} {sid!r}")
        script = (
            "$ErrorActionPreference = 'Stop'; Get-ScheduledTask | Where-Object { "
            "$user = [string]$_.Principal.UserId; "
            f"$user -eq '{sid}' -or $user.Split('\\')[-1] -eq '{account}' }} | "
            f"ForEach-Object {{ {action} }}"
        )
        return [
            _system32(r"WindowsPowerShell\v1.0\powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]

    def _scan(
        self, directory: str, *, unlock: bool = False
    ) -> list[tuple[os.DirEntry[str], os.stat_result]]:
        """A directory's entries; with ``unlock``, a directory SYSTEM may not list is first
        given to SYSTEM alone, as only the reset's own directories may be."""
        try:
            with os.scandir(directory) as entries:
                return [(entry, entry.stat(follow_symlinks=False)) for entry in entries]
        except PermissionError:
            if not unlock:
                return []
        except OSError:
            return []
        self.apply_security(directory, f"O:{SYSTEM_SID}D:P(A;OICI;FA;;;{SYSTEM_SID})")
        return self._scan(directory)

    def _entry(self, path: str, info: os.stat_result) -> Entry:
        try:
            security = self.security(path)
        except OSError as error:
            security = f"unreadable: {error.winerror}"  # type: ignore[attr-defined]
        if _reparse(info):
            tag = info.st_reparse_tag  # type: ignore[attr-defined]
            if tag in (_REPARSE_JUNCTION, _REPARSE_SYMLINK):
                kind = "junction" if tag == _REPARSE_JUNCTION else "symlink"
                return Entry(security, f"{kind}:{os.readlink(path).removeprefix('\\\\?\\')}")
            return Entry(security, f"reparse:{tag:#x}")
        return Entry(security, "", self._streams(path))

    def _streams(self, path: str) -> tuple[str, ...]:
        native = self._native
        data = _StreamData()
        handle = native.FindFirstStreamW(path, 0, ctypes.byref(data), 0)
        if handle in (None, _INVALID):
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            return () if error == _ERROR_HANDLE_EOF else (f"unreadable: {error}",)
        names = []
        try:
            while True:
                if data.name != "::$DATA":
                    names.append(data.name)
                if not native.FindNextStreamW(handle, ctypes.byref(data)):
                    break
        finally:
            native.FindClose(handle)
        return tuple(sorted(names))

    def _delete(self, path: str) -> None:
        native = self._native
        try:
            handle = self._open(path, _DELETE)
        except FileNotFoundError:
            return
        try:
            flags = w.DWORD(_DISPOSE)
            if not native.SetFileInformationByHandle(
                handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), ctypes.sizeof(flags)
            ):
                delete = ctypes.c_ubyte(1)
                native.check(native.SetFileInformationByHandle(
                    handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete)
                ))  # fmt: skip
        finally:
            native.CloseHandle(handle)

    def _open(self, path: str, access: int) -> int:
        """A handle to ``path`` itself, a link rather than what it leads to, whatever its
        access list says, and only if no directory on the way to it is a link.

        A Lane may swap a directory it can write for a junction at any moment, so a path the
        reset walks may lead elsewhere by the time it is opened: once opened, the handle is
        checked to name the very path that was meant, and only the handle is used."""
        native = self._native
        handle = native.CreateFileW(path, access, _SHARE_ALL, None, _OPEN_EXISTING,
                                    _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT, None)  # fmt: skip
        if handle in (None, _INVALID):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            final = self._final_path(handle)
            if final.casefold() != ntpath.normpath(path).casefold():
                raise OSError(f"{path} leads to {final} through a link")
        except BaseException:
            native.CloseHandle(handle)
            raise
        return handle

    def _final_path(self, handle: int) -> str:
        native, size = self._native, 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = native.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if not length:
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
            if length < size:
                break
            size = length + 1
        text = buffer.value
        if text.startswith("\\\\?\\UNC\\"):
            return "\\\\" + text[8:]
        return text.removeprefix("\\\\?\\")

    def _checked(self, command: list[str], timeout: float = 300) -> None:
        code, output = self.run(command, timeout)
        if code != 0:
            raise OSError(f"{command[0]} {command[1]} failed (exit {code}): {output[-300:]}")


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


# --- the pipe server, the service and the client ----------------------------------------


class _PipeServer:
    """Accepts connections on ``PIPE``: from this machine only, and from SYSTEM and the Host's
    account only. Its first instance, made as it is built, claims the name: the service fails
    to start, before it resets a Lane, when another process holds it."""

    def __init__(self, native: _Native, host_sid: str, stopping: threading.Event) -> None:
        self._native = native
        self._stopping = stopping
        self._descriptor = w.LPVOID()
        sddl = f"O:SYG:SYD:P(A;;GA;;;SY)(A;;GRGW;;;{host_sid})"
        native.check(native.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(self._descriptor), None
        ))  # fmt: skip
        self._attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), self._descriptor, False
        )
        self._next = self._instance(first=True)

    def accept(self) -> _Pipe | None:
        """The next connection; None once the service stops. A client that leaves before it
        is accepted, such as an ``ssh`` killed meanwhile, costs only its instance."""
        while True:
            handle, self._next = self._next, None
            connected = self._connect(handle)
            if connected is None and self._stopping.is_set():
                return None
            self._next = self._instance(first=False)
            if connected is not None:
                return _Pipe(self._native, connected)

    def _instance(self, *, first: bool) -> int:
        native = self._native
        mode = _PIPE_ACCESS_DUPLEX | _OVERLAPPED | (_FIRST_PIPE_INSTANCE if first else 0)
        return native.opened(native.CreateNamedPipeW(
            PIPE, mode, _PIPE_REJECT_REMOTE_CLIENTS, 255, CHUNK_BYTES, CHUNK_BYTES, 0,
            ctypes.byref(self._attributes),
        ))  # fmt: skip

    def _connect(self, handle: int) -> int | None:
        """Wait for a client on ``handle``: the handle once one connected, or None, the
        handle closed, when the service stops or the client left first."""
        native = self._native
        event = native.opened(native.CreateEventW(None, True, False, None))
        overlapped = _OverlappedIO(event=event)
        try:
            if native.ConnectNamedPipe(handle, ctypes.byref(overlapped)):
                return handle
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error == _ERROR_PIPE_CONNECTED:
                return handle
            if error == _ERROR_IO_PENDING:
                while native.WaitForSingleObject(event, 500) == _WAIT_TIMEOUT:
                    if self._stopping.is_set():
                        native.CancelIoEx(handle, ctypes.byref(overlapped))
                        native.GetOverlappedResult(
                            handle, ctypes.byref(overlapped), ctypes.byref(w.DWORD()), True
                        )
                        native.CloseHandle(handle)
                        return None
                if native.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(w.DWORD()), False
                ):
                    return handle
                error = ctypes.get_last_error()  # type: ignore[attr-defined]
            native.CloseHandle(handle)
            if error in (_ERROR_NO_DATA, _ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED):
                return None
            raise ctypes.WinError(error)  # type: ignore[attr-defined]
        finally:
            native.CloseHandle(event)


class _Log:
    """The service's log, ``station\\service.log``, kept to a few megabytes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists() and self._path.stat().st_size > 8 * 1024 * 1024:
                os.replace(self._path, self._path.with_suffix(".log.1"))
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(f"{stamp} {message}\n")


def _file_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _serve(stopping: threading.Event) -> None:
    native = _Native()
    windows = Windows(native)
    windows.enable_privileges()
    log = _Log(ROOT / "station" / "service.log")
    station = Station(Config.read(ROOT / "station" / "station.json"), windows, ROOT,
                      digest=_file_digest(), log=log)  # fmt: skip
    server = _PipeServer(native, windows.sid(HOST_ACCOUNT), stopping)
    station.start()

    def answer(pipe: _Pipe) -> None:
        try:
            serve_connection(station, pipe)
        finally:
            pipe.close()

    def accept() -> None:
        try:
            while (pipe := server.accept()) is not None:
                threading.Thread(target=answer, args=(pipe,), daemon=True).start()
        except OSError as error:
            log(f"the pipe stopped accepting: {error}")
            stopping.set()

    threading.Thread(target=accept, daemon=True).start()
    log(f"started, protocol {PROTOCOL}")
    while not stopping.wait(1.0):
        try:
            station.tick()
        except Exception as error:  # the next tick tries again; the service goes on
            log(f"tick failed: {type(error).__name__}: {error}")
    station.stop()
    log("stopped")


def _run_service() -> None:
    """Hand this process to the service control manager until the service stops."""
    native = _Native()
    stopping = threading.Event()
    status = w.HANDLE()
    service_main = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))  # type: ignore[attr-defined]
    handler = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)  # type: ignore[attr-defined]
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]

    class TableEntry(ctypes.Structure):
        _fields_ = [("name", w.LPWSTR), ("proc", service_main)]

    class Status(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in ("type", "state", "accepted", "exit", "specific",
                                                 "checkpoint", "hint")]  # fmt: skip

    advapi.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
    advapi.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, handler, w.LPVOID]
    advapi.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(Status)]
    advapi.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(TableEntry)]

    def report(state: int, failure: int = 0) -> None:
        accepted = 0x1 | 0x4 if state == 4 else 0  # stop and shutdown, while running
        advapi.SetServiceStatus(status, ctypes.byref(Status(0x10, state, accepted, failure)))

    @handler
    def control(code, event, data, context):  # type: ignore[no-untyped-def]
        if code in (0x1, 0x5):  # stop, shutdown
            stopping.set()
        return 0

    @service_main
    def main(argc, argv):  # type: ignore[no-untyped-def]
        status.value = advapi.RegisterServiceCtrlHandlerExW(SERVICE, control, None)
        report(4)  # running
        failure = 0
        try:
            _serve(stopping)
        except Exception as error:
            _Log(ROOT / "station" / "service.log")(f"failed: {type(error).__name__}: {error}")
            failure = 1
        report(1, failure)  # stopped

    table = (TableEntry * 2)(TableEntry(SERVICE, main), TableEntry(None, service_main()))
    native.check(advapi.StartServiceCtrlDispatcherW(table))


def _connect(native: _Native) -> _Pipe:
    """The service's pipe, once its owner is shown to be SYSTEM: a Lane that took the name
    while the service was down must not receive the Host's tokens."""
    deadline = time.monotonic() + 30
    while True:
        handle = native.CreateFileW(PIPE, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING,
                                    _OVERLAPPED | _SQOS_IDENTIFICATION, None)  # fmt: skip
        if handle not in (None, _INVALID):
            break
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error == _ERROR_PIPE_BUSY and time.monotonic() < deadline:
            native.WaitNamedPipeW(PIPE, 2000)
            continue
        if error == _ERROR_FILE_NOT_FOUND:
            raise OSError("ale-stationd is not running")
        raise ctypes.WinError(error)  # type: ignore[attr-defined]
    owner = native.owner(handle, _SE_KERNEL_OBJECT)
    if owner != SYSTEM_SID:
        native.CloseHandle(handle)
        raise OSError(f"{PIPE} is held by {owner}, not by ale-stationd")
    return _Pipe(native, handle)


def _client() -> int:
    """Pass the SSH request to the service and splice standard input and output to it."""
    line = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    try:
        request = parse_request(line)
        pipe = _connect(_Native())
    except (StationError, OSError) as error:
        sys.stderr.write(f"ale-stationd: {error}\n")
        return 2

    def write_out(data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(1, view) :]

    def inbound() -> None:
        try:
            while data := os.read(0, CHUNK_BYTES):
                pipe.write(data)
        except OSError:
            pass
        # The Host is done with a stream once its input ends; a reply still comes for the rest.
        if request.streams:
            pipe.shutdown()

    pipe.write(line.encode() + b"\n")
    threading.Thread(target=inbound, daemon=True).start()
    try:
        first, rest = read_line(pipe)
        reply = json.loads(first)
    except (StationError, ValueError) as error:
        sys.stderr.write(f"ale-stationd: no reply: {error}\n")
        return 1
    if not request.streams or not reply.get("ok"):
        if request.streams:
            sys.stderr.write(f"ale-stationd: {reply.get('error')}: {reply.get('message')}\n")
        else:
            write_out(first.encode() + b"\n")
        sys.stdout.flush()
        os._exit(0 if reply.get("ok") else 1)
    write_out(rest)
    while data := pipe.read(CHUNK_BYTES):
        write_out(data)
    os._exit(0)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["client"]:
        return _client()
    if args == ["service"]:
        _run_service()
        return 0
    if args == ["enroll"]:
        native = _Native()
        windows = Windows(native)
        windows.enable_privileges()
        station = Station(Config.read(ROOT / "station" / "station.json"), windows, ROOT,
                          digest=_file_digest())  # fmt: skip
        lines, ok = station.enroll()
        for line in lines:
            print(line, flush=True)
        return 0 if ok else 1
    sys.stderr.write("usage: stationd.py service | client | enroll\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
