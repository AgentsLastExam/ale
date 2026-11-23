from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from orbit.core.capability import CapabilityProfile, SandboxNeeds
from orbit.core.config import RunConfig, SandboxConfig, SlurmProviderConfig
from orbit.core.errors import ProviderStartError
from orbit.core.harness import (
    AgentRun,
    AutonomousHarness,
    HarnessSession,
    NativeContinuation,
    ResumeSupport,
    TrajectoryParseContext,
)
from orbit.core.sandbox import (
    ExecResult,
    Identity,
    ImageRef,
    PreparedTaskImage,
    Provider,
    Sandbox,
    SandboxRequest,
    SandboxRole,
)
from orbit.core.taskspec import (
    SCHEMA_VERSION,
    ContainerFeature,
    ImageKind,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
)
from orbit.core.trace import ExecutionLog
from orbit.core.trajectory import AtifAgent, AtifTrajectory, TrajectoryBuilder
from orbit.run import orbit_image
from orbit.run.harnesses.base import CliHarness
from orbit.run.providers import ProviderRegistry, machine_arch
from orbit.run.scaffold import scaffold_task
from orbit.run.tasksets.manifest import ManifestTask, load_tasks

ENGINE_ROOT = Path(__file__).resolve().parents[1]
#: The current Orbit bases, for images a test builds or runs.
CONTAINER_BASE = orbit_image("container-ubuntu22-base")
VM_BASE = orbit_image("vm-ubuntu24-base")
#: What a Linux VM Task asks for. The VM base boots a GNOME desktop, which in a container
#: Task's 512 MB spends its time paging and can miss a phase deadline.
VM_RESOURCES: dict[str, Any] = {"cpus": 2, "memory_mb": 4096}


def skip_without_kata() -> None:
    """Skip unless this host starts Kata microVMs: KVM, and Kata's shim on Docker's PATH.

    The shim is looked up on this process's PATH, which holds the same system directories
    as the daemon's on a host that runs ``orbit`` beside its Docker daemon.
    """
    if not Path("/dev/kvm").exists():
        pytest.skip("/dev/kvm is missing")
    if shutil.which("containerd-shim-kata-v2") is None:
        pytest.skip("Kata Containers is not installed (containerd-shim-kata-v2 not on PATH)")


def skip_without_gvisor() -> None:
    """Skip unless Docker here registers Orbit's build of gVisor's runtime, as ``runsc``,
    ``runsc-net-raw`` and ``runsc-nested``
    ([Providers](../packages/orbit-run/src/orbit/run/providers/README.md))."""
    from orbit.run.providers.docker import gvisor

    try:
        shown = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"docker is not reachable: {error}")
    runtimes = json.loads(shown.stdout or "{}")
    missing = set(gvisor.ARGUMENTS) - set(runtimes)
    if missing:
        pytest.skip(f"gVisor is not registered with Docker: {', '.join(sorted(missing))}")
    path = runtimes[gvisor.RUNTIME].get("path", gvisor.RUNTIME)
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"{path} does not run here: {error}")
    if gvisor.BUILD not in version.stdout:
        pytest.skip(f"{path} is not Orbit's build {gvisor.BUILD}: {version.stdout.strip()}")


def slurm_config() -> SlurmProviderConfig:
    """The cluster ``$ORBIT_TEST_SLURM_CONFIG`` names: a Run TOML with ``sandbox.slurm``
    settings, whose partitions' login hosts share the account's home with their nodes."""
    path = os.environ.get("ORBIT_TEST_SLURM_CONFIG")
    if not path:
        pytest.skip("set ORBIT_TEST_SLURM_CONFIG to a Run TOML with sandbox.slurm settings")
    settings = tomllib.loads(Path(path).read_text())["sandbox"]["slurm"]
    return SlurmProviderConfig.model_validate(settings)


def sibling_checkout(name: str) -> Path:
    """Locate a sibling repository from main or any linked worktree."""
    return next(
        (parent / name for parent in ENGINE_ROOT.parents if (parent / name).is_dir()),
        ENGINE_ROOT.parent / name,
    )


def provider_registry(*providers: Provider, only: bool = False) -> ProviderRegistry:
    """A Run's registry, with ``providers`` standing for the Providers their ids name, first
    in ``sandbox.providers`` and in that order, then the default list's others unless
    ``only``."""
    if not providers:
        return ProviderRegistry(RunConfig())
    from orbit.run.providers.slurm import SlurmProvider

    ids = [provider.id for provider in providers]
    order = ids + ([] if only else [name for name in SandboxConfig().providers if name not in ids])
    sandbox: dict[str, Any] = {"providers": order}
    for provider in providers:
        if isinstance(provider, SlurmProvider):
            # A Run that names Slurm configures its partitions.
            sandbox["slurm"] = provider.config.model_dump(mode="json")
    registry = ProviderRegistry(RunConfig.model_validate({"sandbox": sandbox}))
    for provider in providers:
        registry._instances[provider.id] = provider
    return registry


