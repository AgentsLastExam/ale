"""The VM backend against the same suite the container backend passes.

One suite, two backends, no per-provider assertions: that is what makes "the sandbox
contract is real" a claim rather than an aspiration, and what turns the future OS
roadmap into "swap the guest image".

Needs KVM and a built guest image, so it skips rather than fails where either is absent.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, Sandbox, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers.docker import DockerProvider
from ale.run.providers.docker import destroy_retained as destroy_retained_docker
from ale.run.providers.qemu import QemuProvider, destroy_retained, list_retained
from ale.run.sources import cache_root

LEGACY_IMAGE = Path.home() / ".cache/ale/images/ale-ubuntu-desktop.qcow2"
DEFAULT_IMAGE = max(
    (cache_root() / "vm-builds").glob("*.qcow2"),
    key=lambda path: path.stat().st_mtime_ns,
    default=LEGACY_IMAGE,
)
IMAGE = Path(os.environ.get("ALE_QEMU_IMAGE", DEFAULT_IMAGE))

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_kvm,
    pytest.mark.skipif(
        not IMAGE.is_file(),
        reason=f"no prepared VM disk at {IMAGE}; run ale prepare on a VM Task",
    ),
]


class TestQemuProvider(ProviderConformance):
    """Every assertion in the shared suite, run against a virtual machine."""

    provider = QemuProvider(image=IMAGE)
    image = ImageRef(kind="vm", reference="ale-guest-ubuntu-desktop:24.04")
    #: The same disk. There is one VM guest and it has a desktop, so the GUI half of
    #: the shared suite runs against it rather than being skipped.
    gui_image = image

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources())
        assert isinstance(resources, Resources)
        if resources.gpus == 0:
            overrides["resources"] = resources.model_copy(
                update={
                    "cpus": max(resources.cpus, 2),
                    "memory_mb": max(resources.memory_mb, 4096),
                }
            )
        return await super().request(**overrides)

    @pytest.mark.asyncio
    async def test_ubuntu_gnome_session_and_fresh_overlay(self) -> None:
        assert self.provider.capabilities().network_modes == frozenset(NetworkMode)
        started = time.monotonic()
        first = await self.provider.create(await self.request())
        assert time.monotonic() - started < 60
        try:
            release = await first.exec(["sh", "-c", ". /etc/os-release; echo $VERSION_ID"])
            assert release.stdout.strip() == "24.04"
            assert (await first.exec(["systemctl", "is-active", "ale-guestd"])).ok
            assert (await first.exec(["systemctl", "is-active", "gdm3"])).ok
            assert (await first.exec(["pgrep", "-x", "gnome-shell"], identity=Identity.AGENT)).ok
            assert (await first.screenshot()).startswith(b"\x89PNG")
            await first.write_file("/home/user/overlay-marker", b"first")
        finally:
            await first.destroy()

        async with await self.provider.create(await self.request()) as fresh:
            assert not (await fresh.exec(["test", "-e", "/home/user/overlay-marker"])).ok

    @pytest.mark.asyncio
    async def test_retained_vm_stays_running_until_explicit_destroy(self) -> None:
        sandbox = await self.provider.create(await self.request(retention="keep"))
        retained = await sandbox.retain(roles=("solver",), reason="conformance")
        sandbox.release_resources()
        try:
            records = await list_retained()
            record = next(item for item in records if item["handle"] == retained.handle)
            assert record["provider"] == "qemu"
            assert record["running"] is True
            assert Path(sandbox.storage).is_dir()  # type: ignore[attr-defined]
        finally:
            await destroy_retained(retained.handle)
        assert not Path(sandbox.storage).exists()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cli_manages_multiple_mixed_retained_sandboxes() -> None:
    docker = DockerProvider()
    qemu = QemuProvider(image=IMAGE)
    docker_image = await docker.prepare_image(
        ImageRef(kind="container", reference="ghcr.io/agentslastexam/sandbox-base-cli:latest")
    )
    qemu_image = await qemu.prepare_image(
        ImageRef(kind="vm", reference="ale-guest-ubuntu-desktop:24.04")
    )
    active: list[Sandbox] = []
    retained: set[str] = set()
    qemu_storage: Path | None = None

    def cli(*args: str) -> str:
        result = subprocess.run(
            [str(Path(sys.executable).parent / "ale"), "sandbox", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    async def keep(provider: DockerProvider | QemuProvider, request: SandboxRequest) -> Sandbox:
        sandbox = await provider.create(request)
        active.append(sandbox)
        kept = await sandbox.retain(roles=("solver",), reason="mixed conformance")
        sandbox.release_resources()
        active.remove(sandbox)
        retained.add(kept.handle)
        return sandbox

    try:
        for index in range(2):
            await keep(
                docker,
                SandboxRequest(
                    episode_id=f"mixed-docker-{index}-{uuid.uuid4().hex[:6]}",
                    retention="keep",
                    prepared_image=docker_image,
                    resources=Resources(cpus=1, memory_mb=512),
                    network=NetworkPolicy(),
                ),
            )
        vm = await keep(
            qemu,
            SandboxRequest(
                episode_id=f"mixed-qemu-{uuid.uuid4().hex[:6]}",
                retention="keep",
                prepared_image=qemu_image,
                resources=Resources(cpus=2, memory_mb=4096),
                network=NetworkPolicy(),
            ),
        )
        qemu_storage = Path(vm.storage)  # type: ignore[attr-defined]

        listing = cli("list")
        assert all(handle in listing for handle in retained)

        qemu_handle = next(handle for handle in retained if handle.startswith("qemu:"))
        cli("destroy", qemu_handle)
        retained.remove(qemu_handle)
        assert qemu_storage is not None and not qemu_storage.exists()
        listing = cli("list")
        assert qemu_handle not in listing
        assert all(handle in listing for handle in retained)

        first_docker = sorted(retained)[0]
        cli("destroy", first_docker)
        retained.remove(first_docker)
        listing = cli("list")
        assert first_docker not in listing
        assert all(handle in listing for handle in retained)

        last = retained.pop()
        cli("destroy", last)
        assert last not in cli("list")
    finally:
        for sandbox in active:
            with contextlib.suppress(Exception):
                await sandbox.destroy()
        for handle in retained:
            with contextlib.suppress(Exception):
                if handle.startswith("qemu:"):
                    await destroy_retained(handle)
                else:
                    await destroy_retained_docker(handle)
