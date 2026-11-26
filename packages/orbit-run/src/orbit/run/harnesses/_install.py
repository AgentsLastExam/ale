"""How an agent program reaches a Sandbox: worked out on the Host, unpacked in the Sandbox.

The Host works out what to install: a list of archives, each from the program's official
distribution with the digest its publisher states or Orbit pins (an npm package's integrity
value from the registry; a pinned SHA-256 for Node.js and a release). One script then runs in
the Sandbox on the framework's Python, as the account the files are for (``_unpack``): it
checks every archive by its digest and unpacks it, so installing needs no npm, Node.js,
Python or network of the image's, and an unverifiable build never runs.

Where the archives come from depends on the Sandbox. The Host downloads them once into its
cache and uploads them to a Sandbox near it; a Sandbox far from the Host, such as a cloud
one, downloads them itself (``Sandbox.fetches_files``), which is several times faster than an
upload over the Host's link.

npm delivers two kinds of program. A native one ships its build in a package per platform,
which the main package lists as an optional dependency; only that package is installed, so
any published version can be pinned. A JavaScript one runs on Node.js: the npm of Orbit's
pinned Node.js resolves its dependency tree on the Host for the Sandbox's platform, without
downloading or running any package, and each package of the tree is one archive. No install
script runs: an optional dependency that has one is left out, as a failed build leaves it out
of an npm install. The program runs on its own copy of that Node.js, which only its launcher
names, so the agent's PATH and any Node.js of the Task's stay as the image made them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform as host
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Literal

from orbit.core.errors import AgentError, AgentUnsupportedError
from orbit.core.sandbox import Identity, Sandbox
from orbit.core.taskspec import OperatingSystem
from orbit.run.harnesses._agent import (
    agent_path,
    agent_python,
    bash_command,
    reported_version,
    version_mismatch,
)
from orbit.run.sources import cache_root, cached

REGISTRY = "https://registry.npmjs.org"
#: The Node.js builds Orbit runs, by version and npm platform: the SHA-256 nodejs.org
#: publishes for each archive. A ``.tar.gz`` on Linux and macOS, a ``.zip`` on Windows.
NODE: Mapping[str, Mapping[str, str]] = {
    "24.15.0": {
        "linux-x64": "44836872d9aec49f1e6b52a9a922872db9a2b02d235a616a5681b6a85fec8d89",
        "linux-arm64": "73afc234d558c24919875f51c2d1ea002a2ada4ea6f83601a383869fefa64eed",
        "darwin-x64": "ffd5ee293467927f3ee731a553eb88fd1f48cf74eebc2d74a6babe4af228673b",
        "darwin-arm64": "372331b969779ab5d15b949884fc6eaf88d5afe87bde8ba881d6400b9100ffc4",
        "win32-x64": "cc5149eabd53779ce1e7bdc5401643622d0c7e6800ade18928a767e940bb0e62",
    },
}
#: The Node.js the Host runs npm and decompression on.
HOST_NODE = "24.15.0"
#: Below the agent's home: a program's files and its Node.js, outside every PATH. Only the
#: launcher in ``~/.local/bin`` names them.
PROGRAMS = ".local/share/orbit"

_UNPACK = Path(__file__).with_name("_unpack.py")
#: ``uname -m`` spellings of the two CPU architectures.
_ARCHITECTURES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_NPM_OS = {
    OperatingSystem.LINUX: "linux",
    OperatingSystem.MACOS: "darwin",
    OperatingSystem.WINDOWS: "win32",
}
_NPM_CPU = {"x86_64": "x64", "aarch64": "arm64"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXACT = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class Platform:
    """A Sandbox's or the Host's system and CPU."""

    os: OperatingSystem
    machine: str
    """``x86_64`` or ``aarch64``."""
    avx2: bool = True
    """Whether an x86-64 CPU has AVX2, without which some programs need a baseline build."""

    @property
    def npm(self) -> str:
        """As npm and Node.js name it: ``linux-x64``, ``darwin-arm64``, ``win32-x64``."""
        return f"{_NPM_OS[self.os]}-{_NPM_CPU[self.machine]}"


@dataclass(frozen=True)
class Archive:
    """One archive to unpack below a target in the Sandbox, accepted only by ``digest``.

    It is ``url``'s, which a Sandbox that fetches files downloads itself, or a file only the
    Host can make (``made``), which it uploads to every Sandbox.
    """

    digest: str
    """``sha256:<hex>`` or ``sha512:<hex>``."""
    unpack: Literal["package", "members", "file"]
    url: str | None = None
    made: Path | None = None
    into: str = ""
    """The directory below the target it unpacks into."""
    members: tuple[tuple[str, str, str | None], ...] = ()
    """For ``members``: each member's path in the archive, the name it gets and its own
    SHA-256, if it has one."""
    name: str = ""
    """For ``file``: the name it gets."""

    def moved(self, into: str) -> Archive:
        return Archive(self.digest, self.unpack, self.url, self.made, into, self.members, self.name)


@dataclass(frozen=True)
class Tree:
    """A JavaScript program's packages, and the script each of its programs runs."""

    archives: tuple[Archive, ...]
    bin: Mapping[str, str] = field(default_factory=dict)


