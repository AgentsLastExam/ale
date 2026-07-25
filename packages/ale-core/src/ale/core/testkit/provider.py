"""Provider conformance suite.

Subclass, set ``provider``, and the same assertions that keep the container backend
honest apply to any other backend. The suite deliberately tests *contract* behaviour —
what callers are entitled to assume — not implementation details.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import pytest

from ale.core.sandbox import Provider, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources

__all__ = ["ProviderConformance"]


class ProviderConformance:
    """Assertions every :class:`~ale.core.sandbox.Provider` must satisfy."""

    provider: Provider
    image_ref: str = "ghcr.io/agentslastexam/sandbox-base-cli:latest"

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
