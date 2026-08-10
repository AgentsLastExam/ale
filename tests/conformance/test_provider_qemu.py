"""The VM backend against the same suite the container backend passes.

One suite, two backends, no per-provider assertions: that is what makes "the sandbox
contract is real" a claim rather than an aspiration, and what turns the future OS
roadmap into "swap the guest image".

Needs KVM and a built guest image, so it skips rather than fails where either is absent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkMode, Resources
from ale.core.testkit import ProviderConformance
from ale.run.providers.qemu import QemuProvider
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