async def sandbox_platform(sandbox: Sandbox, home: str) -> Platform:
    """The Sandbox's system and CPU, as its agent sees them."""
    system = sandbox.request.os
    probe = await sandbox.exec(
        bash_command(home, "uname -m", login=False), timeout_sec=60, identity=Identity.AGENT
    )
    reported = probe.stdout.strip().lower()
    machine = _ARCHITECTURES.get(reported)
    if machine is None:
        raise AgentUnsupportedError(
            f"no agent program builds for {system.value}/{reported or '?'}"
            + (f": {probe.stderr.strip()[-300:]}" if probe.stderr.strip() else "")
        )
    avx2 = True
    if system is not OperatingSystem.MACOS and machine == "x86_64":
        # Linux's and Git Bash's /proc/cpuinfo; a CPU that cannot be read keeps the
        # ordinary build, as the programs' own installers do.
        flags = await sandbox.exec(
            bash_command(home, "cat /proc/cpuinfo", login=False),
            timeout_sec=60,
            identity=Identity.AGENT,
        )
        avx2 = not flags.ok or re.search(r"\bavx2\b", flags.stdout) is not None
    return Platform(system, machine, avx2)


def host_platform() -> Platform:
    system = {"linux": OperatingSystem.LINUX, "darwin": OperatingSystem.MACOS}.get(sys.platform)
    machine = _ARCHITECTURES.get(host.machine().lower())
    if system is None or machine is None:
        raise AgentUnsupportedError(f"Orbit fetches no Node.js for a {sys.platform} Host")
    return Platform(system, machine)


# --- what to install ---------------------------------------------------------------


def native_package(package: str, version: str, name: str, executable: str) -> list[Archive]:
    """``package@version``'s optional dependency ``name``, which holds the native program.

    An ``executable`` named ``*.br`` is shipped brotli-compressed, which Python's standard
    library cannot undo: the Host decompresses it and uploads the program alone.
    """
    if executable.endswith(".br"):
        return [_decompressed(package, version, name, executable)]
    url, sha512 = _platform_package(package, version, name)
    return [Archive(f"sha512:{sha512}", "package", url=url)]


