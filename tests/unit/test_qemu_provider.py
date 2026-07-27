"""The VM backend's decisions that can be checked without booting one.

A guest image is a multi-gigabyte build, so the boot path is covered by the conformance
suite behind ``needs_kvm``. What belongs here is everything that must be right *before*
anything is provisioned — because a provider that fails late fails expensively.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.qemu import HOST_IP, QemuProvider, QemuSandbox, _port_of

pytestmark = pytest.mark.unit


def request(**overrides: object) -> SandboxRequest:
    base: dict[str, object] = {
        "episode_id": "e1",
        "image_ref": "ale-ubuntu22",
        "resources": Resources(cpus=2, memory_mb=2048),
        "network": NetworkPolicy(),
        "gateway_url": "http://0.0.0.0:8931",
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


class TestPreflight:
    def test_a_missing_image_names_the_way_to_build_one(self, tmp_path: Path) -> None:
        provider = QemuProvider(image=tmp_path / "absent.qcow2")
        with pytest.raises(ProviderCapabilityError, match=r"build\.sh"):
            asyncio.run(provider.preflight())

    def test_problems_are_reported_together(self, tmp_path: Path) -> None:
        """One run should surface every reason, not the first one alphabetically."""
        provider = QemuProvider(image=tmp_path / "absent.qcow2")
        try:
            asyncio.run(provider.preflight())
        except ProviderCapabilityError as error:
            assert "guest image" in str(error)


class TestCapabilities:
    def test_allowlist_is_refused_rather_than_degraded(self) -> None:
        """The in-guest rules are not written, so claiming the mode would be a lie."""
        provider = QemuProvider()
        assert NetworkMode.ALLOWLIST not in provider.capabilities().network_modes
        with pytest.raises(ProviderCapabilityError, match="allowlist"):
            provider.accepts(
                request(
                    network=NetworkPolicy(
                        mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)
                    )
                )
            )

    def test_block_and_open_are_offered(self) -> None:
        modes = QemuProvider().capabilities().network_modes
        assert {NetworkMode.BLOCK, NetworkMode.OPEN} <= modes

    def test_a_desktop_task_passes_the_backend_check(self) -> None:
        """A screen is a property of the disk, so the refusal belongs at the image.

        This backend can host a guest that has one; whether the guest handed to it does
        is read from its manifest once it is up, which is where the refusal happens.
        """
        QemuProvider().accepts(request(needs_gui=True))


class TestGatewayAddressing:
    def test_the_host_is_rewritten_to_the_runner_address(self) -> None:
        """A host bind address is meaningless inside the guest.

        What the guest can reach is the runner holding it, which forwards this one port
        onward — so that is the address the agent's SDK must be handed.
        """
        sandbox = QemuSandbox.__new__(QemuSandbox)
        sandbox.request = request(gateway_url="http://0.0.0.0:8931")  # type: ignore[attr-defined]
        assert sandbox.gateway_url == f"http://{HOST_IP}:8931"

    def test_no_gateway_stays_absent(self) -> None:
        sandbox = QemuSandbox.__new__(QemuSandbox)
        sandbox.request = request(gateway_url="")  # type: ignore[attr-defined]
        assert sandbox.gateway_url is None


class TestGatewayPort:
    """The port the runner forwards is parsed from the URL, not configured twice."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://127.0.0.1:8931", "8931"),
            ("http://127.0.0.1:8931/v1", "8931"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_ports_are_read_from_the_url(self, url: str | None, expected: str) -> None:
        assert _port_of(url) == expected