def fleet_registry(provider: Provider, client: Any, url: str) -> ProviderRegistry:
    """A Run's registry whose one Provider is ``provider``, the Fleet at ``url`` that
    ``client`` reaches."""
    registry = ProviderRegistry(
        RunConfig.model_validate({"sandbox": {"providers": ["fleet"], "fleet": {"url": url}}})
    )
    registry._instances["fleet"] = provider
    registry._fleet = client
    return registry


async def prepare_reference(
    provider: Provider,
    reference: str,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> PreparedTaskImage:
    return await provider.prepare_image(ImageRef(kind=kind, reference=reference))


#: Each list admits example.com and refuses www.iana.org, so one check serves both.
LIST_POLICIES = [
    pytest.param(
        NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)), id="allowlist"
    ),
    pytest.param(
        NetworkPolicy(mode=NetworkMode.BLOCKLIST, blocked_hosts=("*.iana.org",)), id="blocklist"
    ),
]


async def check_a_sealed_list(
    provider: Provider,
    request: Callable[..., Any],
    policy: NetworkPolicy,
) -> None:
    """Sealed under ``policy``, one of :data:`LIST_POLICIES`, a Linux agent reaches
    example.com through guestd's relay to the Host's egress proxy, is refused www.iana.org,
    and reaches nothing directly. ``request`` is the suite's request factory."""
    from orbit.run.gateway.proxy import EgressProxy
    from orbit.run.gateway.session import GatewaySession, SessionRegistry

    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(episode_id="list", model="", purpose="egress", policy=policy)
    )
    proxy = EgressProxy(sessions, host="0.0.0.0")
    await proxy.start()
    curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "30"]
    try:
        sandbox = await provider.create(
            await request(network=policy, proxy_url=proxy.url, proxy_token=session.token)
        )
        async with sandbox:
            await sandbox.close_egress()
            fetched = [
                await sandbox.exec([*curl, *argv], identity=Identity.AGENT, timeout_sec=60)
                for argv in (
                    ["https://example.com/"],
                    ["https://www.iana.org/"],
                    ["--noproxy", "*", "https://example.com/"],
                )
            ]
    finally:
        await proxy.stop()
    admitted, refused, direct = fetched
    assert admitted.ok and admitted.stdout == "200", admitted.stdout + admitted.stderr
    assert refused.exit_code == 56 and "403" in refused.stderr, refused.stderr
    assert not direct.ok, direct.stdout


#: The smallest valid ``task.toml``: a container Task with no network.
MINIMAL_TASK_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "task": {"name": "agentslastexam/demo"},
    "environment": {"cpus": 1, "memory_mb": 512},
    "agent": {"user": "user", "timeout_sec": 120, "network_mode": "no-network"},
    "verifier": {"timeout_sec": 120},
}
_EMPTY_STAGE = "#!/usr/bin/env bash\nset -euo pipefail\n"


def task_toml(config: Mapping[str, Any]) -> str:
    """Render ``config`` as TOML: scalars and arrays first, then each table under a header."""

    def value(item: Any) -> str:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int | float):
            return repr(item)
        if isinstance(item, str):
            return json.dumps(item)
        if isinstance(item, Mapping):
            return "{" + ", ".join(f"{key} = {value(entry)}" for key, entry in item.items()) + "}"
        return "[" + ", ".join(value(entry) for entry in item) + "]"

    def table(prefix: tuple[str, ...], body: Mapping[str, Any]) -> list[str]:
        lines = [f"[{'.'.join(prefix)}]"] if prefix else []
        lines += [
            f"{key} = {value(entry)}"
            for key, entry in body.items()
            if entry is not None and not isinstance(entry, Mapping)
        ]
        for key, entry in body.items():
            if isinstance(entry, Mapping):
                lines += ["", *table((*prefix, key), entry)]
        return lines

    return "\n".join(table((), config)) + "\n"