def npm_tree(package: str, version: str, platform: Platform) -> Tree:
    """``package@version`` and its dependencies for ``platform``, as npm lays them out.

    Resolved once per Host by the npm of its Node.js, which reads only the registry's
    metadata, and kept in the Host cache.
    """

    def fill(into: Path) -> None:
        node = node_distribution(HOST_NODE, host_platform())
        with tempfile.TemporaryDirectory(dir=into) as prefix:
            argv = [
                str(node / "bin" / "node"),
                str(node / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"),
                "install",
                f"{package}@{version}",
                "--prefix",
                prefix,
                f"--registry={REGISTRY}/",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--loglevel=error",
                f"--os={_NPM_OS[platform.os]}",
                f"--cpu={_NPM_CPU[platform.machine]}",
                *(["--libc=glibc"] if platform.os is OperatingSystem.LINUX else []),
            ]
            env = {
                **os.environ,
                "npm_config_cache": str(cache_root() / "npm-cache"),
                "npm_config_update_notifier": "false",
            }
            try:
                result = subprocess.run(
                    argv, env=env, capture_output=True, text=True, timeout=1800, check=False
                )
            except subprocess.TimeoutExpired:
                raise AgentError(
                    f"npm took over 30 minutes to resolve {package}@{version}"
                ) from None
            if result.returncode != 0:
                raise AgentError(
                    f"could not resolve {package}@{version} for {platform.npm}: "
                    f"{(result.stderr or result.stdout)[-800:]}"
                )
            lock = json.loads(Path(prefix, "package-lock.json").read_text(encoding="utf-8"))
        (into / "tree.json").write_text(json.dumps(_tree(lock, package, platform)))

    root = cached(cache_root() / "npm" / package / version / f"tree-{platform.npm}", fill)
    tree = json.loads((root / "tree.json").read_text(encoding="utf-8"))
    return Tree(tuple(Archive(**archive) for archive in tree["archives"]), tree["bin"])


def node_runtime(version: str, platform: Platform) -> Archive:
    """The Node.js executable of ``version`` for ``platform``, from nodejs.org's archive."""
    digest = NODE.get(version, {}).get(platform.npm)
    if digest is None:
        raise AgentUnsupportedError(f"Orbit pins no Node.js {version} for {platform.npm}")
    name, archive = _node_archive(version, platform)
    executable = "node.exe" if platform.os is OperatingSystem.WINDOWS else "node"
    member = f"{name}/{executable}" if executable == "node.exe" else f"{name}/bin/node"
    return Archive(
        f"sha256:{digest}",
        "members",
        url=f"https://nodejs.org/dist/v{version}/{archive}",
        members=((member, executable, None),),
    )


def node_path(directory: str, version: str, platform: Platform) -> str:
    """Where a Node.js of ``version`` lives below ``directory`` in the Sandbox."""
    executable = "node.exe" if platform.os is OperatingSystem.WINDOWS else "node"
    return f"{directory}/node/{version}/{executable}"


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

    def archive(self, name: str) -> Archive:
        """The asset as an archive that unpacks into ``name``."""
        if self.member is None:
            return Archive(f"sha256:{self.sha256}", "file", url=self.url, name=name)
        return Archive(
            f"sha256:{self.sha256}",
            "members",
            url=self.url,
            members=((self.member, name, self.member_sha256),),
        )


ReleaseAssets = Mapping[tuple[OperatingSystem, str], ReleaseAsset]
#: A native npm program's build per npm platform: the optional dependency holding it and
#: the executable's path inside that package, ``*.br`` where npm ships it brotli-compressed.
PlatformPackages = Mapping[str, tuple[str, str]]


def release_identity(assets: ReleaseAssets) -> str:
    """``sha256:`` of the whole pinned table: it names every binary this version may run."""
    lines = sorted(
        f"{system.value}/{architecture} {asset.url} {asset.sha256}"
        + (f" {asset.member} {asset.member_sha256}" if asset.member else "")
        for (system, architecture), asset in assets.items()
    )
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


# --- the Sandbox -------------------------------------------------------------------


async def unpack(
    sandbox: Sandbox,
    archives: Sequence[Archive],
    target: str,
    *,
    identity: Identity,
    fetch: bool,
    executables: Sequence[str] = (),
) -> None:
    """Unpack ``archives`` below ``target`` as ``identity``, then make ``executables``, below
    it too, executable.

    With ``fetch`` the Sandbox downloads every archive that has a URL; otherwise the Host
    uploads them from its cache. Either way the account makes its own directories, which
    guestd would make as root.
    """
    system = sandbox.request.os
    windows = system is OperatingSystem.WINDOWS
    staging = f"{target}/.orbit-unpack"
    if not windows:
        made = await sandbox.exec(["mkdir", "-p", "--", staging], timeout_sec=60, identity=identity)
        if not made.ok:
            raise AgentError(f"could not create {staging}: {made.stderr[-400:]}")
    entries: list[dict[str, object]] = []
    uploads: dict[str, Path] = {}
    for index, archive in enumerate(archives):
        entry: dict[str, object] = {
            "digest": archive.digest,
            "unpack": archive.unpack,
            "into": archive.into,
            "members": [list(member) for member in archive.members],
            "name": archive.name,
        }
        if fetch and archive.url is not None:
            entry["url"] = archive.url
        else:
            uploads[str(index)] = archive.made or await asyncio.to_thread(_downloaded, archive)
        entries.append(entry)
    if uploads:
        # In the cache, so the archives are linked into it rather than copied.
        cache_root().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache_root()) as files:
            for name, path in uploads.items():
                _link(path, Path(files, name))
            await sandbox.upload_dir(files, staging, identity=identity)
    manifest = {"archives": entries, "executables": list(executables)}
    await sandbox.write_file(f"{staging}/unpack.py", _UNPACK.read_bytes(), identity=identity)
    await sandbox.write_file(
        f"{staging}/manifest.json", json.dumps(manifest).encode(), identity=identity
    )
    env: dict[str, str] = {}
    if any("url" in entry for entry in entries):
        # A Task image may carry no CA certificates; the Host's bundle checks the sources.
        import certifi

        await sandbox.write_file(
            f"{staging}/ca.pem", Path(certifi.where()).read_bytes(), identity=identity
        )
        env["SSL_CERT_FILE"] = f"{staging}/ca.pem"
    python = agent_python(system)
    result = await sandbox.exec(
        [python, "-I", f"{staging}/unpack.py", f"{staging}/manifest.json", target],
        env=env,
        timeout_sec=1800,
        identity=identity,
    )
    if not result.ok:
        raise AgentError(
            f"could not unpack into {target}: {(result.stderr or result.stdout)[-800:]}"
        )


