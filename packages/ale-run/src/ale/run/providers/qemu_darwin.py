"""Native QEMU process management for Darwin hosts.

The Linux runtime deliberately remains container based.  macOS cannot pass its
Hypervisor.framework accelerator through Docker Desktop's Linux VM, so Darwin owns the
QEMU process directly.  A persistent control NIC keeps guestd and ALE host services
reachable while a second NIC is toggled through QMP to enforce episode egress phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import shutil
import signal
from pathlib import Path
from typing import Literal

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import PreparedTaskImage, SandboxRequest
from ale.run.sources import cache_root

GUEST_PORT = 7411
CONTROL_HOST_IP = "172.30.0.1"
HOST_IP = "172.30.0.100"
CONTROL_NET = "172.30.0.0/24"
EGRESS_NET = "10.0.2.0/24"
STATE_FILE = "runtime.json"
DARWIN_QEMU_TAG = "v10.0.2-utm"

Architecture = Literal["x86_64", "arm64"]


def image_architecture(image: PreparedTaskImage) -> Architecture:
    """Infer the guest CPU from the image name; legacy images are x86_64."""
    name = f"{image.resolved_reference} {image.runtime_ref}".lower()
    return "arm64" if "arm64" in name else "x86_64"


def host_architecture() -> Architecture:
    """Return the two host architectures supported by QEMU on macOS."""
    machine = platform.machine().lower()
    return "arm64" if machine in {"arm64", "aarch64"} else "x86_64"


def darwin_qemu_root() -> Path:
    """Return the pinned headless QEMU installation built by ALE's setup script."""
    configured = os.environ.get("ALE_DARWIN_QEMU_ROOT")
    return Path(configured) if configured else cache_root() / "darwin-qemu" / DARWIN_QEMU_TAG


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_state(storage: Path) -> dict[str, object]:
    try:
        state = json.loads((storage / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderStartError(f"invalid Darwin QEMU state in {storage}: {exc}") from exc
    if not isinstance(state, dict):
        raise ProviderStartError(f"invalid Darwin QEMU state in {storage}")
    return state


async def _qmp_command(
    socket: Path,
    execute: str,
    arguments: dict[str, object] | None = None,
) -> None:
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
    except OSError as exc:
        raise ProviderStartError(f"could not connect to QMP at {socket}: {exc}") from exc

    async def response() -> dict[str, object]:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not line:
                raise ProviderStartError(f"QMP at {socket} closed unexpectedly")
            message = json.loads(line)
            if "event" not in message:
                return message

    try:
        greeting = await response()
        if "QMP" not in greeting:
            raise ProviderStartError(f"QMP at {socket} returned no greeting")
        writer.write(b'{"execute":"qmp_capabilities"}\n')
        await writer.drain()
        capabilities = await response()
        if "error" in capabilities:
            raise ProviderStartError(f"QMP capabilities failed: {capabilities['error']}")
        command: dict[str, object] = {"execute": execute}
        if arguments:
            command["arguments"] = arguments
        writer.write(json.dumps(command, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        result = await response()
        if "error" in result:
            raise ProviderStartError(f"QMP {execute} failed: {result['error']}")
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class DarwinRuntime:
    """Run concurrent ALE guests as native macOS QEMU processes."""

    def __init__(self, overlay_dir: Path | None = None) -> None:
        self.overlay_dir = overlay_dir or Path.home() / ".cache/ale/qemu"
        self.registry_dir = cache_root() / "qemu-runtimes"

    def _registry_file(self, runtime_id: str) -> Path:
        return self.registry_dir / f"{runtime_id}.json"

    async def preflight(self, fixture_image: Path | None = None) -> None:
        problems: list[str] = []
        for binary in ("qemu-img", "qemu-system-x86_64"):
            if shutil.which(binary) is None:
                problems.append(f"{binary} is not on PATH (brew install qemu)")
        if host_architecture() == "arm64":
            arm_qemu = self._arm_binary()
            if not arm_qemu.is_file() or not os.access(arm_qemu, os.X_OK):
                problems.append(
                    f"pinned ARM QEMU is unavailable at {arm_qemu}; "
                    "run scripts/build-darwin-qemu.sh"
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    str(arm_qemu),
                    "-device",
                    "help",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await process.communicate()
                if process.returncode != 0 or b'name "virtio-ramfb"' not in output:
                    problems.append(f"pinned ARM QEMU at {arm_qemu} lacks virtio-ramfb")
        if fixture_image is not None and not fixture_image.is_file():
            problems.append(
                f"no injected guest image at {fixture_image}; run ale prepare on a VM Task"
            )
        if problems:
            raise ProviderCapabilityError("; ".join(problems))

    def backing_path(self, base_image: Path) -> str:
        return str(base_image.resolve())

    def _arm_binary(self) -> Path:
        return darwin_qemu_root() / "bin/qemu-system-aarch64-utm"

    def _firmware_dir(self, binary: str) -> Path:
        requested = Path(binary)
        executable = requested if requested.is_absolute() else shutil.which(binary)
        if executable is None or not Path(executable).is_file():
            raise ProviderCapabilityError(f"{binary} is not on PATH (brew install qemu)")
        resolved = Path(executable).resolve()
        candidates = (
            resolved.parent.parent / "share/qemu",
            Path("/opt/homebrew/share/qemu"),
            Path("/usr/local/share/qemu"),
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise ProviderCapabilityError("QEMU firmware directory is unavailable")

    def command(
        self,
        request: SandboxRequest,
        storage: Path,
        host_port: int,
        architecture: Architecture,
    ) -> list[str]:
        overlay = storage / "data.qcow2"
        qmp = storage / "qmp.sock"
        pidfile = storage / "qemu.pid"
        control = (
            f"user,id=control,net={CONTROL_NET},host={CONTROL_HOST_IP},dhcpstart=172.30.0.2,"
            f"restrict=on,hostfwd=tcp:127.0.0.1:{host_port}-:{GUEST_PORT}"
        )
        for port in sorted(
            {
                value
                for value in (
                    self._port(request.gateway_url),
                    self._port(request.proxy_url),
                )
                if value
            }
        ):
            control += f",guestfwd=tcp:{HOST_IP}:{port}-cmd:/usr/bin/nc 127.0.0.1 {port}"

        # The published x86 Windows base predates the VirtIO driver injection used by
        # the ARM builder.  e1000e is supported by an inbox Windows driver, while the
        # ARM image deliberately uses the faster VirtIO devices installed at build time.
        nic = "virtio-net-pci" if architecture == "arm64" else "e1000e"

        common = [
            "-name",
            f"ale-{storage.name}",
            "-m",
            str(request.resources.memory_mb),
            "-smp",
            str(request.resources.cpus),
            "-netdev",
            control,
            "-device",
            f"{nic},netdev=control,id=control-nic,mac=52:54:00:30:00:02",
            "-netdev",
            f"user,id=egress,net={EGRESS_NET},host=10.0.2.2,dhcpstart=10.0.2.15",
            "-device",
            f"{nic},netdev=egress,id=egress-nic,mac=52:54:00:20:00:02",
            "-qmp",
            f"unix:{qmp},server=on,wait=off",
            "-pidfile",
            str(pidfile),
            "-display",
            "none",
            "-no-reboot",
        ]
        native = architecture == host_architecture()
        accelerator = "hvf" if native else "tcg"
        if architecture == "arm64":
            binary = str(self._arm_binary())
            firmware = self._firmware_dir(binary)
            code = firmware / "edk2-aarch64-code.fd"
            template = firmware / "edk2-arm-vars.fd"
            variables = storage / "efi-vars.fd"
            if not code.is_file() or not template.is_file():
                raise ProviderCapabilityError("QEMU aarch64 UEFI firmware is unavailable")
            if not variables.exists():
                shutil.copyfile(template, variables)
            return [
                binary,
                "-L",
                str(firmware),
                "-machine",
                f"virt,accel={accelerator},highmem=off",
                "-cpu",
                "host" if native else "max",
                "-drive",
                f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive",
                f"if=pflash,format=qcow2,file={variables}",
                "-device",
                "virtio-ramfb",
                "-device",
                "qemu-xhci",
                "-device",
                "usb-kbd",
                "-device",
                "usb-tablet",
                "-drive",
                f"if=none,id=system,file={overlay},format=qcow2,discard=unmap",
                "-device",
                "nvme,drive=system,serial=ALEWIN11ARM64,bootindex=0",
                *common,
            ]
        binary = "qemu-system-x86_64"
        firmware = self._firmware_dir(binary)
        code = firmware / "edk2-x86_64-code.fd"
        template = firmware / "edk2-i386-vars.fd"
        variables = storage / "efi-vars.fd"
        if not code.is_file() or not template.is_file():
            raise ProviderCapabilityError("QEMU x86_64 UEFI firmware is unavailable")
        if not variables.exists():
            shutil.copyfile(template, variables)
        return [
            binary,
            "-machine",
            f"q35,accel={accelerator}",
            "-cpu",
            (
                "host,hv_relaxed=on,hv_vapic=on,hv_time=on"
                if native
                else "max,hv_relaxed=on,hv_vapic=on,hv_time=on"
            ),
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive",
            f"if=pflash,format=raw,file={variables}",
            "-drive",
            f"file={overlay},if=ide,format=qcow2,discard=unmap",
            *common,
        ]

    async def boot(
        self,
        request: SandboxRequest,
        storage: Path,
        host_port: int,
        architecture: Architecture,
    ) -> str:
        if request.resources.gpus:
            raise ProviderCapabilityError("Darwin QEMU does not support GPU passthrough")
        runtime_id = storage.name
        argv = self.command(request, storage, host_port, architecture)
        log_path = storage / "qemu.log"
        with log_path.open("ab", buffering=0) as log:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        await asyncio.sleep(1)
        if process.returncode is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise ProviderStartError(f"native QEMU exited immediately: {detail.strip()}")
        state = {
            "runtime": "darwin",
            "id": runtime_id,
            "pid": process.pid,
            "episode": request.episode_id,
            "role": request.role.value,
            "retention": request.retention,
            "architecture": architecture,
            "host_port": host_port,
            "image": request.prepared_image.resolved_reference,
            "storage": str(storage.resolve()),
        }
        serialized = json.dumps(state, sort_keys=True)
        (storage / STATE_FILE).write_text(serialized, encoding="utf-8")
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._registry_file(runtime_id).write_text(serialized, encoding="utf-8")
        return runtime_id

    async def set_egress(self, storage: Path, *, enabled: bool) -> None:
        await _qmp_command(
            storage / "qmp.sock",
            "set_link",
            {"name": "egress-nic", "up": enabled},
        )

    async def destroy(self, storage: Path) -> None:
        state = _read_state(storage)
        pid = int(state.get("pid", 0))
        if not _process_alive(pid):
            self._registry_file(str(state.get("id", ""))).unlink(missing_ok=True)
            return
        with contextlib.suppress(Exception):
            await _qmp_command(storage / "qmp.sock", "quit")
        for _ in range(50):
            if not _process_alive(pid):
                self._registry_file(str(state.get("id", ""))).unlink(missing_ok=True)
                return
            await asyncio.sleep(0.1)
        command = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid), "-o", "command=", stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await command.communicate()
        rendered = stdout.decode("utf-8", "replace")
        if "qemu-system-" not in rendered or str(storage / "data.qcow2") not in rendered:
            raise ProviderStartError(f"refusing to signal reused or unrelated pid {pid}")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        for _ in range(20):
            if not _process_alive(pid):
                self._registry_file(str(state.get("id", ""))).unlink(missing_ok=True)
                return
            await asyncio.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        for _ in range(20):
            if not _process_alive(pid):
                self._registry_file(str(state.get("id", ""))).unlink(missing_ok=True)
                return
            await asyncio.sleep(0.1)
        raise ProviderStartError(f"native QEMU pid {pid} did not stop")

    async def list_retained(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if not self.registry_dir.is_dir():
            return found
        for registry_file in self.registry_dir.glob("*.json"):
            with contextlib.suppress(ProviderStartError, OSError, TypeError, ValueError):
                registry = json.loads(registry_file.read_text(encoding="utf-8"))
                storage = Path(str(registry["storage"]))
                state = _read_state(storage)
                if state.get("runtime") != "darwin" or state.get("retention") != "keep":
                    continue
                runtime_id = str(state["id"])
                found.append(
                    {
                        "provider": "qemu",
                        "handle": f"qemu:{runtime_id}",
                        "episode": str(state.get("episode", "")),
                        "role": str(state.get("role", "")),
                        "image": str(state.get("image", "")),
                        "gpus": (),
                        "running": _process_alive(int(state.get("pid", 0))),
                        "cleanup_command": f"ale sandbox destroy qemu:{runtime_id}",
                    }
                )
        return sorted(found, key=lambda item: str(item["handle"]))

    async def destroy_retained(self, runtime_id: str) -> None:
        try:
            registry = json.loads(self._registry_file(runtime_id).read_text(encoding="utf-8"))
            storage = Path(str(registry["storage"]))
        except (KeyError, OSError, json.JSONDecodeError) as exc:
            raise ProviderStartError(f"sandbox qemu:{runtime_id} does not exist: {exc}") from exc
        state = _read_state(storage)
        if (
            state.get("runtime") != "darwin"
            or state.get("retention") != "keep"
            or state.get("id") != runtime_id
            or state.get("storage") != str(storage.resolve())
        ):
            raise ProviderCapabilityError(f"qemu:{runtime_id!s} is not a retained Darwin VM")
        await self.destroy(storage)
        shutil.rmtree(storage, ignore_errors=True)

    @staticmethod
    def _port(url: str | None) -> str:
        if not url:
            return ""
        authority = url.partition("://")[2].partition("/")[0]
        return authority.rpartition(":")[2] if ":" in authority else ""