def merged_config(*layers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge ``task.toml`` layers; a ``None`` value removes the key."""
    result: dict[str, Any] = {}
    for layer in layers:
        for key, entry in (layer or {}).items():
            if entry is None:
                result.pop(key, None)
            elif isinstance(entry, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = merged_config(result[key], entry)
            else:
                result[key] = copy.deepcopy(entry)
    return result


def write_task(
    task: Path,
    *,
    name: str = "agentslastexam/demo",
    config: Mapping[str, Any] | None = None,
    dockerfile: str | None = f"FROM {CONTAINER_BASE}\n",
    instruction: str = "Do the task.\n",
    test: str | None = _EMPTY_STAGE,
    solve: str | None = _EMPTY_STAGE,
) -> Path:
    """Write one complete Task folder: the minimal configuration with ``config`` merged in.

    ``dockerfile`` is ``environment/Dockerfile``; ``None`` writes none.
    """
    full = merged_config(MINIMAL_TASK_CONFIG, {"task": {"name": name}}, config)
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(task_toml(full), encoding="utf-8")
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    if dockerfile is not None:
        target = task / "environment" / "Dockerfile"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dockerfile, encoding="utf-8")
    stages = {"tests/test.sh": test, "solution/solve.sh": solve}
    for relative, body in stages.items():
        if body is not None:
            entry = task / relative
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(body, encoding="utf-8")
            entry.chmod(0o755)
    return task


def edit_task_config(task: Path, layer: Mapping[str, Any]) -> None:
    """Merge ``layer`` into a Task folder's ``task.toml``; a ``None`` value removes a key."""
    path = task / "task.toml"
    path.write_text(task_toml(merged_config(tomllib.loads(path.read_text()), layer)))


#: The pinned Tahoe base the macOS fixture Tasks use; it signs ``admin`` in.
MACOS_BASE = (
    "ghcr.io/cirruslabs/macos-tahoe-base@sha256:"
    "4aaa751fa7538902d73c767f27a066fc87e83f183069da4e4b5022863abd4c77"
)
#: ``environment/build.sh`` of a macOS image with the runtimes the Orbit bases provide CLIs:
#: the Node.js they pin, and the base's own Git and Python.
MACOS_RUNTIME_BUILD = """\
set -euo pipefail
node=node-v24.15.0-darwin-arm64
curl -fsSL --retry 3 -o "/tmp/$node.tar.gz" "https://nodejs.org/dist/v24.15.0/$node.tar.gz"
echo "372331b969779ab5d15b949884fc6eaf88d5afe87bde8ba881d6400b9100ffc4  /tmp/$node.tar.gz" |
    shasum -a 256 -c -
tar -xzf "/tmp/$node.tar.gz" -C /usr/local --strip-components 1 \\
    "$node/bin" "$node/include" "$node/lib" "$node/share"
rm "/tmp/$node.tar.gz"
/usr/local/bin/node --version
git --version
python3 --version
"""


def cli_os() -> OperatingSystem:
    """The system CLI integration tests run their programs on.

    Linux, in a container of the current base; or macOS with ``ORBIT_TEST_CLI_OS=macos`` on
    a Mac whose local Orchard ``scripts/setup_macos.py`` keeps running.
    """
    return OperatingSystem(os.environ.get("ORBIT_TEST_CLI_OS", "linux"))


@asynccontextmanager
async def cli_sandbox(
    episode_id: str, gateway_url: str, work: Path
) -> AsyncIterator[tuple[Sandbox, str]]:
    """A Sandbox an agent CLI installs into and runs in, and the agent's home there.

    On macOS it is a VM of an image built once, as a Task's ``build.sh`` would, on
    ``MACOS_BASE``; the agent is ``admin``, which a macOS Task may run as with sudo.
    """
    registry = ProviderRegistry(RunConfig())
    if cli_os() is OperatingSystem.MACOS:
        from orbit.run.task_images import prepare_task_image
        from orbit.run.tasksets.manifest import load_tasks

        folder = work / "cli-runtime"
        write_task(
            folder,
            name="agentslastexam/cli-runtime",
            config={
                "environment": {"os": "macos", "image": MACOS_BASE},
                "agent": {"user": "admin", "sudo": True, "network_mode": "public"},
            },
            dockerfile=None,
        )
        (folder / "environment").mkdir()
        (folder / "environment" / "build.sh").write_text(MACOS_RUNTIME_BUILD)
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        provider = registry.get("orchard")
        image = await prepare_task_image(load_tasks(folder)[0], provider)
        request = SandboxRequest(
            episode_id=episode_id,
            os=OperatingSystem.MACOS,
            prepared_image=image,
            resources=Resources(cpus=2, memory_mb=4096),
            network=NetworkPolicy(mode=NetworkMode.OPEN),
            gateway_url=gateway_url,
            user="admin",
            sudo=True,
        )
    else:
        provider = registry.get("docker:runc")
        request = SandboxRequest(
            episode_id=episode_id,
            prepared_image=await prepare_reference(provider, CONTAINER_BASE),
            resources=Resources(cpus=2, memory_mb=4096),
            network=NetworkPolicy(),
            gateway_url=gateway_url,
        )
    sandbox = await provider.create(request)
    try:
        yield sandbox, sandbox.agent_home
    finally:
        await sandbox.destroy()


def write_qcow2(path: Path, *, backing: str | None = None, data: bytes = b"") -> Path:
    """A file ``FakeQemuImg`` reads as a qcow2: a header naming ``backing``, then ``data``."""
    name = (backing or "").encode()
    header = b"QFI\xfb" + (3).to_bytes(4, "big")
    header += (32 if name else 0).to_bytes(8, "big") + len(name).to_bytes(4, "big")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header.ljust(32, b"\0") + name + data)
    return path