async def write_launcher(
    sandbox: Sandbox, path: str, argv: Sequence[str], *, identity: Identity = Identity.AGENT
) -> None:
    """A ``sh`` script at ``path`` that runs ``argv`` with its own arguments; Git Bash runs
    it on Windows."""
    script = f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n'
    windows = sandbox.request.os is OperatingSystem.WINDOWS
    if not windows:
        made = await sandbox.exec(
            ["mkdir", "-p", "--", path.rsplit("/", 1)[0]], timeout_sec=60, identity=identity
        )
        if not made.ok:
            raise AgentError(f"could not create {path}'s directory: {made.stderr[-400:]}")
    await sandbox.write_file(path, script.encode(), identity=identity)
    if not windows:
        result = await sandbox.exec(["chmod", "755", path], timeout_sec=60, identity=identity)
        if not result.ok:
            raise AgentError(f"could not make {path} executable: {result.stderr[-400:]}")


async def installed(
    sandbox: Sandbox,
    home: str,
    argv: Sequence[str],
    version: str,
    *,
    identity: Identity = Identity.AGENT,
) -> str | None:
    """``version`` when ``argv --version`` runs and names it."""
    return (await _probe(sandbox, home, argv, version, identity))[0]


async def verified(
    sandbox: Sandbox,
    home: str,
    argv: Sequence[str],
    version: str,
    *,
    binary: str,
    identity: Identity = Identity.AGENT,
) -> str:
    """``version``, which ``argv --version`` must name."""
    reported, output = await _probe(sandbox, home, argv, version, identity)
    if reported is None:
        raise AgentError(version_mismatch(binary, version, output))
    return reported


async def install_release(
    sandbox: Sandbox,
    home: str,
    *,
    binary: str,
    version: str,
    assets: ReleaseAssets,
    platform: Platform,
    directory: str | None = None,
) -> str:
    """Install the Sandbox platform's pinned asset as the agent, into its ``~/.local/bin``
    or the ``directory`` a program looks for the binary in."""
    asset = assets.get((platform.os, platform.machine))
    if asset is None:
        raise AgentUnsupportedError(
            f"{binary} {version} has no official release for {platform.os.value}/{platform.machine}"
        )
    name = binary + (".exe" if platform.os is OperatingSystem.WINDOWS else "")
    target = directory or f"{agent_path(home)}/.local/bin"
    program = f"{target}/{name}"
    if await installed(sandbox, home, [program], version) is None:
        await unpack(
            sandbox,
            [asset.archive(name)],
            target,
            identity=Identity.AGENT,
            fetch=sandbox.fetches_files,
            executables=[name],
        )
    return await verified(sandbox, home, [program], version, binary=binary)


