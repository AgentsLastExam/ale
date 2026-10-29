"""What the engine adds to every Task image.

A Task image is whatever its author built, from any base. What the engine itself needs in
a sandbox is added here, when the image is prepared. A Linux image gets one more image
layer:

- guestd, the guest service every sandbox operation goes through, under root-only
  ``/opt/ale`` so the agent cannot read the protocol driving its sandbox;
- Cua Driver at a pinned version, which guestd starts inside the desktop session when the
  image has one;
- for a VM, the systemd unit that starts guestd at boot;
- a check that the Task's agent account exists in the image;
- working sudo for that account when the Task requests it, installing sudo if needed.

The result is an ordinary image built on the Task image, so Docker runs it and VM
preparation materializes it. A label records what it was built with, which makes adding
the same layer twice a no-op.

A VM disk named by reference or handed straight to the QEMU Provider gets the same pieces
installed into a copy of it offline, inside the VM builder's own small appliance VM, so the
disk's programs never run on the Host. Both record what they installed in a marker file; a
disk that already carries this framework is used as it is.

On Windows the pieces are guestd, Cua Driver and a boot service that starts both in the
agent's session, which replaces the logon tasks only a running Windows could register
(``framework_windows.py`` installs them). A Windows Task image is built from a framed copy
of its base, so the framework is already there when the Task's own image script runs.
Elevation is not part of the image; the Host grants it per boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath

from ale.core.errors import ProviderStartError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.core.sandbox import PreparedTaskImage
from ale.core.taskspec import OperatingSystem
from ale.run import ale_image, guestd
from ale.run.sources import cache_root

__all__ = [
    "CUA_DRIVER_VERSION",
    "LABEL",
    "MARKER",
    "MATERIALIZER_IMAGE",
    "WINDOWS_MARKER",
    "frame_disk",
    "framed_disk",
    "framework_identity",
    "framework_on_disk",
    "windows_framework_identity",
    "with_framework",
]

LABEL = "ale.framework"
#: Where a prepared Linux image records the framework identity it carries.
MARKER = "/opt/ale/framework"
#: The same for a Windows disk, as libguestfs names ``C:\ProgramData\ALE\framework``.
WINDOWS_MARKER = "/ProgramData/ALE/framework"
#: The VM builder: it materializes Linux VM OCI content into a qcow2, and its libguestfs
#: installs the framework into a Linux or Windows qcow2.
MATERIALIZER_IMAGE = os.environ.get("ALE_VM_MATERIALIZER", ale_image("ale-vm-materializer"))
CUA_DRIVER_VERSION = "0.38.0"
#: Release asset and its published SHA-256, per guest platform.
_CUA_DRIVER = {
    "linux": (
        f"cua-driver-rs-{CUA_DRIVER_VERSION}-linux-x86_64-binary.tar.gz",
        "31ab9709dd91ce73677e49b9f3e75c6f0d3e4d9625c286c9e58262bfbea698dd",
    ),
    "windows": (
        f"cua-driver-rs-{CUA_DRIVER_VERSION}-windows-x86_64-binary.zip",
        "5edaa74d0afa1b55a9668dbc4f32736507bd2bd4d75b18ca1dc8881bb12402b5",
    ),
}
_CUA_DRIVER_BINARY = {"linux": "cua-driver", "windows": "cua-driver.exe"}
#: Run inside the VM builder to install the Windows framework into a disk.
_WINDOWS_INSTALLER = Path(__file__).with_name("framework_windows.py")

#: How a prepared VM disk starts the guest service at boot.
_GUEST_UNIT = f"""\
[Unit]
Description=ALE guest service
After=network.target

[Service]
Environment=ALE_AGENT_USER={{user}}
ExecStart=/usr/bin/python3 /opt/ale/guestd/main.py --tcp 0.0.0.0:{guestd.PORT}
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
"""

#: Run as root while the layer is built. POSIX sh, so it works in any Linux image.
_SETUP = r"""set -eu
user="$1"
sudo_requested="$2"
identity="$3"
if ! id -u "$user" >/dev/null 2>&1; then
    echo "ALE: the agent account '$user' does not exist in this image; create it in the" \
        "image or name an existing account with image.user in task.yaml" >&2
    exit 1