class FakeQemuImg:
    """``qemu-img`` for unit tests, on files ``write_qcow2`` made: ``create``, ``info``
    (with ``--backing-chain``), ``check``, ``rebase`` and ``convert``. A rebase or convert
    keeps the file's data, so a test sees what was moved, not merged."""

    VIRTUAL_SIZE = 64 << 30

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        #: Disks whose clusters ``check --output=json`` reports as compressed.
        self.compressed: set[Path] = set()

    async def __call__(self, *argv: str, **_kwargs: object) -> tuple[int, str, str]:
        self.calls.append(argv)
        assert argv[0] == "qemu-img", argv
        options, positional = self._parse(argv[2:])
        command = argv[1]
        try:
            if command == "check":
                disk = Path(positional[0])
                if self.backing(disk) is False:
                    return 1, "", ""
                report = {"compressed-clusters": 1} if disk in self.compressed else {}
                return 0, json.dumps(report) if "--output=json" in options else "", ""
            if command == "info":
                return 0, json.dumps(self._info(Path(positional[0]), options)), ""
            if command == "create":
                write_qcow2(Path(positional[0]), backing=options.get("-b"))
                return 0, "", ""
            if command == "rebase":
                disk = Path(positional[0])
                write_qcow2(disk, backing=options["-b"], data=self._data(disk))
                return 0, "", ""
            if command == "convert":
                source, target = Path(positional[0]), Path(positional[1])
                write_qcow2(target, backing=options.get("-B"), data=self._data(source))
                return 0, "", ""
        except FileNotFoundError as missing:
            return 1, "", f"qemu-img: Could not open '{missing.filename}'"
        raise AssertionError(f"unexpected qemu-img call: {argv}")

    @staticmethod
    def backing(disk: Path) -> str | bool | None:
        """The backing name in ``disk``'s header; False when it is no qcow2."""
        try:
            raw = disk.read_bytes()
        except OSError:
            return False
        if raw[:4] != b"QFI\xfb":
            return False
        offset, size = int.from_bytes(raw[8:16], "big"), int.from_bytes(raw[16:20], "big")
        return raw[offset : offset + size].decode() if offset else None

    def _info(self, disk: Path, options: dict[str, str]) -> object:
        chain: list[dict[str, object]] = []
        current: Path | None = disk
        while current is not None:
            backing = self.backing(current)
            if backing is False:
                raise FileNotFoundError(2, "No such file", str(current))
            entry: dict[str, object] = {
                "filename": str(current),
                "format": "qcow2",
                "virtual-size": self.VIRTUAL_SIZE,
            }
            parent, current = current.parent, None
            if isinstance(backing, str):
                full = Path(backing) if backing.startswith("/") else parent / backing
                entry |= {"backing-filename": backing, "full-backing-filename": str(full)}
                current = full
            chain.append(entry)
            if "--backing-chain" not in options:
                break
        return chain if "--backing-chain" in options else chain[0]

    @staticmethod
    def _data(disk: Path) -> bytes:
        raw = disk.read_bytes()
        offset, size = int.from_bytes(raw[8:16], "big"), int.from_bytes(raw[16:20], "big")
        return raw[max(32, offset + size) :]

    @staticmethod
    def _parse(argv: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
        options: dict[str, str] = {}
        positional: list[str] = []
        arguments = iter(argv)
        for argument in arguments:
            if argument in {"-f", "-F", "-b", "-B", "-O", "-o", "-m"}:
                options[argument] = next(arguments)
            elif argument.startswith("-"):
                options[argument] = ""
            else:
                positional.append(argument)
        return options, positional


class MemorySandbox(Sandbox):
    """A Sandbox held in memory: commands echo their arguments, files live in a dict."""

    def __init__(self, provider: MemoryProvider, request: SandboxRequest) -> None:
        from orbit.core.sandbox import ResolvedImage, ResourceAllocation

        image = request.prepared_image
        super().__init__(
            sandbox_id=request.episode_id,
            request=request,
            resolved_image=ResolvedImage(
                kind=image.kind,
                prepared_identity=image.prepared_identity,
                observed_identity=image.prepared_identity,
                observed_ref=image.runtime_ref,
            ),
            allocation=ResourceAllocation(
                cpus=request.resources.cpus,
                memory_mb=request.resources.memory_mb,
                sudo=request.sudo,
                network_mode=request.network.mode,
                provider=provider.id,
                leaf=provider.id,
            ),
            agent_user=request.user,
            agent_home=f"/home/{request.user}",
        )
        self.provider = provider
        self.files: dict[str, bytes] = {}
        self.uploads: dict[str, dict[str, str]] = {}
        self.egress_open = True
        self.destroyed = False

    async def exec(self, argv, *, cwd=None, env=None, timeout_sec=None, identity=None,
                   output_sink=None):  # type: ignore[no-untyped-def]  # fmt: skip
        if argv[0] == "sleep":
            await asyncio.sleep(float(argv[1]))
        if argv[:2] == ["python3", "-c"] and argv[3:4] == ["inspect"]:
            # The artifact copier: the declared file, when the Sandbox holds it.
            found = argv[4] in self.files
            entries = [{"source": argv[4], "kind": "file", "mode": 0o644}] if found else []
            report = {"entries": entries, "skipped": [], "truncated": False}
            return ExecResult(exit_code=0, stdout=json.dumps(report))
        if argv[:2] == ["python3", "-c"] and argv[3:4] == ["read"]:
            await output_sink("stdout", self.files[argv[4]])
            return ExecResult(exit_code=0)
        if argv[:3] == ["rm", "-rf", "--"]:
            for path in argv[3:]:
                for name in [
                    name for name in self.files if name == path or name.startswith(path + "/")
                ]:
                    del self.files[name]
        text = " ".join(argv)
        if output_sink is not None:
            for word in argv:
                await output_sink("stdout", f"{word}\n".encode())
        return ExecResult(exit_code=0, stdout=text, stderr=str(identity))

    async def write_file(self, path, data, *, identity=None):  # type: ignore[no-untyped-def]
        self.files[str(path)] = data

    async def read_file(self, path, *, offset=0, length=None):  # type: ignore[no-untyped-def]
        data = self.files[str(path)]
        return data[offset:] if length is None else data[offset : offset + length]

    async def list_files(self, path):  # type: ignore[no-untyped-def]
        from orbit.core.sandbox import SandboxFile

        return tuple(
            SandboxFile(path=name, size=len(data))
            for name, data in sorted(self.files.items())
            if name.startswith(str(path))
        )

    async def file_digest(self, path, *, length):  # type: ignore[no-untyped-def]
        import hashlib

        return "sha256:" + hashlib.sha256(self.files[str(path)][:length]).hexdigest()

    async def open_egress(self) -> None:
        self.egress_open = True

    async def close_egress(self) -> None:
        self.egress_open = False

    async def upload_dir(self, source, target, *, identity=None):  # type: ignore[no-untyped-def]
        root = Path(source)
        self.uploads[target] = {
            str(entry.relative_to(root)): (
                f"-> {os.readlink(entry)}"
                if entry.is_symlink()
                else entry.read_bytes().decode(errors="replace")
            )
            for entry in sorted(root.rglob("*"))
            if entry.is_symlink() or entry.is_file()
        }

    async def screenshot(self) -> bytes:
        return b"\x89PNG fake"

    @property
    def handle(self) -> str:
        return f"memory:{self.sandbox_id}"

    async def retain(self, *, roles, reason):  # type: ignore[no-untyped-def]
        from orbit.core.sandbox import RetainedSandbox

        self.provider.kept[self.handle] = self
        return RetainedSandbox(
            provider=self.provider.name,
            handle=self.handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"orbit sandbox destroy {self.handle}",
        )

    async def snapshot(self, *, roles, reason):  # type: ignore[no-untyped-def]
        from orbit.core.sandbox import RetainedSandbox

        # A Sandbox's first snapshot is named by its id, each later one by a number too.
        handle = f"memory:snapshot:{self.sandbox_id}"
        taken = sum(name.startswith(handle) for name in self.provider.saved)
        handle = f"{handle}-{taken}" if taken else handle
        self.provider.saved.append(handle)
        self.provider.snapshots[handle] = dict(self.files)
        return RetainedSandbox(
            provider=self.provider.name,
            handle=handle,
            episode_id=self.request.episode_id,
            roles=roles,
            reason=reason,
            cleanup_command=f"orbit sandbox destroy {handle}",
        )

    async def destroy(self) -> None:
        if not self.destroyed:
            self.destroyed = True
            self.provider.destroyed.append(self.sandbox_id)


class MemoryProvider(Provider):
    """A Provider of :class:`MemorySandbox`, recording what it was asked for."""

    def __init__(
        self,
        name: str = "docker",
        *,
        create_delay: float = 0.0,
        saves_snapshots: bool = False,
        container_features: frozenset[ContainerFeature] = frozenset(),
    ) -> None:
        self.name = name
        self.create_delay = create_delay
        self.saves_snapshots = saves_snapshots
        self.container_features = container_features
        self.requests: list[SandboxRequest] = []
        self.sandboxes: list[MemorySandbox] = []
        self.destroyed: list[str] = []
        self.reattached: list[str] = []
        self.discarded: list[str] = []
        #: Retained Sandboxes and snapshots (their files), by handle.
        self.kept: dict[str, MemorySandbox] = {}
        self.snapshots: dict[str, dict[str, bytes]] = {}
        #: Every snapshot handle it gave out, destroyed ones too.
        self.saved: list[str] = []
        self.attached: list[str] = []
        #: How long handing a retained Sandbox to a later episode takes.
        self.attach_delay = 0.0

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        """Linux containers on this machine's architecture."""
        return (
            CapabilityProfile(
                provider=self.id,
                kind=ImageKind.CONTAINER,
                os=OperatingSystem.LINUX,
                arch=machine_arch(),
                gui=True,
                network_modes=frozenset(NetworkMode),
                snapshot=self.saves_snapshots,
                container_features=self.container_features,
            ),
        )

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> None:
        return None

    async def list_retained(self) -> list[dict[str, object]]:
        return [{"handle": handle} for handle in [*self.kept, *self.snapshots]]

    async def destroy_retained(self, handle: str) -> None:
        from orbit.core.errors import ProviderStartError

        if handle in self.snapshots:
            # As every Provider's: never under a Sandbox booted from it.
            booted = [item for item in self.sandboxes if item.request.snapshot == handle]
            if any(not item.destroyed for item in booted):
                raise ProviderStartError(f"could not destroy {handle}: a Sandbox runs on it")
            del self.snapshots[handle]
        elif handle in self.kept:
            await self.kept.pop(handle).destroy()
        else:
            raise ProviderStartError(f"{handle} is gone")

    async def attach_retained(  # type: ignore[no-untyped-def]
        self, handle, request, allocation, image
    ):
        """The retained Sandbox, driven for a later episode with that episode's request."""
        import asyncio

        from orbit.core.errors import ProviderStartError

        await asyncio.sleep(self.attach_delay)
        sandbox = self.kept.get(handle)
        if sandbox is None or sandbox.destroyed:
            raise ProviderStartError(f"{handle} is gone")
        self.attached.append(handle)
        sandbox.request = request
        return sandbox

    async def prepare_image(self, image: ImageRef | PreparedTaskImage) -> PreparedTaskImage:
        if isinstance(image, PreparedTaskImage):
            return image
        return PreparedTaskImage(
            kind=image.kind,
            source="external-ref",
            input_identity="sha256:" + "0" * 64,
            runtime_ref=image.reference,
            prepared_identity="sha256:" + "0" * 64,
            resolved_reference=image.reference,
        )

    async def reattach(self, handle, request, allocation, image):  # type: ignore[no-untyped-def]
        """The Sandbox this Provider still runs under ``handle``, as another process finds it."""
        from orbit.core.errors import ProviderStartError

        for sandbox in self.sandboxes:
            if sandbox.handle == handle and not sandbox.destroyed:
                self.reattached.append(handle)
                return sandbox
        raise ProviderStartError(f"{handle} is gone")

    async def discard(self, handle: str) -> None:
        self.discarded.append(handle)
        for sandbox in self.sandboxes:
            if sandbox.handle == handle:
                await sandbox.destroy()

    async def create(self, request: SandboxRequest) -> Sandbox:
        import asyncio

        self.requests.append(request)
        await asyncio.sleep(self.create_delay)
        sandbox = MemorySandbox(self, request)
        if request.snapshot is not None:
            sandbox.files = dict(self.snapshots[request.snapshot])
        self.sandboxes.append(sandbox)
        return sandbox


class _NoSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScriptedCliHarness(CliHarness):
    """The smallest agent CLI that resumes its native session, for episodes of several turns.

    Each turn keeps its prompt in the session, reads the first line of it back from there
    and appends it to ``work.txt`` in the agent's home, so a verifier that counts the lines
    scores more each turn and reads which prompt each turn received, and reports its
    session as a CLI's stream would. On Linux it also starts a process meant to outlive the
    turn, after noting in ``survivors`` any an earlier turn left that still runs, in
    ``leaks`` what of ``validation/``, ``tests/`` and their feedback it can see, and in
    ``shown``, ``written`` and ``mounted`` whether it reads, and changes, a copy of the stage
    that validates the Task shows it, and whether that is a read-only mount.
    """

    name = "scripted-cli"
    package = "scripted-cli"
    binary = "sh"
    default_version = "1"
    settings_type = _NoSettings
    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS)
    logs = (".orbit-harness", ".scripted-cli")

    async def install(self, sandbox: Sandbox) -> str:
        return self.version()

    def integrity(self) -> str:
        return "test-double"

    def is_session_file(self, path: PurePosixPath, session_id: str) -> bool:
        return path.parts[:2] == (".scripted-cli", session_id)

    async def prepare_command(  # type: ignore[no-untyped-def]
        self, prompt_path, sandbox, session, *, native_session_id
    ):
        from orbit.run.harnesses.base import CliCommand

        session_id = native_session_id or "scripted"
        home = shlex.quote(session.home)
        directory = shlex.quote(f"{session.home}/.scripted-cli/{session_id}")
        survivors = f"""
for process in /proc/[0-9]*; do
  if tr '\\0' ' ' < "$process/cmdline" 2>/dev/null | grep -q '^sleep 9999'; then
    echo "turn $turn" >> {home}/survivors
  fi
done
(setsid sleep 9999 > /dev/null 2>&1 &)
for leaked in /opt/orbit/validation /opt/orbit/tests /logs/verifier/feedback.md; do
  if [ -e "$leaked" ]; then echo "turn $turn: $leaked" >> {home}/leaks; fi
done
if [ -r /opt/validation/verify.py ]; then echo "turn $turn" >> {home}/shown; fi
if awk '$2 == "/opt/validation" && $4 ~ /^ro(,|$)/' /proc/mounts | grep -q .; then
  echo "turn $turn" >> {home}/mounted
fi
if touch /opt/validation/verify.py 2>/dev/null; then echo "turn $turn" >> {home}/written; fi
"""
        script = f"""
mkdir -p {directory}
turn=$(( $(ls {directory} | wc -l) + 1 ))
cp {shlex.quote(prompt_path)} {directory}/turn-$turn.md
head -n 1 {directory}/turn-$turn.md >> {home}/work.txt
{survivors if session.os is OperatingSystem.LINUX else ""}
printf '{{"session_id": "%s", "result": "turn %s"}}\\n' {shlex.quote(session_id)} "$turn"
"""
        return CliCommand(script.strip(), {}, session_id)

    def read_result(self, stdout: str):  # type: ignore[no-untyped-def]
        from orbit.run.harnesses.base import NativeResult

        for line in reversed(stdout.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            return NativeResult(event.get("session_id"), event.get("result"))
        return NativeResult(None, None, error="no result")

    def parse_trajectory(self, context):  # type: ignore[no-untyped-def]
        """Each turn's prompt as a user message and its result as the agent's answer."""
        from orbit.core.trajectory import AtifAgent, TrajectoryBuilder
        from orbit.run.harnesses.base import invocations

        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id,
            agent=AtifAgent(name=self.name, version=context.agent_version),
        )
        for invocation in invocations(context):
            builder.add(source="user", message=invocation.prompt)
            answer = self.read_result(invocation.stdout).final_message
            if answer:
                builder.add(source="agent", message=answer)
        return builder.build()


