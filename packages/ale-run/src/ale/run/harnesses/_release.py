"""Install a CLI from its pinned official release binary, verified by digest."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ale.core.errors import AgentError, AgentUnsupportedError
from ale.core.sandbox import Identity, Sandbox
from ale.core.taskspec import OperatingSystem
from ale.run.harnesses._npm import (
    agent_home,
    agent_path,
    bash_command,
    is_windows,
    reported_version,
    version_mismatch,
)

#: ``uname -m`` spellings of the two CPU architectures release tables name.
_ARCHITECTURES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Run by the Sandbox's own Python, which both bases provide, so download, verification
# and replacement behave alike on Linux and Windows. A matching file is reused; anything
# else, a link included, is replaced only by bytes whose digest matched. From an archive,
# only the named member is read, so no other entry can land anywhere.
_INSTALL = r"""
import hashlib, os, shutil, sys, tarfile, tempfile, urllib.request, zipfile

url, expected, target, member, member_expected = sys.argv[1:6]
runs = member_expected if member else expected

def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            value.update(block)
    return value.hexdigest()

if not os.path.islink(target) and os.path.isfile(target) and digest(target) == runs:
    sys.exit(0)
os.makedirs(os.path.dirname(target), exist_ok=True)
temporaries = []

def temporary():
    handle, path = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".download-")
    os.close(handle)
    temporaries.append(path)
    return path

try:
    download = temporary()
    with open(download, "wb") as out, urllib.request.urlopen(url, timeout=600) as response:
        shutil.copyfileobj(response, out, 1 << 20)
    actual = digest(download)
    if actual != expected:
        sys.exit(f"{url} has sha256 {actual}, not the pinned {expected}")
    executable = download
    if member:
        executable = temporary()
        with open(executable, "wb") as out:
            if zipfile.is_zipfile(download):
                with zipfile.ZipFile(download) as archive, archive.open(member) as source:
                    shutil.copyfileobj(source, out, 1 << 20)
            else:
                with tarfile.open(download) as archive:
                    entry = archive.getmember(member)
                    if not entry.isfile():
                        sys.exit(f"{member} in {url} is not a regular file")
                    shutil.copyfileobj(archive.extractfile(entry), out, 1 << 20)
        actual = digest(executable)
        if actual != member_expected:
            sys.exit(f"{member} in {url} has sha256 {actual}, not the pinned {member_expected}")
    os.chmod(executable, 0o755)
    os.replace(executable, target)
finally:
    for path in temporaries:
        if os.path.exists(path):
            os.unlink(path)
"""


@dataclass(frozen=True)
class ReleaseAsset:
    """One platform's official release download and the SHA-256 that pins it.

    The download is the executable itself, or a tar or zip archive whose ``member`` is,
    pinned separately by ``member_sha256``.
    """

    url: str
    sha256: str
    member: str | None = None
    member_sha256: str | None = None

    # --- validation ---

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError(f"release asset must be fetched over https: {self.url}")
        for digest in (self.sha256, self.member_sha256):
            if digest is not None and not _SHA256.fullmatch(digest):
                raise ValueError(
                    f"release asset sha256 must be 64 lowercase hex digits: {self.url}"
                )
        if (self.member is None) != (self.member_sha256 is None):
            raise ValueError(f"an archive member needs its own sha256, and only then: {self.url}")


ReleaseAssets = Mapping[tuple[OperatingSystem, str], ReleaseAsset]


def release_identity(assets: ReleaseAssets) -> str:
    """``sha256:`` of the whole pinned table: it names every binary this version may run."""
    lines = sorted(
        f"{system.value}/{architecture} {asset.url} {asset.sha256}"
        + (f" {asset.member} {asset.member_sha256}" if asset.member else "")
        for (system, architecture), asset in assets.items()
    )
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


async def ensure_release_binary(
    sandbox: Sandbox, *, binary: str, version: str, assets: ReleaseAssets
) -> str:
    """Install the Sandbox platform's pinned asset into the agent's prefix as the agent."""
    home = await agent_home(sandbox)
    system = sandbox.request.os
    machine = await sandbox.exec(
        bash_command(home, "uname -m", login=False), timeout_sec=60, identity=Identity.AGENT
    )
    reported = machine.stdout.strip().lower()
    asset = assets.get((system, _ARCHITECTURES.get(reported, reported)))
    if asset is None:
        raise AgentUnsupportedError(
            f"{binary} {version} has no official release for {system.value}/{reported or '?'}"
        )
    target = f"{agent_path(home)}/.local/bin/{binary}" + (".exe" if is_windows(home) else "")
    result = await sandbox.exec(
        [
            "python" if is_windows(home) else "python3",
            "-c",
            _INSTALL,
            asset.url,
            asset.sha256,
            target,
            asset.member or "",
            asset.member_sha256 or "",
        ],
        timeout_sec=1200,
        identity=Identity.AGENT,
    )
    if not result.ok:
        detail = (result.stderr or result.stdout)[-800:]
        raise AgentError(f"could not install {binary} {version}: {detail}")
    probe = await sandbox.exec([target, "--version"], timeout_sec=60, identity=Identity.AGENT)
    output = f"{probe.stdout}\n{probe.stderr}"
    installed = reported_version(output, version) if probe.ok else None
    if installed is None:
        raise AgentError(version_mismatch(binary, version, output))
    return installed