# --- helpers -----------------------------------------------------------------------


def _platform_package(package: str, version: str, name: str) -> tuple[str, str]:
    """The tarball URL and hex sha512 of ``package@version``'s optional dependency ``name``,
    kept in the Host cache once read from the registry."""

    def fill(into: Path) -> None:
        spec = _manifest(package, version).get("optionalDependencies", {}).get(name)
        if not isinstance(spec, str):
            raise AgentUnsupportedError(f"{package}@{version} lists no {name}")
        real, pinned = name, spec
        if spec.startswith("npm:"):
            real, _, pinned = spec.removeprefix("npm:").rpartition("@")
        if not _EXACT.fullmatch(pinned):
            raise AgentError(f"{package}@{version} names {name} by a range: {spec}")
        dist = _manifest(real, pinned).get("dist", {})
        (into / "package.json").write_text(
            json.dumps({"url": dist.get("tarball"), "sha512": _sha512(dist, f"{real}@{pinned}")})
        )

    root = cached(cache_root() / "npm" / package / version / name, fill)
    resolved = json.loads((root / "package.json").read_text(encoding="utf-8"))
    return resolved["url"], resolved["sha512"]


def _decompressed(package: str, version: str, name: str, executable: str) -> Archive:
    url, sha512 = _platform_package(package, version, name)
    program = executable.removesuffix(".br")

    def fill(into: Path) -> None:
        tarball = download(url, into / ".tarball", sha512=sha512)
        with tarfile.open(tarball) as archive:
            source = archive.extractfile(f"package/{executable}")
            if source is None:
                raise AgentError(f"{name} has no {executable}")
            (into / "compressed").write_bytes(source.read())
        tarball.unlink()
        _decompress(into / "compressed", into / "program")

    made = cached(cache_root() / "npm" / package / version / f"{name}-decompressed", fill)
    path = made / "program"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return Archive(f"sha256:{digest}", "file", made=path, name=program)


def _tree(lock: dict, package: str, platform: Platform) -> dict:
    """The archives of a resolved lockfile that ``platform`` installs, by where they go."""
    archives = []
    for path, entry in lock.get("packages", {}).items():
        if not path.startswith("node_modules/") or entry.get("dev") or entry.get("inBundle"):
            continue
        if entry.get("optional") and (
            entry.get("hasInstallScript") or not _supports(entry, platform)
        ):
            continue
        if not entry.get("resolved") or not entry.get("integrity"):
            raise AgentError(f"npm resolved no archive for {path}")
        archives.append(
            {
                "digest": f"sha512:{_sha512(entry, path)}",
                "unpack": "package",
                "url": entry["resolved"],
                "into": path,
            }
        )
    main = lock["packages"].get(f"node_modules/{package}", {})
    declared = main.get("bin") or {}
    return {"archives": archives, "bin": declared}


def _supports(entry: dict, platform: Platform) -> bool:
    """Whether a package's ``os``, ``cpu`` and ``libc`` admit ``platform``, as npm reads them:
    a name allows, a ``!name`` refuses, and no list allows all."""
    values = {
        "os": _NPM_OS[platform.os],
        "cpu": _NPM_CPU[platform.machine],
        "libc": "glibc" if platform.os is OperatingSystem.LINUX else None,
    }
    for key, value in values.items():
        listed = entry.get(key) or []
        if value is None or not listed:
            continue
        if f"!{value}" in listed:
            return False
        allowed = [item for item in listed if not item.startswith("!")]
        if allowed and value not in allowed:
            return False
    return True


def _sha512(dist: dict, what: str) -> str:
    """The hex sha512 of an npm integrity value."""
    for item in str(dist.get("integrity", "")).split():
        if item.startswith("sha512-"):
            return base64.b64decode(item.removeprefix("sha512-")).hex()
    raise AgentError(f"{what} has no sha512 integrity value")