class WorkingCliHarness(ScriptedCliHarness):
    """A CLI that works for ``lines`` lines of work in its first turn, one every ``every``
    seconds, appended to ``work.txt`` with when and on which host each was written, and goes
    on from the last line when its session resumes, as an agent whose turn is interrupted
    does; each run first notes the first line of its prompt in ``prompts.txt`` and starts a
    process meant to outlive it, noting in ``survivors`` any an earlier run left running."""

    def __init__(self, lines: int, *, every: int = 10) -> None:
        super().__init__()
        self.lines = lines
        self.every = every

    async def prepare_command(  # type: ignore[no-untyped-def]
        self, prompt_path, sandbox, session, *, native_session_id
    ):
        from orbit.run.harnesses.base import CliCommand

        session_id = native_session_id or "scripted"
        home = shlex.quote(session.home)
        directory = shlex.quote(f"{session.home}/.scripted-cli/{session_id}")
        script = f"""
mkdir -p {directory}
run=$(( $(ls {directory} | wc -l) + 1 ))
cp {shlex.quote(prompt_path)} {directory}/run-$run.md
head -n 1 {directory}/run-$run.md >> {home}/prompts.txt
for process in /proc/[0-9]*; do
  if tr '\\0' ' ' < "$process/cmdline" 2>/dev/null | grep -q '^sleep 9999'; then
    echo "run $run" >> {home}/survivors
  fi
done
(setsid sleep 9999 > /dev/null 2>&1 &)
touch {home}/work.txt
while [ "$(wc -l < {home}/work.txt)" -lt {self.lines} ]; do
  echo "$(date +%s) $(hostname) run $run" >> {home}/work.txt
  sleep {self.every}
done
printf '{{"session_id": "%s", "result": "run %s"}}\\n' {shlex.quote(session_id)} "$run"
"""
        return CliCommand(script.strip(), {}, session_id)


