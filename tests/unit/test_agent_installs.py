"""Agent programs: worked out on the Host as archives, unpacked in the Sandbox.

The Host resolves what to install and caches the archives; the Sandbox's unpack script
checks each archive's digest, unpacks it as the account the files are for, and downloads it
itself where the Sandbox fetches files.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbit.core.config import AgentJudgeConfig, VerificationConfig
from orbit.core.errors import AgentError, AgentUnsupportedError, VerificationInfrastructureError
from orbit.core.sandbox import ExecResult, Identity
from orbit.core.task import Stage
from orbit.core.taskspec import OperatingSystem
from orbit.run import guestd
from orbit.run.harnesses import _install
from orbit.run.harnesses._install import Archive, Platform, ReleaseAsset, Tree
from orbit.run.harnesses.builtin import OracleHarness
from orbit.run.harnesses.codex_cli import CodexCliHarness
from orbit.run.harnesses.droid import DroidHarness
from orbit.run.harnesses.pi_cli import PiCliHarness
from orbit.run.protocol import episode_protocol
from orbit.run.verification import install_agent_judge

pytestmark = pytest.mark.unit

LINUX = Platform(OperatingSystem.LINUX, "x86_64")
PROGRAM = b"#!/bin/sh\necho 'fixture 2.0.0'\n"
UNPACK = Path(_install.__file__).with_name("_unpack.py")
CALLS_THE_JUDGE = """\
from orbit_verify import judge