def _downloaded(archive: Archive) -> Path:
    """``archive`` in the Host cache, downloaded once and checked by its digest."""
    assert archive.url is not None
    algorithm, expected = archive.digest.split(":", 1)

    def fill(into: Path) -> None:
        download(archive.url, into / "archive", **{algorithm: expected})  # type: ignore[arg-type]

    return cached(cache_root() / "downloads" / f"{algorithm}-{expected}", fill) / "archive"


def _link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def node_distribution(version: str, platform: Platform) -> Path:
    """nodejs.org's ``version`` for ``platform``, unpacked on the Host for its npm."""
    digest = NODE.get(version, {}).get(platform.npm)
    if digest is None:
        raise AgentUnsupportedError(f"Orbit pins no Node.js {version} for {platform.npm}")
    name, archive = _node_archive(version, platform)

    def fill(into: Path) -> None:
        path = download(
            f"https://nodejs.org/dist/v{version}/{archive}", into / ".download", sha256=digest
        )
        with tarfile.open(path) as tarball:
            tarball.extractall(into, filter="data")
        path.unlink()

    return cached(cache_root() / "node" / version / platform.npm, fill) / name


def _node_archive(version: str, platform: Platform) -> tuple[str, str]:
    if platform.os is OperatingSystem.WINDOWS:
        name = f"node-v{version}-win-{_NPM_CPU[platform.machine]}"
        return name, f"{name}.zip"
    name = f"node-v{version}-{platform.npm}"
    return name, f"{name}.tar.gz"


def _manifest(name: str, version: str) -> dict:
    url = f"{REGISTRY}/{name.replace('/', '%2F')}/{version}"
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return json.load(response)
    except (OSError, HTTPException, ValueError) as error:
        raise AgentError(
            f"could not read {name}@{version} from the npm registry: {error}"
        ) from error


def download(
    url: str, target: Path, *, sha256: str | None = None, sha512: str | None = None
) -> Path:
    """``url`` streamed to ``target``, accepted only by the hex digest given."""
    digests = {"sha256": hashlib.sha256(), "sha512": hashlib.sha512()}
    try:
        with urllib.request.urlopen(url, timeout=600) as response, target.open("wb") as out:
            while block := response.read(1 << 20):
                for digest in digests.values():
                    digest.update(block)
                out.write(block)
    except (OSError, HTTPException) as error:
        raise AgentError(f"could not download {url}: {error}") from error
    if sha256 is not None and (actual := digests["sha256"].hexdigest()) != sha256:
        raise AgentError(f"{url} has sha256 {actual}, not the pinned {sha256}")
    if sha512 is not None and (actual := digests["sha512"].hexdigest()) != sha512:
        raise AgentError(f"{url} has sha512 {actual}, not the expected {sha512}")
    return target


def _decompress(source: Path, target: Path) -> None:
    """Brotli, which Python's standard library lacks, by the Host's Node.js."""
    node = node_distribution(HOST_NODE, host_platform()) / "bin" / "node"
    script = (
        "const fs = require('fs'), zlib = require('zlib');"
        "const data = fs.readFileSync(process.argv[1]);"
        "fs.writeFileSync(process.argv[2], zlib.brotliDecompressSync(data));"
    )
    try:
        result = subprocess.run(
            [str(node), "-e", script, str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AgentError(f"could not decompress {source.name} in 10 minutes") from None
    if result.returncode != 0:
        raise AgentError(f"could not decompress {source.name}: {result.stderr[-400:]}")
    source.unlink()


async def _probe(
    sandbox: Sandbox, home: str, argv: Sequence[str], version: str, identity: Identity
) -> tuple[str | None, str]:
    probe = await sandbox.exec(
        bash_command(home, f"{shlex.join(argv)} --version", login=False),
        timeout_sec=60,
        identity=identity,
    )
    output = f"{probe.stdout}\n{probe.stderr}"
    return (reported_version(output, version) if probe.ok else None), output