class TraceSink:
    """An episode's execution trace, as the episode's logging writes it."""

    def __init__(self) -> None:
        self.events: list[ExecutionLog] = []

    def append(self, event: object, *, durable: bool = False) -> int:
        assert isinstance(event, ExecutionLog)
        self.events.append(event)
        return len(self.events)


# --- validation rounds ---

#: The line of work a :class:`TurnsHarness` adds each turn, which :class:`Verifiers` score.
ROUNDS_WORK = "/home/user/work.txt"
#: Where a :class:`TurnsHarness` keeps its native session: every prompt and its answer.
TURNS_LOG = "/home/user/.turns/session.jsonl"
TURNS_SESSION = NativeContinuation(harness="turns", native_session_id="session")


class TurnsHarness(AutonomousHarness):
    """Writes one more line of work each turn, and keeps its native session in the agent's
    home: every prompt it received and what it answered."""

    name = "turns"
    resume_support = ResumeSupport.NATIVE
    logs = (".turns",)

    def __init__(self, *, stops: dict[int, Exception] | None = None, sleep: float = 0.0) -> None:
        self.stops = stops or {}
        self.sleep = sleep
        self.prompts: list[str] = []
        self.timeouts: list[float] = []
        self.sandboxes: list[str] = []

    def version(self) -> str:
        return "1"

    async def launch(self, instruction, sandbox, session, *, timeout_sec):  # type: ignore[no-untyped-def]
        return await self._turn(instruction, sandbox, session, timeout_sec)

    async def resume(self, instruction, continuation, sandbox, session, *, timeout_sec):  # type: ignore[no-untyped-def]
        assert continuation == TURNS_SESSION
        # Its session is in the Sandbox booted from the round's snapshot.
        assert await sandbox.read_file(TURNS_LOG)
        return await self._turn(instruction, sandbox, session, timeout_sec)

    async def _turn(
        self, prompt: str, sandbox: Any, session: HarnessSession, timeout_sec: float
    ) -> AgentRun:
        turn = len(self.prompts) + 1
        self.prompts.append(prompt)
        self.timeouts.append(timeout_sec)
        self.sandboxes.append(sandbox.sandbox_id)
        await asyncio.sleep(self.sleep)
        for path, line in (
            (ROUNDS_WORK, "line\n"),
            (TURNS_LOG, json.dumps({"user": prompt, "agent": f"turn {turn}"}) + "\n"),
        ):
            try:
                held = await sandbox.read_file(path)
            except Exception:
                held = b""
            await sandbox.write_file(path, held + line.encode())
        if turn in self.stops:
            raise self.stops[turn]
        return AgentRun(exit_code=0, final_message=f"turn {turn}", continuation=TURNS_SESSION)

    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory:
        builder = TrajectoryBuilder(
            trajectory_id=context.trajectory_id,
            session_id=context.session_id,
            agent=AtifAgent(name=self.name, version="1"),
        )
        session = context.logs_dir / ".turns" / "session.jsonl"
        for line in session.read_text().splitlines():
            event = json.loads(line)
            builder.add(source="user", message=event["user"])
            builder.add(source="agent", message=event["agent"])
        return builder.build()


