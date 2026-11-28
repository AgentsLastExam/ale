"""The engine a cluster's compute nodes run each episode with.

The login node installs it below the cache once per identity: the framework's Python for the
nodes' architecture, with the engine's own packages and every locked dependency in its
``site-packages``, all from wheels, so a compute node needs no network, compiler or Python
of its own to run an episode, whatever architecture the login node has.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
from importlib import metadata
from pathlib import Path

from orbit.core.capability import Arch
from orbit.core.config import ClusterProviderConfig
from orbit.core.errors import ProviderStartError
from orbit.core.ids import content_hash
from orbit.run.framework import PYTHON_RELEASE, linux_python
from orbit.run.process import run
from orbit.run.provenance import INSTALLED_COMMIT, framework_provenance
from orbit.run.providers.cluster.job import cache

#: Below the cache: each engine, by its identity.
ENGINE = "engine"
#: The engine's packages an episode runs with; the Fleet's are no episode's.
PACKAGES = ("orbit-core", "orbit-run", "orbit-verify")
_PLATFORMS = {Arch.X86_64: "x86_64-unknown-linux-gnu", Arch.ARM64: "aarch64-unknown-linux-gnu"}
#: The framework's Python's version, whose ``site-packages`` the engine installs into.
_SITE = Path("lib/python3.12/site-packages")


def _sources() -> tuple[Path, tuple[Path, ...]]:
    """The checkout this engine runs from, the directory of its ``uv.lock``, and the source
    directory of each of its packages, which uv installed from there."""
    directories = []
    for name in PACKAGES:
        url = json.loads(metadata.distribution(name).read_text("direct_url.json") or "{}")
        location = str(url.get("url", ""))
        if not location.startswith("file://"):
            raise ProviderStartError(f"{name} is not installed from a checkout: {location!r}")
        directories.append(Path(location.removeprefix("file://")))
    root = next((p for p in directories[0].parents if (p / "uv.lock").is_file()), None)
    if root is None:
        raise ProviderStartError(f"no uv.lock above {directories[0]}")
    return root, tuple(directories)


def identity(arch: Arch) -> str:
    """What the engine for ``arch`` is installed from: the lock, every file of its packages'
    sources, the Python and the platform."""
    root, directories = _sources()
    digest = hashlib.sha256((root / "uv.lock").read_bytes())
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return content_hash(
        {"sources": digest.hexdigest(), "python": PYTHON_RELEASE, "platform": _PLATFORMS[arch]}
    )


async def engine(config: ClusterProviderConfig) -> Path:
    """The engine's Python for the cluster's compute nodes, installed below the cache once."""
    arch = config.architecture
    key = identity(arch).removeprefix("sha256:")[:16]
    root = cache(config) / ENGINE
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    installed = root / key
    python = installed / "python" / "bin" / "python3"
    with (root / f"{key}.lock").open("a") as lock:
        await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)
        if not python.is_file():
            partial = root / f".{key}.part"
            await asyncio.to_thread(shutil.rmtree, partial, True)
            await asyncio.to_thread(
                shutil.copytree, linux_python(arch), partial / "python", symlinks=True
            )
            await _install(partial / "python" / _SITE, arch)
            partial.rename(installed)
    return python


async def _install(site: Path, arch: Arch) -> None:
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        raise ProviderStartError("installing the engine for the cluster's nodes needs uv")
    root, directories = _sources()
    platform = ["--python-platform", _PLATFORMS[arch], "--python-version", "3.12"]
    requirements = site.parent / "requirements.txt"
    steps = [
        [uv, "export", "--project", str(root), "--frozen", "--no-dev", "--no-hashes",
         "--no-emit-workspace", "--package", "orbit-run", "--quiet",
         "--output-file", str(requirements)],
        [uv, "pip", "install", "--quiet", "--target", str(site), *platform,
         "--only-binary", ":all:", "--requirement", str(requirements)],
        [uv, "pip", "install", "--quiet", "--target", str(site), *platform, "--no-deps",
         *map(str, directories)],
    ]  # fmt: skip
    for argv in steps:
        code, out, err = await run(*argv, timeout=1800)
        if code:
            raise ProviderStartError(f"installing the engine failed: {err.strip() or out.strip()}")
    requirements.unlink()
    # It has no checkout: it records the one it was installed from.
    commit = site / INSTALLED_COMMIT.relative_to(INSTALLED_COMMIT.parents[2])
    commit.write_text(framework_provenance().commit + "\n")
