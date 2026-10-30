"""The VM backend against the same suite the container backend passes.

One suite, two backends, no per-provider assertions: that is what makes "the sandbox
contract is real" a claim rather than an aspiration, and what turns the future OS
roadmap into "swap the guest image".

The disk is the current VM base, materialized once per host, without the
framework: the Provider adds it, as Docker adds it to the container base. Set
``ALE_QEMU_IMAGE`` to run the suite on another Linux disk. Needs KVM and Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from tests.support import CONTAINER_BASE, VM_BASE

from ale.core.sandbox import Identity, ImageRef, Sandbox, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run import task_images
from ale.run.framework import framework_identity
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_kvm]


@pytest.fixture(scope="module")
def vm_disk() -> Path:
    """The disk every VM in this module boots, the same for every run of one engine."""
    if override := os.environ.get("ALE_QEMU_IMAGE"):
        return Path(override)
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("/dev/kvm is unavailable")

    async def materialize() -> Path:
        identity = await task_images._docker_image_identity(VM_BASE)
        build = task_images._OciBuild(
            input_identity=identity,
            image_source_identity=identity,
            runtime_ref=identity,
            oci_identity=identity,
            base_materials=(),
        )
        return Path((await task_images._materialize_vm(build, source="solver-local")).runtime_ref)

    return asyncio.run(materialize())


class TestQemuProvider(ProviderConformance):
    """Every assertion in the shared suite, run against a virtual machine."""

    image = ImageRef(kind="vm", reference=VM_BASE)
    #: The same disk. There is one VM guest and it has a desktop, so the GUI half of
    #: the shared suite runs against it rather than being skipped.
    gui_image = image
    framework = framework_identity(user="user", sudo=False, vm=True)

    @pytest.fixture(autouse=True)
    def _provider(self, vm_disk: Path) -> None:
        self.provider = QemuProvider(image=vm_disk)

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
        first = await self.provider.create(await self.request())
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

        # Timed on the second boot: the first may have framed the disk.
        request = await self.request()
        started = time.monotonic()
        async with await self.provider.create(request) as fresh:
            assert time.monotonic() - started < 60
            assert not (await fresh.exec(["test", "-e", "/home/user/overlay-marker"])).ok

    @pytest.mark.asyncio
    async def test_retained_vm_stays_running_until_explicit_destroy(self) -> None:
        sandbox = await self.provider.create(await self.request(retention="keep"))
        retained = await sandbox.retain(roles=("solver",), reason="conformance")
        sandbox.release_resources()
        try:
            records = await QemuProvider().list_retained()
            record = next(item for item in records if item["handle"] == retained.handle)
            assert record["provider"] == "qemu"
            assert record["running"] is True
            assert Path(sandbox.storage).is_dir()  # type: ignore[attr-defined]
        finally:
            await QemuProvider().destroy_retained(retained.handle)
        assert not Path(sandbox.storage).exists()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cli_manages_multiple_mixed_retained_sandboxes(vm_disk: Path) -> None:
    docker = DockerProvider()
    qemu = QemuProvider(image=vm_disk)
    docker_image = await docker.prepare_image(ImageRef(kind="container", reference=CONTAINER_BASE))
    qemu_image = await qemu.prepare_image(ImageRef(kind="vm", reference=VM_BASE))
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
                    await QemuProvider().destroy_retained(handle)
                else:
                    await DockerProvider().destroy_retained(handle)
