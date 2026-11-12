from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ale.core.config import RunConfig
from ale.core.sandbox import ImageRef, PreparedTaskImage, Provider, Sandbox, SandboxRequest
from ale.core.taskspec import ImageKind, NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run import ale_image
from ale.run.providers import ProviderRegistry

ENGINE_ROOT = Path(__file__).resolve().parents[1]
#: The current ALE bases, for images a test builds or runs.
CONTAINER_BASE = ale_image("container-ubuntu22-base")
VM_BASE = ale_image("vm-ubuntu24-base")
#: What a Linux VM Task asks for. The VM base boots a GNOME desktop, which in a container
#: Task's 512 MB spends its time paging and can miss a phase deadline.
VM_RESOURCES: dict[str, Any] = {"cpus": 2, "memory_mb": 4096}


def skip_without_kata() -> None:
    """Skip unless this host starts Kata microVMs: KVM, and Kata's shim on Docker's PATH.

    The shim is looked up on this process's PATH, which holds the same system directories
    as the daemon's on a host that runs ``ale`` beside its Docker daemon.
    """
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("/dev/kvm is unavailable")
    if shutil.which("containerd-shim-kata-v2") is None:
        pytest.skip("Kata Containers is not installed (containerd-shim-kata-v2 not on PATH)")


def sibling_checkout(name: str) -> Path:
    """Locate a sibling repository from main or any linked worktree."""
    return next(
        (parent / name for parent in ENGINE_ROOT.parents if (parent / name).is_dir()),
        ENGINE_ROOT.parent / name,
    )


def provider_registry(
    provider: Provider | None = None,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> ProviderRegistry:
    registry = ProviderRegistry(RunConfig())
    if provider is not None:
        registry._instances[(kind, False)] = provider
    return registry


async def prepare_reference(
    provider: Provider,
    reference: str,
    *,
    kind: ImageKind = ImageKind.CONTAINER,
) -> PreparedTaskImage:
    return await provider.prepare_image(ImageRef(kind=kind, reference=reference))


#: The smallest valid ``task.toml``: a container Task with no network.
MINIMAL_TASK_CONFIG: dict[str, Any] = {
    "schema_version": "1.4+ale.1",
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
#: ``environment/build.sh`` of a macOS image with the runtimes the ALE bases provide CLIs:
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

    Linux, in a container of the current base; or macOS with ``ALE_TEST_CLI_OS=macos`` on
    a Mac whose local Orchard ``scripts/setup_macos.py`` keeps running.
    """
    return OperatingSystem(os.environ.get("ALE_TEST_CLI_OS", "linux"))


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
        from ale.run.task_images import prepare_task_image
        from ale.run.tasksets.manifest import load_tasks

        folder = work / "cli-runtime"
        write_task(
            folder,
            name="agentslastexam/cli-runtime",
            config={
                "environment": {"os": "macos", "vm_base": MACOS_BASE},
                "agent": {"user": "admin", "sudo": True, "network_mode": "public"},
            },
            dockerfile=None,
        )
        (folder / "environment").mkdir()
        (folder / "environment" / "build.sh").write_text(MACOS_RUNTIME_BUILD)
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        image = await prepare_task_image(load_tasks(folder)[0], registry)
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
        provider = registry.get(ImageKind.VM, OperatingSystem.MACOS)
    else:
        provider = registry.get(ImageKind.CONTAINER, OperatingSystem.LINUX)
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

    async def __call__(self, *argv: str, **_kwargs: object) -> tuple[int, str, str]:
        self.calls.append(argv)
        assert argv[0] == "qemu-img", argv
        options, positional = self._parse(argv[2:])
        command = argv[1]
        try:
            if command == "check":
                return (0 if self.backing(Path(positional[0])) is not False else 1), "", ""
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