class RoundsProvider(MemoryProvider):
    """Saves snapshots, ``max_saves`` in a row, gives each Sandbox an id of its own, and fails
    what ``fail`` names."""

    def __init__(self) -> None:
        super().__init__("docker:runc", saves_snapshots=True)
        self.fail: Callable[[SandboxRequest], bool] = lambda request: False
        self.refusal: type[Exception] = ProviderStartError
        #: The Sandboxes a Run's selection asked it about, in order: their role and needs.
        self.asked: list[tuple[SandboxRole, SandboxNeeds]] = []
        self.max_saves: int | None = None

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            profile.model_copy(update={"max_saves": self.max_saves})
            for profile in await super().profiles()
        )

    async def create(self, request: SandboxRequest) -> MemorySandbox:
        if self.fail(request):
            self.requests.append(request)
            raise self.refusal(f"no room for the {request.role}")
        sandbox = await super().create(request)
        assert isinstance(sandbox, MemorySandbox)
        sandbox.sandbox_id = f"{request.episode_id}-{len(self.sandboxes)}"
        return sandbox

    async def unserved(
        self, needs: SandboxNeeds, *, user: str, role: SandboxRole
    ) -> tuple[str, ...]:
        self.asked.append((role, needs))
        return await super().unserved(needs, user=user, role=role)

    def booted(self, role: SandboxRole) -> list[SandboxRequest]:
        return [request for request in self.requests if request.role is role]


