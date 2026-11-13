"""A macOS image is framed once, offline on the Mac, booted once and kept as a local image."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from ale.core.errors import ProviderStartError
from ale.core.sandbox import DesktopResolution, ImageKind, PreparedTaskImage
from ale.run.framework import MACOS_FRAMER
from ale.run.providers import orchard_client, orchard_framing
from ale.run.providers.orchard_client import LocalOrchard, OrchardExecResult, OwnedVM

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64
REF = "registry.example/macos@" + DIGEST
IMAGE = PreparedTaskImage(
    kind=ImageKind.VM,
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref=REF,
    prepared_identity=DIGEST,
    resolved_reference=REF,
    base_materials=(REF,),
)
LABEL = "org.example.agent"
PENDING = f"""
 Records for UID -2 : FFFFEEEE-DDDD-CCCC-BBBB-AAAAFFFFFFFE
 #1:
          Disposition: [enabled, allowed, not notified] (0x3)
           Identifier: 16.org.example.daemon
 Records for UID 501 : B1DCA4A6-BDA4-4F90-9AF8-EAE6062EDF26
 #1:
          Disposition: [enabled, allowed, not notified] (0x3)
           Identifier: 8.{LABEL}
    Parent Identifier: Example, Inc.
"""
SETTLED = PENDING.replace(
    "allowed, not notified] (0x3)\n           Identifier: 8.",
    "allowed, notified] (0xb)\n           Identifier: 8.",
)


class Controller:
    """The local Orchard while the framed disk boots once: who is signed in, and BTM."""

    def __init__(self, *, consoles=("admin",), dumps=(SETTLED,)):
        self.consoles, self.dumps = list(consoles), list(dumps)
        self.vm: dict | None = None
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.execs: list[list[str]] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_vm(self, name):
        return dict(self.vm) if self.vm and self.vm["name"] == name else None

    async def create_vm(self, spec):
        self.created.append(dict(spec))
        self.vm = dict(spec) | {"uid": "uid-1", "status": "running", "generation": 1}
        return dict(self.vm)

    async def update_vm(self, name, spec):
        assert spec["powerState"] == "stopped"
        self.vm.update(spec)
        self.vm.update(
            generation=2,
            observedGeneration=2,
            conditions=[{"type": "running", "state": "false"}],
        )
        return dict(self.vm)

    async def delete_vm(self, name):
        self.deleted.append(name)
        self.vm = None

    async def exec(self, name, argv, *, stdin=b"", timeout_sec=120, deadline=None):
        self.execs.append(list(argv))
        if argv[0] == "/usr/bin/stat":
            console = self.consoles.pop(0) if len(self.consoles) > 1 else self.consoles[0]
            return OrchardExecResult(0, console.encode() + b"\n", b"")
        if argv[:2] == ["/bin/sh", "-c"]:
            return OrchardExecResult(0, LABEL.encode() + b"\n", b"")
        if orchard_framing._DISMISS_NOTICES in argv or argv[2:] == orchard_framing.FORGET_DISPLAYS:
            return OrchardExecResult(0, b"", b"")
        assert argv == ["sudo", "-n", "/usr/bin/sfltool", "dumpbtm"]
        dump = self.dumps.pop(0) if len(self.dumps) > 1 else self.dumps[0]
        return OrchardExecResult(0, dump.encode(), b"")


class Local:
    def __init__(self, controller: Controller, *, frame_error: Exception | None = None):
        self.controller, self.frame_error = controller, frame_error
        self.images: set[str] = set()
        self.tarts: list[tuple[str, ...]] = []
        self.framed: list[tuple[str, str, str]] = []
        self.saved: list[tuple[str, str]] = []
        self.shutdowns: list[str] = []

    def client(self) -> Controller:
        return self.controller

    async def tart(self, *args, timeout_sec=600):
        self.tarts.append(args)
        return ""

    async def local_images(self) -> set[str]:
        return set(self.images)

    async def frame_disk(self, name, *, user, identity, payload):
        assert payload == b"framework"
        self.framed.append((name, user, identity))
        if self.frame_error:
            raise self.frame_error
        return {"agent_home": f"/Users/{user}", "agent_uid": 501}

    async def shut_down(self, client, owned) -> None:
        assert client is self.controller
        self.shutdowns.append(owned.name)
        owned.stopped = True

    async def save(self, owned, name: str) -> None:
        assert owned.stopped and self.shutdowns[-1] == owned.name
        self.saved.append((owned.name, name))
        self.images.add(name)


@pytest.fixture(autouse=True)
def framing(tmp_path, monkeypatch):
    monkeypatch.setattr(orchard_framing, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(orchard_framing, "macos_framework_payload", lambda: b"framework")
    monkeypatch.setattr(orchard_framing, "_NOTICE_GRACE_SEC", 0)
    monkeypatch.setattr(orchard_framing.asyncio, "sleep", _no_wait)


_real_sleep = orchard_framing.asyncio.sleep


async def _no_wait(seconds):
    await _real_sleep(0)


async def test_an_image_is_framed_offline_booted_once_and_then_reused():
    controller = Controller()
    local = Local(controller)
    framed = await orchard_framing.frame_image(local, IMAGE, user="admin")
    clone, delete = local.tarts[0], local.tarts[-1]
    # A reference Tart's cache lacks is pulled onto the cached image it shares most with.
    assert clone[:3] == ("clone", "--deduplicate", REF) and clone[3].startswith("ale-frame-")
    assert local.framed == [(clone[3], "admin", framed.identity)]
    (vm,) = controller.created
    assert vm["image"] == clone[3]
    assert local.saved == [(vm["name"], framed.name)]
    # The display mode the first login saved is forgotten, so each VM starts at its own.
    assert controller.execs[-1] == ["sudo", "-n", *orchard_framing.FORGET_DISPLAYS]
    # What the first login delivered to Notification Center is dismissed, as the account.
    assert controller.execs[-2] == [
        "sudo",
        "-n",
        "-u",
        "admin",
        "/bin/sh",
        "-c",
        orchard_framing._DISMISS_NOTICES,
        "sh",
        "/Users/admin",
    ]
    assert controller.deleted == [vm["name"]]
    assert delete == ("delete", clone[3])
    assert (framed.agent_home, framed.agent_uid) == ("/Users/admin", 501)

    again = await orchard_framing.frame_image(local, IMAGE, user="admin")
    assert again == framed and len(controller.created) == 1 and len(local.tarts) == 2


async def test_each_resolution_boots_its_own_clone_of_the_framed_image():
    local = Local(Controller())
    framed = await orchard_framing.frame_image(local, IMAGE, user="admin")
    local.tarts.clear()

    name = await orchard_framing.at_resolution(local, framed, DesktopResolution())
    assert name == f"{framed.name}-1024x768"
    clone, display, rename = local.tarts
    assert clone[:2] == ("clone", framed.name) and clone[2].startswith("ale-frame-")
    assert display == ("set", clone[2], "--display", "1024x768pt")
    assert rename == ("rename", clone[2], name)

    local.images.add(name)
    assert await orchard_framing.at_resolution(local, framed, DesktopResolution()) == name
    assert len(local.tarts) == 3
    other = await orchard_framing.at_resolution(local, framed, DesktopResolution(width=1366))
    assert other == f"{framed.name}-1366x768"


async def test_framing_again_deletes_the_clones_of_the_earlier_framing():
    local = Local(Controller())
    framed = await orchard_framing.frame_image(local, IMAGE, user="admin")
    local.images = {f"{framed.name}-1024x768", f"{framed.name}-1366x768", "ale-framed-other-1"}
    local.tarts.clear()
    again = await orchard_framing.frame_image(local, IMAGE, user="admin")
    assert again.name == framed.name
    assert local.tarts[:2] == [
        ("delete", f"{framed.name}-1024x768"),
        ("delete", f"{framed.name}-1366x768"),
    ]


async def test_a_clone_whose_display_cannot_be_set_is_deleted():
    class Refusing(Local):
        async def tart(self, *args, timeout_sec=600):
            self.tarts.append(args)
            if args[0] == "set":
                raise ProviderStartError("tart set failed")
            return ""

    local = Refusing(Controller())
    framed = orchard_framing.FramedImage("ale-framed-x", "sha256:" + "0" * 64, "/Users/a", 501)
    with pytest.raises(ProviderStartError, match="tart set failed"):
        await orchard_framing.at_resolution(local, framed, DesktopResolution())
    assert local.tarts[-1] == ("delete", local.tarts[0][2])


async def test_a_local_image_is_cloned_without_a_pull():
    built = PreparedTaskImage(
        kind=ImageKind.VM,
        source="solver-local",
        input_identity=DIGEST,
        image_source_identity=DIGEST,
        runtime_ref="ale-built-" + "b" * 32,
        prepared_identity=DIGEST,
        base_materials=(DIGEST,),
        builder_identity=DIGEST,
    )
    local = Local(Controller())
    await orchard_framing.frame_image(local, built, user="admin")
    clone = local.tarts[0]
    assert clone[:2] == ("clone", built.runtime_ref) and clone[2].startswith("ale-frame-")


async def test_a_failed_pull_keeps_nothing():
    class Offline(Local):
        async def tart(self, *args, timeout_sec=600):
            self.tarts.append(args)
            raise ProviderStartError("tart clone failed: registry unreachable")

    local = Offline(Controller())
    with pytest.raises(ProviderStartError, match="registry unreachable"):
        await orchard_framing.frame_image(local, IMAGE, user="admin")
    (clone,) = local.tarts
    assert clone[:3] == ("clone", "--deduplicate", REF)
    assert not local.framed and not local.controller.created and not local.saved


async def test_each_account_has_its_framed_image():
    local = Local(Controller())
    admin = await orchard_framing.frame_image(local, IMAGE, user="admin")
    local.controller.consoles = ["tester"]
    other = await orchard_framing.frame_image(local, IMAGE, user="tester")
    assert admin.name != other.name


async def test_the_first_login_waits_for_the_account_and_its_background_items():
    controller = Controller(consoles=("root", "admin"), dumps=(PENDING, SETTLED))
    await orchard_framing.frame_image(Local(controller), IMAGE, user="admin")
    stats = [argv for argv in controller.execs if argv[0] == "/usr/bin/stat"]
    dumps = [argv for argv in controller.execs if argv[-1] == "dumpbtm"]
    assert len(stats) == 2 and len(dumps) == 2


async def test_a_first_login_that_never_settles_keeps_nothing(monkeypatch):
    monkeypatch.setattr(orchard_framing, "_SETTLE_TIMEOUT_SEC", 0.05)
    controller = Controller(dumps=(PENDING,))
    local = Local(controller)
    with pytest.raises(ProviderStartError, match="did not finish signing admin in"):
        await orchard_framing.frame_image(local, IMAGE, user="admin")
    assert controller.deleted and not local.saved and local.tarts[-1][0] == "delete"


async def test_a_failed_framer_deletes_the_clone_and_keeps_nothing():
    local = Local(Controller(), frame_error=ProviderStartError("ALE: no Data volume"))
    with pytest.raises(ProviderStartError, match="no Data volume"):
        await orchard_framing.frame_image(local, IMAGE, user="admin")
    assert not local.controller.created and not local.saved
    assert local.tarts[-1] == ("delete", local.tarts[0][-1])
    local.frame_error = None
    await orchard_framing.frame_image(local, IMAGE, user="admin")
    assert local.saved


@pytest.mark.parametrize(
    ("dump", "labels", "pending"),
    [
        (PENDING, {LABEL}, True),
        (SETTLED, {LABEL}, False),
        (SETTLED, {LABEL, "org.example.other"}, True),
        (SETTLED.replace("UID 501", "UID 502"), {LABEL}, True),
        ("", set(), False),
    ],
)
def test_pending_notices_reads_the_accounts_records(dump, labels, pending):
    assert orchard_framing.pending_notices(dump, 501, labels) is pending


async def test_a_stopped_vm_is_saved_by_cloning_its_tart_vm(tmp_path):
    local = LocalOrchard("https://localhost:1", "secret", tmp_path, tmp_path / "tart", tmp_path)
    calls = []

    async def tart(*args, timeout_sec=600):
        calls.append(args)
        if args[0] == "list":
            names = ["orchard-ale-1-uid-1-0", "orchard-ale-10-uid-9-0", "ale-framed-old"]
            return json.dumps([{"Name": name} for name in names])
        return ""

    local.__dict__["tart"] = tart
    await local.save(OwnedVM("ale-1", "uid-1"), "ale-built-new")
    assert calls[-1] == ("clone", "--overwrite", "orchard-ale-1-uid-1-0", "ale-built-new")


class Process:
    def __init__(self, returncode, stdout, stderr=b""):
        self.returncode, self.output = returncode, (stdout, stderr)
        self.input = None

    async def communicate(self, data):
        self.input = data
        return self.output


@pytest.fixture
def framer(tmp_path, monkeypatch):
    installed = tmp_path / "framework-macos"
    installed.write_bytes(MACOS_FRAMER.read_bytes())
    monkeypatch.setattr(orchard_client, "FRAMER", installed)
    runs = []

    def answer(returncode, stdout, stderr=b""):
        async def run(*argv, **kwargs):
            runs.append(Process(returncode, stdout, stderr))
            runs[-1].argv = argv
            return runs[-1]

        monkeypatch.setattr(orchard_client.asyncio, "create_subprocess_exec", run)

    local = LocalOrchard("https://localhost:1", "secret", tmp_path, tmp_path / "tart", tmp_path)
    return local, installed, runs, answer


async def test_the_framer_runs_through_sudo_with_the_payload(framer):
    local, installed, runs, answer = framer
    answer(0, b'{"agent_home": "/Users/admin", "agent_uid": 501}\n')
    facts = await local.frame_disk("ale-frame-1", user="admin", identity=DIGEST, payload=b"tar")
    assert facts == {"agent_home": "/Users/admin", "agent_uid": 501}
    disk = str(local.tart_home / "vms" / "ale-frame-1" / "disk.img")
    assert runs[0].argv == ("/usr/bin/sudo", "-n", str(installed), disk, "admin", "admin", DIGEST)
    assert runs[0].input == b"tar"


async def test_the_framer_reason_is_reported(framer):
    local, _, _, answer = framer
    answer(1, b"", b"ALE: the image does not sign 'admin' in automatically\n")
    with pytest.raises(ProviderStartError, match="does not sign 'admin' in automatically"):
        await local.frame_disk("ale-frame-1", user="admin", identity=DIGEST, payload=b"")


async def test_a_stale_installed_framer_is_refused(framer):
    local, installed, runs, answer = framer
    answer(0, b"{}")
    installed.write_bytes(b"#!/bin/sh\nexit 0\n")
    with pytest.raises(ProviderStartError, match=r"setup_macos\.py"):
        await local.frame_disk("ale-frame-1", user="admin", identity=DIGEST, payload=b"")
    assert not runs


def test_the_framer_is_valid_sh():
    assert subprocess.run(["sh", "-n", str(MACOS_FRAMER)]).returncode == 0


def test_driver_archive_rejects_wrong_checksum(monkeypatch, tmp_path):
    from ale.run import framework

    monkeypatch.setattr(framework, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(
        framework.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"tampered")
    )
    with pytest.raises(ProviderStartError, match="checksum"):
        framework.macos_driver_archive()
    assert not list(tmp_path.rglob("*.tar.gz"))


class Shutdown:
    def __init__(self, result=None):
        self.result, self.argv = result, None

    async def exec(self, name, argv, *, timeout_sec=120):
        self.argv = argv
        if self.result is None:
            raise orchard_client.GuestUnreachableError("the session ended")
        return self.result


def stopping_local(tmp_path, states):
    local = LocalOrchard("https://localhost:1", "secret", tmp_path, tmp_path / "tart", tmp_path)

    async def tart(*args, timeout_sec=600):
        state = states.pop(0) if len(states) > 1 else states[0]
        return json.dumps([{"Name": "orchard-ale-1-uid-1-0", "State": state}])

    local.__dict__["tart"] = tart
    return local


async def test_a_kept_disk_is_shut_down_by_its_guest(tmp_path, monkeypatch):
    monkeypatch.setattr(orchard_client.asyncio, "sleep", _no_wait)
    local = stopping_local(tmp_path, ["running", "running", "stopped"])
    client, owned = Shutdown(), OwnedVM("ale-1", "uid-1")
    await local.shut_down(client, owned)
    assert client.argv == ["sudo", "-n", "/sbin/shutdown", "-h", "now"]
    assert owned.stopped


async def test_a_refused_shutdown_is_reported(tmp_path):
    local = stopping_local(tmp_path, ["running"])
    client = Shutdown(OrchardExecResult(1, b"", b"sudo: a password is required"))
    with pytest.raises(ProviderStartError, match="password is required"):
        await local.shut_down(client, OwnedVM("ale-1", "uid-1"))


async def test_a_guest_that_never_stops_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(orchard_client.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(orchard_client, "SHUTDOWN_TIMEOUT_SEC", 0)
    local = stopping_local(tmp_path, ["running"])
    with pytest.raises(ProviderStartError, match="did not shut down"):
        await local.shut_down(Shutdown(), OwnedVM("ale-1", "uid-1"))
