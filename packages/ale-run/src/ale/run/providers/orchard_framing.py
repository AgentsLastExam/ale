"""Install the framework into a macOS image once, and keep the result as a local image.

A Tart disk is APFS, which only macOS reads, so a macOS image is framed on this Mac the
way the QEMU Provider frames a qcow2: a clone of the image is changed offline by
``framework_macos.sh``, which runs as root without starting any of the image's programs,
and records the privacy grants Cua Driver needs as System Settings would. The clone is
then booted once, without the agent or the Driver, so macOS finishes the image's first
login; the background-item notice it posts then, which Notification Center shows again at
every login until someone dismisses it, is dismissed before the disk is kept. The stopped
VM is kept as a local Tart image, and every later VM boots a clone of it at its desktop
resolution, which on APFS copies no data.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path

from ale.core.errors import ProviderStartError
from ale.core.ids import content_hash
from ale.core.sandbox import DesktopResolution, PreparedTaskImage
from ale.run.framework import macos_framework_identity, macos_framework_payload
from ale.run.providers.orchard_client import (
    PULL_TIMEOUT_SEC,
    LocalOrchard,
    OrchardClient,
    OwnedVM,
    await_running,
    create_owned,
    delete_owned,
    vm_spec,
)
from ale.run.sources import cache_root

__all__ = [
    "FORGET_DISPLAYS",
    "FramedImage",
    "at_resolution",
    "frame_image",
    "framed_record",
    "remove_locks",
]

#: The first login of a framed disk, from boot until its notices have been posted.
_SETTLE_TIMEOUT_SEC = 600
#: macOS posts a first login's background-item notice some seconds after it registers the
#: account's items, and reports neither that nor having none to post; nine was observed.
_NOTICE_GRACE_SEC = 30
#: Run as the account: dismiss what its first login delivered to Notification Center.
#: usernoted writes the notices it holds back at shutdown, so it is stopped without saving.
_DISMISS_NOTICES = (
    'db="$1/Library/Group Containers/group.com.apple.usernoted/db2/db"; '
    '[ ! -f "$db" ] || /usr/bin/sqlite3 "$db" '
    '"DELETE FROM record; DELETE FROM delivered; DELETE FROM displayed;"; '
    "/usr/bin/pkill -9 -x usernoted || true"
)
#: Run as root before a disk is kept: forget the display mode its desktop saved. macOS
#: restores it on a display of the same identity whatever size Tart gives that display, so a
#: VM would otherwise keep that size rather than start at its own.
FORGET_DISPLAYS = ["/bin/rm", "-f", "/Library/Preferences/com.apple.windowserver.displays.plist"]
#: The labels of the LaunchAgents every account's session loads.
_AGENT_LABELS = (
    'for plist in /Library/LaunchAgents/*.plist; do [ -f "$plist" ] || continue; '
    "/usr/libexec/PlistBuddy -c 'Print :Label' \"$plist\" 2>/dev/null || true; done"
)


@dataclass(frozen=True)
class FramedImage:
    """A local Tart image that carries this framework, and the account framing found."""

    name: str
    identity: str
    agent_home: str
    agent_uid: int


def _identity(image: PreparedTaskImage, *, user: str) -> str:
    return content_hash(
        {
            "image": image.prepared_identity,
            "framework": macos_framework_identity(user=user),
            "first_login": [_DISMISS_NOTICES, _AGENT_LABELS, FORGET_DISPLAYS],
        }
    )


def _records() -> Path:
    root = cache_root() / "macos-frames"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _framing(image: PreparedTaskImage, *, user: str) -> tuple[str, str, Path]:
    """The identity of ``image`` framed for this account, the key its local image and lock
    are named by, and where framing records it."""
    identity = _identity(image, user=user)
    key = identity.removeprefix("sha256:")[:32]
    return identity, key, _records() / f"{key}.json"


def framed_record(image: PreparedTaskImage, *, user: str) -> FramedImage | None:
    """The framed copy of ``image`` for this account as framing recorded it, if it did."""
    _, _, record = _framing(image, user=user)
    try:
        return FramedImage(**json.loads(record.read_text()))
    except (OSError, ValueError, TypeError):
        return None


async def frame_image(local: LocalOrchard, image: PreparedTaskImage, *, user: str) -> FramedImage:
    """The framed copy of ``image`` for this account, made on first use and then reused.

    Solver and verifier VMs of one image and account share it.
    """
    identity, key, record = _framing(image, user=user)
    async with _exclusive(f"{key}.lock"):
        images = await local.local_images()
        if record.is_file():
            framed = FramedImage(**json.loads(record.read_text()))
            if framed.name in images:
                return framed
        name = f"ale-framed-{key}"
        # Clones of an earlier framing of this image would boot instead of the new one.
        for stale in sorted(image for image in images if image.startswith(f"{name}-")):
            await local.tart("delete", stale)
        framed = await _frame(local, image, name, identity, user=user)
        record.write_text(json.dumps(asdict(framed)))
        return framed


async def at_resolution(
    local: LocalOrchard, framed: FramedImage, resolution: DesktopResolution
) -> str:
    """The local image that boots ``framed`` with its desktop at ``resolution``.

    Tart keeps a VM's display size in the VM's configuration, which a clone copies and
    Orchard cannot set, so each resolution boots its own clone of the framed image, made on
    first use. Tart takes the size in points, as the Run gives it.
    """
    name = f"{framed.name}-{resolution}"
    async with _exclusive(f"{name}.lock"):
        if name in await local.local_images():
            return name
        work = f"ale-frame-{uuid.uuid4().hex}"
        try:
            await local.tart("clone", framed.name, work)
            await local.tart("set", work, "--display", f"{resolution}pt")
            await local.tart("rename", work, name)
        except BaseException:
            with contextlib.suppress(ProviderStartError):
                await asyncio.shield(local.tart("delete", work))
            raise
        return name


def remove_locks(image: str) -> None:
    """Remove this Host's locks for the local image ``image`` and its clones at each
    resolution, once they are deleted."""
    for lock in [_records() / f"{image}.lock", *_records().glob(f"{image}-*.lock")]:
        lock.unlink(missing_ok=True)


@contextlib.asynccontextmanager
async def _exclusive(lock_name: str) -> AsyncIterator[None]:
    """Hold this Host's lock ``lock_name``, so one process makes each local image."""
    with (_records() / lock_name).open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def _frame(
    local: LocalOrchard,
    image: PreparedTaskImage,
    name: str,
    identity: str,
    *,
    user: str,
) -> FramedImage:
    # A clone of a pinned reference pulls it by digest when Tart's cache lacks it, so it is the
    # image that was prepared. Deduplicating, that pull starts the disk as an APFS clone of a
    # cached image sharing at least 1 GB with it, such as the base's earlier version, so the
    # chunks they share take no new space. A local image is only cloned.
    work = f"ale-frame-{uuid.uuid4().hex}"
    pull = ["--deduplicate"] if image.source == "external-ref" else []
    await local.tart("clone", *pull, image.runtime_ref, work, timeout_sec=PULL_TIMEOUT_SEC)
    try:
        payload = await asyncio.to_thread(macos_framework_payload)
        facts = await local.frame_disk(work, user=user, identity=identity, payload=payload)
        framed = FramedImage(
            name=name,
            identity=identity,
            agent_home=str(facts["agent_home"]),
            agent_uid=int(facts["agent_uid"]),  # type: ignore[call-overload]
        )
        await _first_login(local, work, framed, user=user)
        return framed
    finally:
        with contextlib.suppress(ProviderStartError):
            await local.tart("delete", work)


