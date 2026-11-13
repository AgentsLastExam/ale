"""A sealed Windows agent reaches its allowlist through the account's system proxy too."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ale.core.sandbox import Identity, ImageRef, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.providers.qemu import QemuProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_kvm]

POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
SETTINGS = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
#: The account's proxy settings as WinINet keeps them, with the default connection's flags.
OWN_SETTINGS = (
    f"$s = Get-ItemProperty '{SETTINGS}'; "
    f"$c = (Get-ItemProperty '{SETTINGS}\\Connections' -ErrorAction SilentlyContinue)"
    ".DefaultConnectionSettings; "
    "'{0}|{1}|{2}|{3}' -f $s.ProxyEnable, $s.ProxyServer, $s.ProxyOverride, $(if ($c) { $c[8] })"
)


def _fetch(url: str) -> list[str]:
    """PowerShell 5.1's web client, which ignores the proxy variables, as a browser does."""
    return [
        *POWERSHELL,
        f"try {{ (Invoke-WebRequest {url} -UseBasicParsing -TimeoutSec 60).StatusCode }} "
        "catch { $_.Exception.Message }",
    ]


@pytest.mark.asyncio
async def test_a_sealed_windows_agent_reaches_its_allowlist_through_the_system_proxy(
    tmp_path: Path,
) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk or not Path(disk).is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(
            episode_id="allowlist", model="none", allowed_hosts=frozenset({"example.com"})
        )
    )
    proxy = EgressProxy(sessions, host="0.0.0.0")
    await proxy.start()
    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    request = SandboxRequest(
        episode_id=f"allowlist-{uuid.uuid4().hex[:8]}",
        os=OperatingSystem.WINDOWS,
        user=os.environ.get("ALE_TEST_WINDOWS_USER", "user"),
        sudo=False,
        prepared_image=await provider.prepare_image(ImageRef(kind="vm", reference=disk)),
        resources=Resources(cpus=2, memory_mb=8192),
        network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
        proxy_url=proxy.url,
        proxy_token=session.token,
    )
    settings = [*POWERSHELL, OWN_SETTINGS]
    try:
        async with await provider.create(request) as sandbox:
            own = await sandbox.exec(settings, identity=Identity.AGENT)
            await sandbox.close_egress()
            relayed = await sandbox.exec(settings, identity=Identity.AGENT)
            allowed = await sandbox.exec(
                _fetch("https://example.com/"), identity=Identity.AGENT, timeout_sec=120
            )
            refused = await sandbox.exec(
                _fetch("https://www.iana.org/"), identity=Identity.AGENT, timeout_sec=120
            )
            await sandbox.open_egress()
            restored = await sandbox.exec(settings, identity=Identity.AGENT)
    finally:
        await proxy.stop()

    enabled, server, bypass, _ = relayed.stdout.strip().split("|")
    assert (enabled, server.rpartition(":")[0]) == ("1", "127.0.0.1"), relayed.stdout
    assert bypass == "172.30.0.1;localhost;127.0.0.1"
    assert allowed.stdout.strip() == "200", allowed.stdout + allowed.stderr
    assert "(403)" in refused.stdout, refused.stdout + refused.stderr
    # The framework's phases run as the agent's account, so they get its own settings back.
    assert own.ok and restored.stdout == own.stdout