fi
chown -R 0:0 /opt/ale
chmod -R go-rwx /opt/ale
chown 0:0 /usr/local/bin/cua-driver
chmod 0755 /usr/local/bin/cua-driver
if [ -f /etc/systemd/system/ale-guestd.service ]; then
    chown 0:0 /etc/systemd/system/ale-guestd.service
    chmod 0644 /etc/systemd/system/ale-guestd.service
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /etc/systemd/system/ale-guestd.service \
        /etc/systemd/system/multi-user.target.wants/ale-guestd.service
fi
if [ "$sudo_requested" = true ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            export DEBIAN_FRONTEND=noninteractive
            apt-get update && apt-get install -y --no-install-recommends sudo
            rm -rf /var/lib/apt/lists/*
        elif command -v dnf >/dev/null 2>&1; then dnf install -y sudo && dnf clean all
        elif command -v microdnf >/dev/null 2>&1; then microdnf install -y sudo
        elif command -v yum >/dev/null 2>&1; then yum install -y sudo && yum clean all
        elif command -v apk >/dev/null 2>&1; then apk add --no-cache sudo
        elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install sudo
        else
            echo "ALE: sudo was requested, but this image has neither sudo nor a package" \
                "manager to install it with" >&2
            exit 1
        fi
    fi
    mkdir -p /etc/sudoers.d
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > /etc/sudoers.d/ale-agent
    chmod 0440 /etc/sudoers.d/ale-agent
    if ! sudo -n -l -U "$user" 2>/dev/null | grep -q 'NOPASSWD: ALL'; then
        printf '@includedir /etc/sudoers.d\n' >> /etc/sudoers
    fi
    if ! sudo -n -l -U "$user" 2>/dev/null | grep -q 'NOPASSWD: ALL'; then
        echo "ALE: sudo for '$user' could not be configured in this image" >&2
        exit 1
    fi
fi
printf '%s\n' "$identity" > /opt/ale/framework
"""


def framework_identity(*, user: str, sudo: bool, vm: bool) -> str:
    """Everything the Linux layer is built from, so a change to any of it rebuilds it."""
    return content_hash(
        {
            "guestd": _guestd_digests(),
            "cua_driver": _CUA_DRIVER["linux"][1],
            "setup": _SETUP,
            "unit": _GUEST_UNIT if vm else None,
            "user": user,
            "sudo": sudo,
        }
    )


def windows_framework_identity(*, user: str) -> str:
    """Everything the Windows framework is built from, so a change to any of it reframes."""
    return content_hash(
        {
            "guestd": _guestd_digests(),
            "cua_driver": _CUA_DRIVER["windows"][1],
            "installer": hashlib.sha256(_WINDOWS_INSTALLER.read_bytes()).hexdigest(),
            "runner": guestd.HOST_ADDRESS,
            "user": user,
        }
    )


def _guestd_digests() -> dict[str, str]:
    return {
        name: hashlib.sha256((guestd.SOURCE / name).read_bytes()).hexdigest()
        for name in guestd.FILES
    }


async def with_framework(image: str, *, user: str, sudo: bool, vm: bool = False) -> str:
    """Return the local identity of ``image`` with the framework layer added.

    ``image`` is a local image ID or reference. Building is serialized per result, and an
    image that already carries this exact layer is returned unchanged.
    """
    identity = framework_identity(user=user, sudo=sudo, vm=vm)
    config = await _inspect(image)
    if (config.get("Labels") or {}).get(LABEL) == identity:
        return config["Id"]
    digest = hashlib.sha256(f"{config['Id']}\n{identity}".encode()).hexdigest()[:32]
    tag = f"ale-framework:{digest}"
    root = cache_root() / "framework"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{digest}.lock").open("a") as lock:
        await _acquire(lock)
        try:
            built = await _inspect(tag, missing_ok=True)
            if built and (built.get("Labels") or {}).get(LABEL) == identity:
                return built["Id"]
            await _build(config, tag, identity, user=user, sudo=sudo, vm=vm)
            return (await _inspect(tag))["Id"]
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def framed_disk(
    image: PreparedTaskImage, *, operating_system: OperatingSystem, user: str, sudo: bool
) -> tuple[PreparedTaskImage, bool]:
    """A VM disk with the framework installed, and whether it was already there.

    A copy of the disk is framed once per disk, framework and builder, and then used
    directly. A disk that already carries this exact framework, such as one ALE prepared,
    is used as it is; that is read from the disk once and then recorded. Windows does not
    take ``sudo``: the Host grants Administrator access per boot, not in the disk.
    """
    identity = (
        windows_framework_identity(user=user)
        if operating_system is OperatingSystem.WINDOWS
        else framework_identity(user=user, sudo=sudo, vm=True)
    )
    key = content_hash(
        {
            "disk": image.prepared_identity,
            "framework": identity,
            "builder": await _builder_identity(),
        }
    )
    root = cache_root() / "vm-builds"
    root.mkdir(parents=True, exist_ok=True)
    disk = root / f"{key.removeprefix('sha256:')}.qcow2"
    # Names the input disk when it already carried this framework, so it is checked once.
    carried = disk.with_suffix(".json")
    source = Path(image.runtime_ref)
    with disk.with_suffix(".lock").open("a") as lock:
        await _acquire(lock)
        try:
            if await _valid_qcow2(disk):
                return _with_disk(image, disk, key), True
            if _carried(carried) == str(source):
                return image, True
            found = await framework_on_disk(
                source, operating_system=operating_system, builder=MATERIALIZER_IMAGE
            )
            if found == identity:
                carried.write_text(json.dumps({"disk": str(source)}), encoding="utf-8")
                return image, True
            with tempfile.TemporaryDirectory(prefix=f".{disk.stem}-", dir=root) as temporary:
                output = Path(temporary) / "disk.qcow2"
                code, _, stderr = await _run(
                    "qemu-img", "convert", "-O", "qcow2", str(source), str(output), timeout=1800
                )
                if code != 0:
                    raise ProviderStartError(f"could not copy the VM disk: {stderr.strip()}")
                await frame_disk(
                    output,
                    operating_system=operating_system,
                    user=user,
                    sudo=sudo,
                    builder=MATERIALIZER_IMAGE,
                )
                if not await _valid_qcow2(output):
                    raise ProviderStartError("framing produced no valid qcow2")
                os.replace(output, disk)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return _with_disk(image, disk, key), False


def _carried(record: Path) -> str | None:
    try:
        return str(json.loads(record.read_text(encoding="utf-8"))["disk"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _with_disk(image: PreparedTaskImage, disk: Path, identity: str) -> PreparedTaskImage:
    return image.model_copy(
        update={"runtime_ref": str(disk.resolve()), "prepared_identity": identity}
    )


async def framework_on_disk(
    disk: Path, *, operating_system: OperatingSystem, builder: str
) -> str | None:
    """The framework identity a VM disk records, or None when it carries none.

    Judged by what is printed: virt-cat can print the file and still exit non-zero over
    an unrelated mount it could not follow.
    """
    _, output, _ = await _in_container(
        "--device=/dev/kvm", "--network=none",
        "--mount", f"type=bind,src={disk.parent.resolve()},dst=/disk,readonly",
        "--entrypoint", "virt-cat", builder,
        "--format", "qcow2", "--add", f"/disk/{disk.name}",
        WINDOWS_MARKER if operating_system is OperatingSystem.WINDOWS else MARKER,
        timeout=300,
    )  # fmt: skip
    found = output.strip()
    return found if re.fullmatch(r"sha256:[0-9a-f]{64}", found) else None


async def frame_disk(
    disk: Path, *, operating_system: OperatingSystem, user: str, sudo: bool, builder: str
) -> None:
    """Install the framework into a standalone VM disk, in place, without booting it.

    ``builder`` is the VM builder image. Its libguestfs edits the disk from a small
    appliance VM of its own, never on the Host.
    """
    if operating_system is OperatingSystem.WINDOWS:
        await _frame_windows_disk(disk, user=user, builder=builder)
    else:
        await _frame_linux_disk(disk, user=user, sudo=sudo, builder=builder)


async def _frame_windows_disk(disk: Path, *, user: str, builder: str) -> None:
    """Check the disk and install the framework, by ``framework_windows.py`` in the builder."""
    identity = windows_framework_identity(user=user)
    driver = await asyncio.to_thread(_cua_driver, "windows")
    with tempfile.TemporaryDirectory(prefix="ale-framework-") as temporary:
        context = Path(temporary)
        shutil.copytree(guestd.SOURCE, context / "guestd", ignore=_only(guestd.FILES))
        shutil.copytree(driver, context / "cua-driver")
        shutil.copy2(_WINDOWS_INSTALLER, context / "install.py")
        code, output, stderr = await _in_container(
            "--device=/dev/kvm", "--network=none",
            "--mount", f"type=bind,src={disk.parent.resolve()},dst=/disk",
            "--mount", f"type=bind,src={context},dst=/framework,readonly",
            "--entrypoint", "python3", builder,
            "/framework/install.py", f"/disk/{disk.name}", "/framework",
            "--user", user, "--identity", identity, "--runner-address", guestd.HOST_ADDRESS,
            timeout=1800,
        )  # fmt: skip
    if code != 0:
        raise TaskDefinitionError(
            f"could not add the ALE framework to the Windows disk: {_explained(stderr, output)}"
        )


async def _frame_linux_disk(disk: Path, *, user: str, sudo: bool, builder: str) -> None:
    """Run the setup script inside the disk's own appliance VM; sudo installs use its network."""
    identity = framework_identity(user=user, sudo=sudo, vm=True)
    driver = await asyncio.to_thread(_cua_driver, "linux")
    with tempfile.TemporaryDirectory(prefix="ale-framework-") as temporary:
        context = Path(temporary)
        _stage(context, driver, user=user, vm=True)
        setup = shlex.join(["/bin/sh", "/opt/ale/setup.sh", user, _flag(sudo), identity])
        code, output, stderr = await _in_container(
            "--device=/dev/kvm",
            "--mount", f"type=bind,src={disk.parent.resolve()},dst=/disk",
            "--mount", f"type=bind,src={context},dst=/framework,readonly",
            "--entrypoint", "virt-customize", builder,
            "--format", "qcow2", "--add", f"/disk/{disk.name}", "--no-logfile",
            "--delete", "/opt/ale/guestd",
            "--mkdir", "/opt/ale",
            "--copy-in", "/framework/guestd:/opt/ale",
            "--upload", "/framework/setup.sh:/opt/ale/setup.sh",
            "--upload", "/framework/cua-driver:/usr/local/bin/cua-driver",
            "--upload", "/framework/ale-guestd.service:/etc/systemd/system/ale-guestd.service",
            "--run-command", setup,
            timeout=1800,
        )  # fmt: skip
    if code != 0:
        raise TaskDefinitionError(
            f"could not add the ALE framework to the VM disk: {_explained(stderr, output)}"
        )


def _explained(stderr: str, stdout: str) -> str:
    """What a framing script said was wrong, else the tail of what it printed."""
    said = [
        line.split("ALE: ", 1)[1] for line in f"{stderr}\n{stdout}".splitlines() if "ALE: " in line
    ]
    return " ".join(said) if said else stderr.strip()[-2000:]


async def _build(base: dict, tag: str, identity: str, *, user: str, sudo: bool, vm: bool) -> None:
    driver = await asyncio.to_thread(_cua_driver, "linux")
    base_tag = f"ale-framework-base:{base['Id'].removeprefix('sha256:')}"
    code, _, stderr = await _run("docker", "tag", base["Id"], base_tag)
    if code != 0:
        raise ProviderStartError(f"could not reference the Task image: {stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="ale-framework-") as temporary:
        context = Path(temporary)
        _stage(context, driver, user=user, vm=vm)
        lines = [
            f"FROM {base_tag}",
            "USER root",
            "COPY guestd/ /opt/ale/guestd/",
            "COPY --chmod=0755 cua-driver /usr/local/bin/cua-driver",
            "COPY setup.sh /opt/ale/setup.sh",
        ]
        if vm:
            lines.append("COPY ale-guestd.service /etc/systemd/system/ale-guestd.service")
        lines.append(_json_run("/bin/sh", "/opt/ale/setup.sh", user, _flag(sudo), identity))
        if base.get("User"):
            lines.append(f"USER {base['User']}")
        lines.append(f"LABEL {LABEL}={json.dumps(identity)}")
        (context / "Dockerfile").write_text("\n".join(lines) + "\n")
        code, _, stderr = await _run(
            "docker", "buildx", "build", "--load", "--provenance=false", "--sbom=false",
            "--tag", tag, str(context), timeout=1800,
        )  # fmt: skip
    if code != 0:
        # The setup script explains what the image lacks; buildx's own summary does not.
        explained = [line.split("ALE: ", 1)[1] for line in stderr.splitlines() if "ALE: " in line]
        reason = " ".join(explained) if explained else stderr.strip()[-2000:]
        raise TaskDefinitionError(
            f"could not add the ALE framework layer to the Task image: {reason}"
        )


def _stage(context: Path, driver: Path, *, user: str, vm: bool) -> None:
    """Lay out what the Linux framework installs, for either installation path."""
    shutil.copytree(guestd.SOURCE, context / "guestd", ignore=_only(guestd.FILES))
    shutil.copy2(driver / "cua-driver", context / "cua-driver")
    (context / "setup.sh").write_text(_SETUP)
    if vm:
        (context / "ale-guestd.service").write_text(_GUEST_UNIT.format(user=user))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _json_run(*argv: str) -> str:
    return "RUN " + json.dumps(list(argv))


def _only(names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    return lambda directory, entries: [entry for entry in entries if entry not in names]


def _cua_driver(platform: str) -> Path:
    """The pinned Cua Driver release files, downloaded once and verified against the release."""
    asset, sha256 = _CUA_DRIVER[platform]
    root = cache_root() / "cua-driver" / CUA_DRIVER_VERSION
    files = root / platform
    if files.is_dir():
        return files
    root.mkdir(parents=True, exist_ok=True)
    with (root / "download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if files.is_dir():
            return files
        url = (
            "https://github.com/trycua/cua/releases/download/"
            f"cua-driver-rs-v{CUA_DRIVER_VERSION}/{asset}"
        )
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                payload = response.read()
        except OSError as exc:
            raise ProviderStartError(f"could not download Cua Driver: {exc}") from exc
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise ProviderStartError("downloaded Cua Driver does not match its pinned checksum")
        partial = Path(tempfile.mkdtemp(prefix=f".{platform}-", dir=root))
        for name, data in _archive_files(asset, payload):
            (partial / name).write_bytes(data)
        binary = partial / _CUA_DRIVER_BINARY[platform]
        if not binary.is_file():
            shutil.rmtree(partial)
            raise ProviderStartError(f"Cua Driver archive has no {binary.name}")
        binary.chmod(0o755)
        partial.rename(files)
    return files


def _archive_files(asset: str, payload: bytes) -> Iterator[tuple[str, bytes]]:
    """The regular files of a release archive, by base name only."""
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            for info in bundle.infolist():
                if not info.is_dir():
                    yield PureWindowsPath(info.filename).name, bundle.read(info)
        return
    with tarfile.open(fileobj=io.BytesIO(payload)) as bundle:
        for member in bundle.getmembers():
            source = bundle.extractfile(member) if member.isfile() else None
            if source is not None:
                yield PureWindowsPath(member.name).name, source.read()


async def _inspect(reference: str, *, missing_ok: bool = False) -> dict:
    code, output, stderr = await _run(
        "docker", "image", "inspect", "--format",
        '{{json .Config}}{{"\\n"}}{{.Id}}', reference, timeout=60,
    )  # fmt: skip
    if code != 0:
        if missing_ok:
            return {}
        raise ProviderStartError(f"could not inspect image {reference}: {stderr.strip()}")
    config, _, image_id = output.strip().rpartition("\n")
    loaded = json.loads(config or "null") or {}
    loaded["Id"] = image_id.strip()
    return loaded


async def _builder_identity() -> str:
    """The VM builder's local image ID, pulling it first when this host lacks it."""
    if found := await _inspect(MATERIALIZER_IMAGE, missing_ok=True):
        return found["Id"]
    code, _, stderr = await _run("docker", "pull", MATERIALIZER_IMAGE, timeout=900)
    if code != 0:
        raise ProviderStartError(f"could not acquire image {MATERIALIZER_IMAGE}: {stderr.strip()}")
    return (await _inspect(MATERIALIZER_IMAGE))["Id"]


async def _valid_qcow2(path: Path) -> bool:
    if not path.is_file():
        return False
    code, _, _ = await _run("qemu-img", "check", "-q", str(path), timeout=300)
    return code == 0


async def _acquire(handle) -> None:  # type: ignore[no-untyped-def]
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            await asyncio.sleep(0.1)


async def _in_container(*argv: str, timeout: float) -> tuple[int, str, str]:
    """``docker run --rm`` that ends with this call: killing the client alone would leave
    the container running, still writing into files this process has given up."""
    name = f"ale-framework-{uuid.uuid4().hex[:12]}"
    try:
        return await _run("docker", "run", "--rm", "--name", name, *argv, timeout=timeout)
    except BaseException:
        with contextlib.suppress(Exception):
            await asyncio.shield(_run("docker", "rm", "--force", name, timeout=60))
        raise


async def _run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            raise ProviderStartError(f"{' '.join(argv[:3])} timed out after {timeout:g}s") from None
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