async def _first_login(local: LocalOrchard, work: str, framed: FramedImage, *, user: str) -> None:
    """Boot the framed disk once until macOS has settled the account's login, then keep it."""
    client = local.client()
    await client.start()
    owned: OwnedVM | None = None
    try:
        spec = vm_spec(f"ale-settle-{uuid.uuid4().hex}", work, cpus=2, memory_mb=4096)
        owned = await create_owned(client, spec)
        await await_running(client, owned)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SETTLE_TIMEOUT_SEC
        try:
            async with asyncio.timeout_at(deadline):
                await _settled(client, owned.name, user, framed, deadline)
        except TimeoutError:
            raise ProviderStartError(
                f"the framed macOS image did not finish signing {user} in within "
                f"{_SETTLE_TIMEOUT_SEC}s"
            ) from None
        await local.shut_down(client, owned)
        await local.save(owned, framed.name)
    finally:
        try:
            if owned is not None:
                await asyncio.shield(delete_owned(client, owned))
        finally:
            await client.close()


async def _settled(
    client: OrchardClient, name: str, user: str, framed: FramedImage, deadline: float
) -> None:
    while True:
        console = await client.exec(
            name, ["/usr/bin/stat", "-f", "%Su", "/dev/console"], deadline=deadline
        )
        if console.exit_code == 0 and console.stdout.decode().strip() == user:
            break
        await asyncio.sleep(2)
    listed = await client.exec(name, ["/bin/sh", "-c", _AGENT_LABELS], deadline=deadline)
    labels = {line for line in listed.stdout.decode().splitlines() if line}
    while True:
        dump = await client.exec(
            name, ["sudo", "-n", "/usr/bin/sfltool", "dumpbtm"], deadline=deadline
        )
        if dump.exit_code == 0 and not pending_notices(
            dump.stdout.decode(), framed.agent_uid, labels
        ):
            break
        await asyncio.sleep(2)
    await asyncio.sleep(_NOTICE_GRACE_SEC)
    dismissed = await client.exec(
        name,
        ["sudo", "-n", "-u", user, "/bin/sh", "-c", _DISMISS_NOTICES, "sh", framed.agent_home],
        deadline=deadline,
    )
    if dismissed.exit_code:
        detail = dismissed.stderr.decode(errors="replace").strip()
        raise ProviderStartError(f"could not dismiss the first login's notices: {detail}")
    forgotten = await client.exec(name, ["sudo", "-n", *FORGET_DISPLAYS], deadline=deadline)
    if forgotten.exit_code:
        detail = forgotten.stderr.decode(errors="replace").strip()
        raise ProviderStartError(f"could not forget the first login's display mode: {detail}")


def pending_notices(dump: str, uid: int, labels: set[str]) -> bool:
    """Whether macOS has yet to register and announce the account's background items.

    ``sfltool dumpbtm`` lists each account's items; a LaunchAgent appears as ``8.<label>``
    once that account's login registered it, and stays ``not notified`` until its notice.
    """
    found: set[str] = set()
    pending = False
    section: int | None = None
    disposition = ""
    for line in dump.splitlines():
        if match := re.match(r"\s*Records for UID (-?\d+)", line):
            section = int(match[1])
        elif section == uid and (match := re.match(r"\s*Disposition: \[([^]]*)\]", line)):
            disposition = match[1]
        elif section == uid and (match := re.match(r"\s*Identifier: (\S+)", line)):
            states = {part.strip() for part in disposition.split(",")}
            pending |= "enabled" in states and "not notified" in states
            found.add(match[1])
            disposition = ""
    return pending or any(f"8.{label}" not in found for label in labels)
