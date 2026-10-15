"""The layer the engine adds on top of every Linux Task image.

A Task image is whatever its author built, from any base. What the engine itself needs in
a sandbox is added here, when the image is prepared, as one more image layer:

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
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from ale.core.errors import ProviderStartError, TaskDefinitionError
from ale.core.ids import content_hash
from ale.run import guestd
from ale.run.sources import cache_root

__all__ = ["CUA_DRIVER_VERSION", "LABEL", "framework_identity", "with_framework"]

LABEL = "ale.framework"
CUA_DRIVER_VERSION = "0.12.6"
_CUA_DRIVER_ASSET = f"cua-driver-rs-{CUA_DRIVER_VERSION}-linux-x86_64-binary.tar.gz"
_CUA_DRIVER_SHA256 = "795b8464a25b036fe1a47d9cd1f560d6b4e2ab58e23630c0de3d9feff4757039"
_CUA_DRIVER_URL = (
    "https://github.com/trycua/cua/releases/download/"
    f"cua-driver-rs-v{CUA_DRIVER_VERSION}/{_CUA_DRIVER_ASSET}"
)

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
if ! id -u "$user" >/dev/null 2>&1; then
    echo "ALE: the agent account '$user' does not exist in this image; create it in the" \
        "image or name an existing account with image.user in task.yaml" >&2
    exit 1
fi
chown -R 0:0 /opt/ale
chmod -R go-rwx /opt/ale
if [ -f /etc/systemd/system/ale-guestd.service ]; then
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
"""


def framework_identity(*, user: str, sudo: bool, vm: bool) -> str:
    """Everything the layer is built from, so a change to any of it rebuilds it."""
    return content_hash(
        {
            "guestd": {
                name: hashlib.sha256((guestd.SOURCE / name).read_bytes()).hexdigest()
                for name in guestd.FILES
            },
            "cua_driver": _CUA_DRIVER_SHA256,
            "setup": _SETUP,
            "unit": _GUEST_UNIT if vm else None,
            "user": user,
            "sudo": sudo,
        }
    )


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


async def _build(base: dict, tag: str, identity: str, *, user: str, sudo: bool, vm: bool) -> None:
    driver = await asyncio.to_thread(_cua_driver)
    base_tag = f"ale-framework-base:{base['Id'].removeprefix('sha256:')}"
    code, _, stderr = await _run("docker", "tag", base["Id"], base_tag)
    if code != 0:
        raise ProviderStartError(f"could not reference the Task image: {stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="ale-framework-") as temporary:
        context = Path(temporary)
        shutil.copytree(guestd.SOURCE, context / "guestd", ignore=_only(guestd.FILES))
        shutil.copy2(driver, context / "cua-driver")
        (context / "setup.sh").write_text(_SETUP)
        lines = [
            f"FROM {base_tag}",
            "USER root",
            "COPY guestd/ /opt/ale/guestd/",
            "COPY --chmod=0755 cua-driver /usr/local/bin/cua-driver",
            "COPY setup.sh /opt/ale/setup.sh",
        ]
        if vm:
            (context / "ale-guestd.service").write_text(_GUEST_UNIT.format(user=user))
            lines.append("COPY ale-guestd.service /etc/systemd/system/ale-guestd.service")
        lines.append(_json_run("/bin/sh", "/opt/ale/setup.sh", user, "true" if sudo else "false"))
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


def _json_run(*argv: str) -> str:
    return "RUN " + json.dumps(list(argv))


def _only(names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    return lambda directory, entries: [entry for entry in entries if entry not in names]


def _cua_driver() -> Path:
    """The pinned Cua Driver binary, downloaded once and verified against its release."""
    root = cache_root() / "cua-driver" / CUA_DRIVER_VERSION
    binary = root / "cua-driver"
    if binary.is_file():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    archive = root / _CUA_DRIVER_ASSET
    with (root / "download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file():
            return binary
        try:
            with urllib.request.urlopen(_CUA_DRIVER_URL, timeout=300) as response:
                archive.write_bytes(response.read())
        except OSError as exc:
            raise ProviderStartError(f"could not download Cua Driver: {exc}") from exc
        if hashlib.sha256(archive.read_bytes()).hexdigest() != _CUA_DRIVER_SHA256:
            archive.unlink()
            raise ProviderStartError("downloaded Cua Driver does not match its pinned checksum")
        with tarfile.open(archive) as bundle:
            member = next(
                (
                    m
                    for m in bundle.getmembers()
                    if m.isfile() and Path(m.name).name == "cua-driver"
                ),
                None,
            )
            source = bundle.extractfile(member) if member is not None else None
            if source is None:
                raise ProviderStartError("Cua Driver archive has no cua-driver binary")
            partial = root / "cua-driver.partial"
            with source, partial.open("wb") as output:
                shutil.copyfileobj(source, output)
        partial.chmod(0o755)
        partial.replace(binary)
        archive.unlink()
    return binary


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


async def _acquire(handle) -> None:  # type: ignore[no-untyped-def]
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            await asyncio.sleep(0.1)


async def _run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ProviderStartError(f"{' '.join(argv[:3])} timed out after {timeout:g}s") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