class Verifiers:
    """``validation/`` and ``tests/`` both score the lines of work, a third each, and write
    feedback; the ``failing`` runs of ``validation/``, counted from 1, exit 1 instead, and
    ``seen`` is called with the Sandbox each run of ``validation/`` scores. ``envs`` holds
    each run's environment, of ``tests/`` too."""

    def __init__(self) -> None:
        self.failing: set[int] = set()
        self.seen: Callable[[MemorySandbox], None] = lambda sandbox: None
        self.feedback: dict[str, str] = {
            "/opt/orbit/validation": "{lines} lines; write more",
            "/opt/orbit/tests": "hidden test detail",
        }
        self.runs: list[tuple[str, str]] = []
        self.envs: list[dict[str, str]] = []

    async def __call__(self, sandbox: MemorySandbox, argv: Any, options: dict[str, Any]) -> bool:
        cwd = options.get("cwd")
        if list(argv) != ["bash", "test.sh"] or cwd not in self.feedback:
            return True
        self.runs.append((cwd, sandbox.sandbox_id))
        self.envs.append(dict(options.get("env") or {}))
        if cwd == "/opt/orbit/validation":
            self.seen(sandbox)
            if sum(stage == cwd for stage, _ in self.runs) in self.failing:
                return False
        lines = sandbox.files.get(ROUNDS_WORK, b"").count(b"\n")
        reward = round(min(1.0, lines / 3), 2)
        sandbox.files["/logs/verifier/reward.json"] = json.dumps({"reward": reward}).encode()
        text = self.feedback[cwd].format(lines=lines)
        sandbox.files["/logs/verifier/feedback.md"] = text.encode()
        return True


def rounds_task(
    tmp_path: Path,
    *,
    rounds: int = 2,
    mode: str = "shared",
    validation_dir: bool = True,
    validation: dict[str, Any] | None = None,
    **config: Any,
) -> ManifestTask:
    """A Task whose agent receives ``rounds`` validation rounds, verified in ``mode`` and
    validated as ``validation``, its ``[verifier.validation]``, says; without
    ``validation_dir``, ``tests/`` validates."""
    root = scaffold_task(tmp_path / "task")
    if validation_dir:
        (root / "validation").mkdir()
        (root / "validation" / "test.sh").write_text("#!/usr/bin/env bash\n")
    verifier: dict[str, Any] = {"environment_mode": mode}
    if mode != "shared":
        verifier["environment"] = {"cpus": 4, "memory_mb": 2048}
    if validation is not None:
        verifier["validation"] = validation
    edit_task_config(
        root,
        {
            "environment": {"cpus": 1, "memory_mb": 1024},
            "agent": {"validation_rounds": rounds, "timeout_sec": 600},
            "verifier": verifier,
            "artifacts": ["work.txt"],
            **config,
        },
    )
    return load_tasks(root)[0]