judge("agent", prompt="Does it work?")
"""


@pytest.fixture(autouse=True)
def cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("ORBIT_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


def _tar(files: dict[str, tuple[bytes, int]], path: Path) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return path


def _deb(members: dict[str, bytes], path: Path) -> Path:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    def entry(name: str, content: bytes) -> bytes:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(content):<10}`\n".encode()
        return header + content + (b"\n" if len(content) % 2 else b"")

    path.write_bytes(
        b"!<arch>\n" + entry("debian-binary", b"2.0\n") + entry("data.tar.xz", data.getvalue())
    )
    return path


def _digest(path: Path, algorithm: str = "sha256") -> str:
    return f"{algorithm}:{hashlib.new(algorithm, path.read_bytes()).hexdigest()}"


def _unpack(
    staging: Path, target: Path, archives: list[dict], executables: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """The Sandbox's unpack script, run here by this interpreter."""
    staging.mkdir(parents=True, exist_ok=True)
    manifest = staging / "manifest.json"
    manifest.write_text(json.dumps({"archives": archives, "executables": list(executables)}))
    return subprocess.run(
        [sys.executable, "-I", str(UNPACK), str(manifest), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the unpack script --------------------------------------------------------------


def test_the_sandbox_unpacks_every_kind_of_archive_it_is_given_or_fetches(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    package = _tar(
        {"package/bin/cli": (PROGRAM, 0o644), "package/README.md": (b"r", 0o644)},
        sources / "package.tgz",
    )
    release = _tar(
        {"dist/cli": (PROGRAM, 0o755), "dist/other": (b"x", 0o755)}, sources / "release.tgz"
    )
    windows = sources / "release.zip"
    with zipfile.ZipFile(windows, "w") as bundle:
        bundle.writestr("tool.exe", PROGRAM)
    deb = _deb({"./opt/app/app.cjs": b"app", "./opt/app/gui": b"\x7fELF"}, sources / "app.deb")
    raw = sources / "raw"
    raw.write_bytes(PROGRAM)
    staging = tmp_path / "target/.orbit-unpack"
    staging.mkdir(parents=True)
    (staging / "1").write_bytes(release.read_bytes())  # uploaded beside the manifest
    archives = [
        {
            "digest": _digest(package, "sha512"),
            "unpack": "package",
            "url": package.as_uri(),
            "into": "pkg",
        },
        {
            "digest": _digest(release),
            "unpack": "members",
            "into": "bin",
            "members": [["dist/cli", "cli", hashlib.sha256(PROGRAM).hexdigest()]],
        },
        {
            "digest": _digest(windows),
            "unpack": "members",
            "url": windows.as_uri(),
            "into": "w",
            "members": [["tool.exe", "tool.exe", None]],
        },
        {
            "digest": _digest(deb),
            "unpack": "members",
            "url": deb.as_uri(),
            "into": "app",
            "members": [["./opt/app/app.cjs", "app.cjs", hashlib.sha256(b"app").hexdigest()]],
        },
        {
            "digest": _digest(raw),
            "unpack": "file",
            "url": raw.as_uri(),
            "into": "",
            "name": "raw/program",
        },
    ]
    done = _unpack(staging, tmp_path / "target", archives, executables=("pkg/bin/cli",))
    assert done.returncode == 0, done.stderr
    target = tmp_path / "target"
    assert (target / "pkg/bin/cli").read_bytes() == PROGRAM
    assert (target / "pkg/bin/cli").stat().st_mode & 0o777 == 0o755
    assert (target / "pkg/README.md").exists()
    # Only the named members, under the names they are given.
    assert sorted(path.name for path in (target / "bin").iterdir()) == ["cli"]
    assert (target / "w/tool.exe").read_bytes() == PROGRAM
    assert sorted(path.name for path in (target / "app").iterdir()) == ["app.cjs"]
    assert (target / "raw/program").read_bytes() == PROGRAM
    assert not staging.exists()


def test_a_wrong_digest_stops_the_sandbox_before_anything_is_unpacked(tmp_path: Path) -> None:
    package = _tar({"package/bin/cli": (PROGRAM, 0o755)}, tmp_path / "package.tgz")
    staging = tmp_path / "target/.orbit-unpack"
    good = {"digest": _digest(package), "unpack": "package", "url": package.as_uri(), "into": "a"}
    tampered = {**good, "into": "b", "digest": "sha256:" + "0" * 64}
    refused = _unpack(staging, tmp_path / "target", [good, tampered])
    assert refused.returncode != 0 and "does not have the sha256" in refused.stderr
    assert sorted(path.name for path in (tmp_path / "target").iterdir()) == [".orbit-unpack"]
    member = {
        "digest": _digest(package),
        "unpack": "members",
        "url": package.as_uri(),
        "into": "c",
        "members": [["package/bin/cli", "cli", "0" * 64]],
    }
    assert "is not the pinned build" in _unpack(staging, tmp_path / "target", [member]).stderr
    absent = {**member, "members": [["package/absent", "absent", None]]}
    assert "has no package/absent" in _unpack(staging, tmp_path / "target", [absent]).stderr
    # A sandbox that cannot download says which file, not a traceback.
    unreachable = {**good, "url": (tmp_path / "missing.tgz").as_uri()}
    failed = _unpack(staging, tmp_path / "target", [unreachable]).stderr
    assert failed.startswith("could not download file:") and "Traceback" not in failed


# --- what the Host works out --------------------------------------------------------


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry of ``@acme/cli@1.0.0`` and its Linux build, served from files."""
    registry = tmp_path / "registry"
    tarball = _tar({"package/bin/cli.br": (b"compressed", 0o644)}, tmp_path / "cli-linux-x64.tgz")
    digest = hashlib.sha512(tarball.read_bytes()).digest()
    manifests = {
        "@acme/cli/1.0.0": {
            "optionalDependencies": {
                "@acme/cli-linux-x64": "npm:@acme/cli@1.0.0-linux-x64",
                "@acme/cli-darwin-arm64": "^1.0.0",
            },
        },
        "@acme/cli/1.0.0-linux-x64": {
            "dist": {
                "tarball": tarball.as_uri(),
                "integrity": "sha512-" + base64.b64encode(digest).decode(),
            }
        },
    }
    for name, manifest in manifests.items():
        (registry / name).parent.mkdir(parents=True, exist_ok=True)
        (registry / name).write_text(json.dumps(manifest))
    monkeypatch.setattr(_install, "REGISTRY", registry.as_uri())
    return tarball


def test_a_native_program_is_its_platform_package_as_the_registry_pins_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tarball = _registry(tmp_path, monkeypatch)
    (archive,) = _install.native_package("@acme/cli", "1.0.0", "@acme/cli-linux-x64", "bin/cli")
    assert archive == Archive(_digest(tarball, "sha512"), "package", url=tarball.as_uri())
    with pytest.raises(AgentUnsupportedError, match="lists no @acme/cli-win32-x64"):
        _install.native_package("@acme/cli", "1.0.0", "@acme/cli-win32-x64", "cli.exe")
    # Read once: a Host that ran it before needs no registry.
    monkeypatch.setattr(_install, "REGISTRY", "file:///nonexistent")
    again = _install.native_package("@acme/cli", "1.0.0", "@acme/cli-linux-x64", "bin/cli")
    assert again == [archive]


def test_a_platform_package_named_by_a_range_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(tmp_path, monkeypatch)
    with pytest.raises(AgentError, match="by a range"):
        _install.native_package("@acme/cli", "1.0.0", "@acme/cli-darwin-arm64", "bin/cli")


def test_a_brotli_compressed_build_is_decompressed_on_the_host_and_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(tmp_path, monkeypatch)
    decompressed = []

    def decompress(source: Path, target: Path) -> None:
        decompressed.append(source.read_bytes())
        target.write_bytes(PROGRAM)
        source.unlink()

    monkeypatch.setattr(_install, "_decompress", decompress)
    (archive,) = _install.native_package("@acme/cli", "1.0.0", "@acme/cli-linux-x64", "bin/cli.br")
    assert decompressed == [b"compressed"]
    assert (archive.unpack, archive.name, archive.url) == ("file", "bin/cli", None)
    assert archive.made is not None and archive.made.read_bytes() == PROGRAM
    assert archive.digest == f"sha256:{hashlib.sha256(PROGRAM).hexdigest()}"


def test_npm_resolves_a_javascript_tree_for_the_sandbox_without_fetching_or_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(_install, "node_distribution", lambda version, platform: tmp_path)

    def npm(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        prefix = Path(argv[argv.index("--prefix") + 1])
        packages = {
            "node_modules/tool": {"bin": {"tool": "./cli.js"}, "hasInstallScript": True},
            "node_modules/tool/node_modules/dep": {},
            "node_modules/native-addon": {"optional": True, "hasInstallScript": True},
            "node_modules/prebuilt-linux": {"optional": True, "os": ["linux"], "cpu": ["x64"]},
            "node_modules/prebuilt-darwin": {"optional": True, "os": ["darwin"]},
            "node_modules/not-windows": {"optional": True, "os": ["!win32"]},
            "node_modules/musl-only": {"optional": True, "libc": ["musl"]},
            "node_modules/bundled": {"inBundle": True},
            "node_modules/testing": {"dev": True},
        }
        for name, entry in packages.items():
            entry["resolved"] = f"https://registry.test/{name}.tgz"
            digest = hashlib.sha512(name.encode()).digest()
            entry["integrity"] = "sha512-" + base64.b64encode(digest).decode()
        lock = {"packages": {"": {}, **packages}}
        (prefix / "package-lock.json").write_text(json.dumps(lock))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_install.subprocess, "run", npm)
    tree = _install.npm_tree("tool", "1.0.0", LINUX)
    (argv,) = calls
    assert argv[2:4] == ["install", "tool@1.0.0"]
    assert {
        "--package-lock-only",
        "--ignore-scripts",
        "--os=linux",
        "--cpu=x64",
        "--libc=glibc",
        "--registry=https://registry.npmjs.org/",
    } <= set(argv)
    assert tree.bin == {"tool": "./cli.js"}
    # No dev, bundled or other-platform package, and no optional one with an install script.
    assert [archive.into for archive in tree.archives] == [
        "node_modules/tool",
        "node_modules/tool/node_modules/dep",
        "node_modules/prebuilt-linux",
        "node_modules/not-windows",
    ]
    assert tree.archives[0] == Archive(
        f"sha512:{hashlib.sha512(b'node_modules/tool').hexdigest()}",
        "package",
        url="https://registry.test/node_modules/tool.tgz",
        into="node_modules/tool",
    )
    # Resolved once per Host and platform.
    assert _install.npm_tree("tool", "1.0.0", LINUX) == tree and len(calls) == 1
    _install.npm_tree("tool", "1.0.0", Platform(OperatingSystem.MACOS, "aarch64"))
    assert {"--os=darwin", "--cpu=arm64"} <= set(calls[-1])
    assert not any(flag.startswith("--libc") for flag in calls[-1])


def _lock_naming(prefix: str, entries: dict[str, dict]) -> dict:
    """A lockfile as npm writes it under ``prefix``: each package named from the real
    directory, which a path through a symbolic link is not."""
    real = os.path.realpath(prefix)
    return {
        "packages": {
            "": {},
            **{
                os.path.relpath(os.path.join(real, path), prefix): entry
                for path, entry in entries.items()
            },
        }
    }


def test_npm_resolves_a_tree_in_a_cache_reached_through_a_symbolic_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "disk").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "disk")
    monkeypatch.setenv("ORBIT_CACHE_DIR", str(tmp_path / "linked" / "cache"))
    monkeypatch.setattr(_install, "node_distribution", lambda version, platform: tmp_path)
    digest = "sha512-" + base64.b64encode(hashlib.sha512(b"tool").digest()).decode()
    entry = {"bin": {"tool": "./cli.js"}, "resolved": "https://registry.test/tool.tgz"}

    def npm(argv, **kwargs):  # type: ignore[no-untyped-def]
        prefix = argv[argv.index("--prefix") + 1]
        lock = _lock_naming(prefix, {"node_modules/tool": {**entry, "integrity": digest}})
        Path(prefix, "package-lock.json").write_text(json.dumps(lock))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_install.subprocess, "run", npm)
    tree = _install.npm_tree("tool", "1.0.0", LINUX)
    assert tree.bin == {"tool": "./cli.js"}
    assert [archive.into for archive in tree.archives] == ["node_modules/tool"]


def test_a_lockfile_without_the_package_fails_and_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_install, "node_distribution", lambda version, platform: tmp_path)
    calls = []

    def npm(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        prefix = Path(argv[argv.index("--prefix") + 1])
        (prefix / "package-lock.json").write_text(json.dumps({"packages": {"": {}}}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_install.subprocess, "run", npm)
    for _ in range(2):
        with pytest.raises(AgentError, match="lockfile for tool does not name it"):
            _install.npm_tree("tool", "1.0.0", LINUX)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("platform", "member"),
    [
        (LINUX, "node-v24.15.0-linux-x64/bin/node"),
        (Platform(OperatingSystem.MACOS, "aarch64"), "node-v24.15.0-darwin-arm64/bin/node"),
        (Platform(OperatingSystem.WINDOWS, "x86_64"), "node-v24.15.0-win-x64/node.exe"),
    ],
)
def test_node_is_nodejs_orgs_pinned_build_of_its_one_executable(
    platform: Platform, member: str
) -> None:
    archive = _install.node_runtime("24.15.0", platform)
    assert archive.digest == f"sha256:{_install.NODE['24.15.0'][platform.npm]}"
    assert archive.url is not None and archive.url.startswith("https://nodejs.org/dist/v24.15.0/")
    assert archive.members == ((member, member.rsplit("/", 1)[1], None),)
    with pytest.raises(AgentUnsupportedError, match=r"no Node\.js 1\.0\.0"):
        _install.node_runtime("1.0.0", platform)


def test_node_is_pinned_for_every_platform_a_sandbox_or_host_runs() -> None:
    for platform in ("linux-x64", "darwin-arm64", "win32-x64", "linux-arm64", "darwin-x64"):
        assert len(_install.NODE[_install.HOST_NODE][platform]) == 64


# --- the Sandbox --------------------------------------------------------------------


class Sandbox:
    """Records what an install does; a program runs once something was unpacked."""

    def __init__(
        self,
        system: OperatingSystem = OperatingSystem.LINUX,
        *,
        machine: str = "x86_64",
        cpuinfo: str = "flags : sse avx avx2",
        version: str = "0.146.0",
        installed: bool = False,
        fetches: bool = False,
    ) -> None:
        self.request = SimpleNamespace(os=system)
        self.fetches_files = fetches
        self.machine, self.cpuinfo, self.version = machine, cpuinfo, version
        self.installed = installed
        self.commands: list[tuple[list[str], Identity, dict]] = []
        self.uploads: list[tuple[list[str], str, Identity]] = []
        self.files: dict[str, bytes] = {}
        self.home = "C:/Users/agent" if system is OperatingSystem.WINDOWS else "/home/user"

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        argv = [str(part) for part in argv]
        identity = kwargs.get("identity", Identity.FRAMEWORK)
        self.commands.append((argv, identity, kwargs.get("env") or {}))
        if argv[-1] == 'printf %s "$HOME"' or "Path.home()" in argv[-1]:
            return ExecResult(exit_code=0, stdout=self.home)
        if argv[-1].endswith("uname -m"):
            return ExecResult(exit_code=0, stdout=f"{self.machine}\n")
        if argv[-1].endswith("cat /proc/cpuinfo"):
            return ExecResult(exit_code=0, stdout=self.cpuinfo)
        if argv[-1].endswith("--version"):
            ready = self.installed or self.unpacked
            return ExecResult(exit_code=0 if ready else 127, stdout=f"cli {self.version}\n")
        return ExecResult(exit_code=0)

    @property
    def unpacked(self) -> list[tuple[list[str], Identity, dict]]:
        return [command for command in self.commands if "unpack.py" in " ".join(command[0])]

    def manifest(self) -> dict:
        (path,) = [name for name in self.files if name.endswith("/manifest.json")]
        return json.loads(self.files[path])

    async def write_file(self, path, data, **kwargs):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def upload_dir(self, source, target, **kwargs):  # type: ignore[no-untyped-def]
        names = sorted(path.name for path in Path(source).iterdir())
        self.uploads.append((names, str(target), kwargs["identity"]))


@pytest.fixture
def codex_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    archive = tmp_path / "codex.tgz"
    archive.write_bytes(b"codex package")
    resolved: list[tuple] = []

    def native_package(*args: str) -> list[Archive]:
        resolved.append(args)
        return [Archive("sha512:abc", "package", url="https://registry.test/codex.tgz")]

    monkeypatch.setattr(_install, "native_package", native_package)
    monkeypatch.setattr(_install, "_downloaded", lambda archive_: archive)
    return resolved


CODEX = "codex/0.146.0/vendor/x86_64-unknown-linux-musl/bin/codex"


async def test_the_host_uploads_a_native_program_and_the_agent_unpacks_it(
    codex_archives: list[tuple],
) -> None:
    sandbox = Sandbox()
    assert await CodexCliHarness().install(sandbox) == "0.146.0"  # type: ignore[arg-type]
    private = "/home/user/.local/share/orbit"
    assert codex_archives == [("@openai/codex", "0.146.0", "@openai/codex-linux-x64", CODEX[14:])]
    assert sandbox.uploads == [(["0"], f"{private}/.orbit-unpack", Identity.AGENT)]
    assert sandbox.manifest() == {
        "archives": [
            {
                "digest": "sha512:abc",
                "unpack": "package",
                "into": "codex/0.146.0",
                "members": [],
                "name": "",
            }
        ],
        "executables": [CODEX],
    }
    # As the agent, on the framework's Python, which the agent may run: the image needs none.
    ((argv, identity, env),) = sandbox.unpacked
    assert argv[:3] == [guestd.PYTHON["linux"], "-I", f"{private}/.orbit-unpack/unpack.py"]
    assert argv[4] == private and identity is Identity.AGENT and env == {}
    # The agent makes its own directories, which guestd would make as root.
    made = [argv[-1] for argv, _, _ in sandbox.commands if argv[0] == "mkdir"]
    assert made == [f"{private}/.orbit-unpack", "/home/user/.local/bin"]
    launcher = f'#!/bin/sh\nexec {private}/{CODEX} "$@"\n'
    assert sandbox.files["/home/user/.local/bin/codex"] == launcher.encode()


async def test_a_sandbox_that_fetches_files_downloads_the_archives_itself(
    codex_archives: list[tuple],
) -> None:
    sandbox = Sandbox(fetches=True)
    await CodexCliHarness().install(sandbox)  # type: ignore[arg-type]
    assert sandbox.uploads == []
    (archive,) = sandbox.manifest()["archives"]
    assert archive["url"] == "https://registry.test/codex.tgz"
    # With the Host's CA certificates, which a Task image may lack.
    staging = "/home/user/.local/share/orbit/.orbit-unpack"
    assert b"BEGIN CERTIFICATE" in sandbox.files[f"{staging}/ca.pem"]
    ((_, _, env),) = sandbox.unpacked
    assert env == {"SSL_CERT_FILE": f"{staging}/ca.pem"}


async def test_a_program_already_in_place_is_used_as_it_is(codex_archives: list[tuple]) -> None:
    sandbox = Sandbox(installed=True)
    assert await CodexCliHarness().install(sandbox) == "0.146.0"  # type: ignore[arg-type]
    assert codex_archives == [] and sandbox.uploads == [] and not sandbox.unpacked


async def test_a_program_that_reports_another_version_fails_the_install(
    codex_archives: list[tuple],
) -> None:
    with pytest.raises(AgentError, match=r"does not name 0\.146\.0: 'cli 0\.145\.0'"):
        await CodexCliHarness().install(Sandbox(version="0.145.0"))  # type: ignore[arg-type]


async def test_a_windows_program_unpacks_on_the_framework_python_behind_a_git_bash_launcher(
    codex_archives: list[tuple],
) -> None:
    sandbox = Sandbox(OperatingSystem.WINDOWS)
    await CodexCliHarness().install(sandbox)  # type: ignore[arg-type]
    assert codex_archives[0][2:] == (
        "@openai/codex-win32-x64",
        "vendor/x86_64-pc-windows-msvc/bin/codex.exe",
    )
    ((argv, _, _),) = sandbox.unpacked
    # Named with forward slashes, as Git Bash and the CLIs' configuration take it.
    assert argv[0] == "C:/ProgramData/Orbit/python/python.exe"
    assert not [argv for argv, _, _ in sandbox.commands if argv[0] in ("chmod", "mkdir")]
    assert sandbox.files["C:/Users/agent/.local/bin/codex"] == (
        b"#!/bin/sh\nexec C:/Users/agent/.local/share/orbit/codex/0.146.0/"
        b'vendor/x86_64-pc-windows-msvc/bin/codex.exe "$@"\n'
    )


@pytest.mark.parametrize(
    ("system", "cpuinfo", "build"),
    [
        (OperatingSystem.LINUX, "flags : avx avx2", "@factory/cli-linux-x64"),
        (OperatingSystem.LINUX, "flags : avx", "@factory/cli-linux-x64-baseline"),
        (OperatingSystem.WINDOWS, "flags : avx", "@factory/cli-win32-x64-baseline"),
    ],
)
async def test_an_x86_64_cpu_without_avx2_gets_the_baseline_build(
    codex_archives: list[tuple], system: OperatingSystem, cpuinfo: str, build: str
) -> None:
    sandbox = Sandbox(system, cpuinfo=cpuinfo, version="0.249.0")
    await DroidHarness().install(sandbox)  # type: ignore[arg-type]
    assert codex_archives[0][2] == build


async def test_a_platform_without_a_build_is_refused(codex_archives: list[tuple]) -> None:
    with pytest.raises(AgentUnsupportedError, match="no build for linux-arm64"):
        await CodexCliHarness().install(Sandbox(machine="aarch64"))  # type: ignore[arg-type]
    with pytest.raises(AgentUnsupportedError, match="linux/riscv64"):
        await CodexCliHarness().install(Sandbox(machine="riscv64"))  # type: ignore[arg-type]


async def test_a_javascript_program_runs_on_its_own_node_through_its_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = "node_modules/@earendil-works/pi-coding-agent"
    tree = Tree(
        (Archive("sha512:aa", "package", url="https://registry.test/pi.tgz", into=package),),
        {"pi": "dist/bundle/cli.js"},
    )
    monkeypatch.setattr(_install, "npm_tree", lambda *args: tree)
    archive = tmp_path / "archive"
    archive.write_bytes(b"x")
    monkeypatch.setattr(_install, "_downloaded", lambda archive_: archive)
    sandbox = Sandbox(version="1.4.0")
    assert await PiCliHarness().install(sandbox) == "1.4.0"  # type: ignore[arg-type]
    manifest = sandbox.manifest()
    assert [entry["into"] for entry in manifest["archives"]] == [
        "node/24.15.0",
        f"pi/1.4.0/{package}",
    ]
    member = ["node-v24.15.0-linux-x64/bin/node", "node", None]
    assert manifest["archives"][0]["members"] == [member]
    assert manifest["executables"] == ["node/24.15.0/node"]
    private = "/home/user/.local/share/orbit"
    entry = f"{private}/pi/1.4.0/{package}/dist/bundle/cli.js"
    launcher = f'#!/bin/sh\nexec {private}/node/24.15.0/node {entry} "$@"\n'
    assert sandbox.files["/home/user/.local/bin/pi"] == launcher.encode()


async def test_the_agent_judge_gets_its_own_copy_as_the_framework(
    codex_archives: list[tuple],
) -> None:
    sandbox = Sandbox()
    executable = await install_agent_judge(
        sandbox,  # type: ignore[arg-type]
        "codex-cli",
        "0.146.0",
        "/opt/orbit/judge",
        fetch=False,
    )
    assert executable == f"/opt/orbit/judge/{CODEX}"
    assert sandbox.uploads == [(["0"], "/opt/orbit/judge/.orbit-unpack", Identity.FRAMEWORK)]
    ((argv, identity, _),) = sandbox.unpacked
    assert argv[0] == guestd.PYTHON["linux"] and identity is Identity.FRAMEWORK
    assert "/home/user/.local/bin/codex" not in sandbox.files  # orbit_verify runs it itself
    # A copy of its own already there, as on the second stage of one verifier, is used.
    codex_archives.clear()
    installed = Sandbox(installed=True)
    await install_agent_judge(
        installed,  # type: ignore[arg-type]
        "codex-cli",
        "0.146.0",
        "/opt/orbit/judge",
        fetch=True,
    )
    assert codex_archives == [] and not installed.unpacked


async def test_a_verifier_stage_gets_the_judge_program_only_when_it_calls_the_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = []

    async def install(sandbox, adapter: str, version: str, directory: str, *, fetch: bool) -> str:  # type: ignore[no-untyped-def]
        installed.append((adapter, version, directory, fetch))
        return f"{directory}/codex/{version}/codex"

    monkeypatch.setattr(episode_protocol, "install_agent_judge", install)
    judge = AgentJudgeConfig(
        provider="openai",
        adapter="codex-cli",
        version="0.146.0",
        model="judge-model",
        reasoning_effort="high",
    )
    ctx = SimpleNamespace(
        verification_config=VerificationConfig(agent=judge),
        judge_tokens={"agent": "episode-token"},
        spec=SimpleNamespace(os=OperatingSystem.LINUX),
    )
    sandbox = Sandbox()
    sandbox.gateway_url = "http://gateway"  # type: ignore[attr-defined]
    protocol = episode_protocol.EpisodeProtocol(OracleHarness())
    plain, calling = tmp_path / "plain", tmp_path / "calling"
    for stage, source in ((plain, "print('ok')\n"), (calling, CALLS_THE_JUDGE)):
        stage.mkdir()
        (stage / "verify.py").write_text(source)

    def staged() -> dict:
        return json.loads(sandbox.files["/opt/orbit/tests/config.json"])["agent"]

    stage = protocol._stage_verification_config
    await stage(ctx, sandbox, Stage.TESTS, plain, fetch=False)  # type: ignore[arg-type]
    assert installed == [] and "executable" not in staged()
    await stage(ctx, sandbox, Stage.TESTS, calling, fetch=True)  # type: ignore[arg-type]
    assert installed == [("codex-cli", "0.146.0", "/opt/orbit/judge", True)]
    assert staged()["executable"] == "/opt/orbit/judge/codex/0.146.0/codex"

    async def unreachable(*args: object, **kwargs: object) -> str:
        raise AgentError("could not read @openai/codex@0.146.0 from the registry")

    # The Judge's program is the verification's to provide, never the agent's fault.
    monkeypatch.setattr(episode_protocol, "install_agent_judge", unreachable)
    with pytest.raises(VerificationInfrastructureError, match="Agent Judge's codex-cli"):
        await stage(ctx, sandbox, Stage.TESTS, calling, fetch=False)  # type: ignore[arg-type]


async def test_a_release_binary_goes_into_the_agent_bin_or_where_its_program_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = {
        (OperatingSystem.LINUX, "x86_64"): ReleaseAsset("https://example.test/x86_64", "a" * 64),
        (OperatingSystem.LINUX, "aarch64"): ReleaseAsset(
            "https://example.test/aarch64.tgz", "b" * 64, member="cli", member_sha256="c" * 64
        ),
    }
    archive = tmp_path / "archive"
    archive.write_bytes(b"x")
    monkeypatch.setattr(_install, "_downloaded", lambda archive_: archive)

    async def install(sandbox: Sandbox, **kwargs) -> str:  # type: ignore[no-untyped-def]
        return await _install.install_release(
            sandbox,  # type: ignore[arg-type]
            "/home/user",
            binary="fixture",
            version="2.0.0",
            assets=assets,
            platform=await _install.sandbox_platform(sandbox, "/home/user"),  # type: ignore[arg-type]
            **kwargs,
        )

    sandbox = Sandbox(machine="arm64", version="2.0.0")
    assert await install(sandbox) == "2.0.0"
    assert sandbox.manifest() == {
        "archives": [
            {
                "digest": "sha256:" + "b" * 64,
                "unpack": "members",
                "into": "",
                "members": [["cli", "fixture", "c" * 64]],
                "name": "",
            }
        ],
        "executables": ["fixture"],
    }
    assert sandbox.uploads[0][1] == "/home/user/.local/bin/.orbit-unpack"
    sandbox = Sandbox(version="2.0.0")
    await install(sandbox, directory="/home/user/.cache/program/bin")
    assert sandbox.manifest()["archives"][0]["unpack"] == "file"
    assert sandbox.uploads[0][1] == "/home/user/.cache/program/bin/.orbit-unpack"
    # One already in place that names the version is used as it is.
    sandbox = Sandbox(version="2.0.0", installed=True)
    assert await install(sandbox) == "2.0.0" and not sandbox.unpacked
    with pytest.raises(AgentUnsupportedError, match="no official release for windows/x86_64"):
        await install(Sandbox(OperatingSystem.WINDOWS, version="2.0.0"))
