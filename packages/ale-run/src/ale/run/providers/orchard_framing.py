"""Install the framework into a macOS image once, and keep the result as a local image.

A Tart disk is APFS, which only macOS reads, so a macOS image is framed on this Mac the
way the QEMU Provider frames a qcow2: a clone of the image is changed offline by
``framework_macos.sh``, which runs as root without starting any of the image's programs,
and records the privacy grants Cua Driver needs as System Settings would. The clone is
then booted once, without the agent or the Driver, so macOS finishes the image's first
login; the background-item notice it posts then, which Notification Center shows again at
every login until someone dismisses it, is dismissed before the disk is kept. The stopped
VM is kept as a local Tart image, and every later VM clones it, which on APFS copies no
data.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ale.core.errors import ProviderStartError
from ale.core.ids import content_hash
from ale.core.sandbox import PreparedTaskImage
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

__all__ = ["FramedImage", "frame_image"]

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
            "first_login": [_DISMISS_NOTICES, _AGENT_LABELS],
        }
    )


def _records() -> Path:
    root = cache_root() / "macos-frames"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def frame_image(local: LocalOrchard, image: PreparedTaskImage, *, user: str) -> FramedImage:
    """The framed copy of ``image`` for this account, made on first use and then reused.

    Solver and verifier VMs of one image and account share it.
    """
    identity = _identity(image, user=user)
    key = identity.removeprefix("sha256:")[:32]
    root = _records()
    record = root / f"{key}.json"
    with (root / f"{key}.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.5)
        try:
            if record.is_file():
                framed = FramedImage(**json.loads(record.read_text()))
                if framed.name in await local.local_images():
                    return framed
            framed = await _frame(local, image, f"ale-framed-{key}", identity, user=user)
            record.write_text(json.dumps(asdict(framed)))
            return framed
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
    # A clone of a pinned reference pulls it by digest, so it is the image that was prepared.
    work = f"ale-frame-{uuid.uuid4().hex}"
    await local.tart("clone", image.runtime_ref, work, timeout_sec=PULL_TIMEOUT_SEC)
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
