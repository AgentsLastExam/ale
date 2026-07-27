"""Provider conformance suite.

Subclass, set ``provider``, and the same assertions that keep the container backend
honest apply to any other backend. The suite deliberately tests *contract* behaviour —
what callers are entitled to assume — not implementation details.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import Provider, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

__all__ = ["ProviderConformance"]


class ProviderConformance:
    """Assertions every :class:`~ale.core.sandbox.Provider` must satisfy."""

    provider: Provider
    image_ref: str = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

    gui_image_ref: str = ""
    """An image that starts a desktop, if this backend has one to test with.

    A desktop is a property of the *image*, not of the backend: a provider that can host
    one says so in its capabilities, but whether a given sandbox has a screen depends on
    what it was built from. So the GUI assertions need an image that claims a desktop,
    and skip rather than fail where none is configured."""

    def request(self, **overrides: object) -> SandboxRequest:
        base: dict[str, object] = {
            "episode_id": f"conformance-{uuid.uuid4().hex[:8]}",
            "image_ref": self.image_ref,
            "resources": Resources(cpus=1, memory_mb=512),
            "network": NetworkPolicy(mode=NetworkMode.BLOCK),
        }
        return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_preflight_is_explicit(self) -> None:
        """A provider either works here or says why, before anything is provisioned."""
        await self.provider.preflight()

    @pytest.mark.asyncio
    async def test_declares_capabilities(self) -> None:
        caps = self.provider.capabilities()
        assert caps.network_modes, "a provider must declare at least one network mode"
        assert caps.os

    @pytest.mark.asyncio
    async def test_exec_reports_exit_codes_and_streams(self) -> None:
        async with await self.provider.create(self.request()) as sandbox:
            result = await sandbox.exec(["echo", "conformance"])
            assert result.ok
            assert "conformance" in result.stdout

            failure = await sandbox.exec(["sh", "-c", "exit 3"])
            assert failure.exit_code == 3

    @pytest.mark.asyncio
    async def test_file_round_trip(self) -> None:
        async with await self.provider.create(self.request()) as sandbox:
            path = PurePosixPath("/tmp/conformance.bin")
            payload = b"\x00binary\xffdata"
            await sandbox.write_file(path, payload)
            assert await sandbox.read_file(path) == payload

    @pytest.mark.asyncio
    async def test_destroy_is_idempotent(self) -> None:
        """Teardown runs on failure paths too, sometimes more than once."""
        sandbox = await self.provider.create(self.request())
        await sandbox.destroy()
        await sandbox.destroy()

    @pytest.mark.asyncio
    async def test_rejects_a_request_it_cannot_serve(self) -> None:
        caps = self.provider.capabilities()
        if caps.gpus > 0:
            pytest.skip("provider offers GPUs; nothing to reject here")
        from ale.core.errors import ProviderCapabilityError

        with pytest.raises(ProviderCapabilityError):
            self.provider.accepts(self.request(resources=Resources(gpus=1)))

    @pytest.mark.asyncio
    async def test_a_declared_desktop_can_actually_be_used(self) -> None:
        """A provider claiming a desktop is held to it.

        Declaring the capability and not supplying it is the failure mode this suite
        exists to catch, and it is quiet: an image with no graphical session looks
        healthy until the first screenshot, which then reports a display error rather
        than a missing capability.

        Skipped where the provider does not claim a desktop — that is an honest answer.
        """
        if not (self.provider.capabilities().gui and self.gui_image_ref):
            pytest.skip("no desktop image configured for this backend")

        request = self.request(image_ref=self.gui_image_ref, needs_gui=True)
        async with await self.provider.create(request) as sandbox:
            png = await sandbox.screenshot()
            assert png.startswith(b"\x89PNG"), "a desktop was declared but produced no image"
            assert len(png) > 1000, "a capture this small is an empty screen, not a desktop"

    @pytest.mark.asyncio
    async def test_a_declared_desktop_accepts_input(self) -> None:
        """The other half of a desktop: an agent can act on it, not only look at it."""
        if not (self.provider.capabilities().gui and self.gui_image_ref):
            pytest.skip("no desktop image configured for this backend")

        request = self.request(image_ref=self.gui_image_ref, needs_gui=True)
        async with await self.provider.create(request) as sandbox:
            applied = await sandbox.inject_input([{"type": "move", "coordinate": [500, 500]}])
            assert applied == 1, "the desktop accepted no input"

    @pytest.mark.asyncio
    async def test_an_undeclared_desktop_is_refused_not_faked(self) -> None:
        """A headless provider says so at admission rather than failing mid-episode."""
        if self.provider.capabilities().gui:
            pytest.skip("this provider claims a desktop")

        with pytest.raises(ProviderCapabilityError):
            self.provider.accepts(self.request(needs_gui=True))
