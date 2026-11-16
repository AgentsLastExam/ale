"""A sealed agent reaches its allowlist through guestd's relay, from its commands and from
its desktop's system proxy alike, and the account gets its own settings back."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.sandbox import Identity, ImageRef, PreparedTaskImage, Provider, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.providers import ProviderRegistry
from ale.run.providers.docker import DockerProvider
from ale.run.providers.qemu import QemuProvider
from ale.run.task_images import prepare_task_image, resolve_image_kinds
from ale.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, VM_BASE, VM_RESOURCES, prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

POLICY = NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",))


@asynccontextmanager
async def _proxy() -> AsyncIterator[tuple[EgressProxy, str]]:
    sessions = SessionRegistry()
    session = sessions.open(
        GatewaySession(
            episode_id="allowlist", model="none", allowed_hosts=frozenset({"example.com"})
        )
    )
    proxy = EgressProxy(sessions, host="0.0.0.0")
    await proxy.start()
    try:
        yield proxy, session.token
    finally:
        await proxy.stop()


#: The desktop's session bus, where a VM's systemd keeps it if guestd named none.
BUS = (
    'bus="unix:path=/run/user/$(id -u)/bus"; '
    'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-$bus}"; '
)
#: The agent's GNOME proxy settings, one line.
GNOME = BUS + (
    "{ for key in mode ignore-hosts; do gsettings get org.gnome.system.proxy $key; done; "
    "for scheme in http https; do for key in host port; do "
    "gsettings get org.gnome.system.proxy.$scheme $key; done; done; } | paste -sd'|'"
)
#: The declared host through the proxy the desktop's settings name, the variables unset.
THROUGH_SETTINGS = BUS + (
    "port=$(gsettings get org.gnome.system.proxy.https port); "
    "env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy "
    "curl -sS -o /dev/null -w '%{http_code}' --proxy http://127.0.0.1:$port "
    "https://example.com/"
)
#: What Python's urllib gets, which sends no proxy credentials without a password.
URLLIB = (
    "import sys, urllib.request\n"
    "try:\n"
    "    print(urllib.request.urlopen(sys.argv[1], timeout=60).status)\n"
    "except Exception as error:\n"
    "    print(error)\n"
)


async def _check_linux_desktop(
    provider: Provider, image: PreparedTaskImage, resources: Resources, host: str
) -> None:
    async with _proxy() as (proxy, token):
        request = SandboxRequest(
            episode_id=f"allowlist-{uuid.uuid4().hex[:8]}",
            prepared_image=image,
            resources=resources,
            network=POLICY,
            proxy_url=proxy.url,
            proxy_token=token,
        )
        async with await provider.create(request) as sandbox:
            own = await sandbox.exec(["sh", "-c", GNOME], identity=Identity.AGENT)
            await sandbox.close_egress()
            relayed = await sandbox.exec(["sh", "-c", GNOME], identity=Identity.AGENT)
            variables = await sandbox.exec(
                ["sh", "-c", 'echo "$HTTPS_PROXY"'], identity=Identity.AGENT
            )
            settings = await sandbox.exec(
                ["sh", "-c", THROUGH_SETTINGS],
                identity=Identity.AGENT,
                timeout_sec=120,
            )
            allowed = await sandbox.exec(
                ["python3", "-c", URLLIB, "https://example.com/"],
                identity=Identity.AGENT,
                timeout_sec=120,
            )
            refused = await sandbox.exec(
                ["python3", "-c", URLLIB, "https://www.iana.org/"],
                identity=Identity.AGENT,
                timeout_sec=120,
            )
            await sandbox.open_egress()
            restored = await sandbox.exec(["sh", "-c", GNOME], identity=Identity.AGENT)

    mode, hosts, http_host, http_port, https_host, https_port = relayed.stdout.strip().split("|")
    assert (mode, http_host, https_host) == ("'manual'", "'127.0.0.1'", "'127.0.0.1'")
    assert http_port == https_port
    assert hosts == f"['{host}', 'localhost', '127.0.0.1']"
    # The commands' variables name the same relay, with no token in them.
    assert variables.stdout.strip() == f"http://127.0.0.1:{https_port}"
    assert settings.stdout.strip() == "200", settings.stdout + settings.stderr
    assert allowed.stdout.strip() == "200", allowed.stdout + allowed.stderr
    assert "403" in refused.stdout, refused.stdout + refused.stderr
    assert own.ok and restored.stdout == own.stdout


@pytest.mark.needs_gui
@pytest.mark.asyncio
async def test_a_sealed_container_desktop_names_the_relay_in_its_gnome_settings() -> None:
    provider = DockerProvider()
    image = await prepare_reference(provider, CONTAINER_BASE)
    await _check_linux_desktop(
        provider, image, Resources(cpus=2, memory_mb=2048), "ale-gateway.internal"
    )


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_sealed_vm_desktop_names_the_relay_in_its_gnome_settings(
    tmp_path: Path, write_repo: Callable[..., Path], docker_buildx: None
) -> None:
    task = write_repo(
        tmp_path / "repo", config={"environment": {"image": VM_BASE}}, dockerfile=None
    )
    tasks = load_tasks(task)
    await resolve_image_kinds(tasks)
    image = await prepare_task_image(tasks[0], ProviderRegistry(RunConfig()))
    provider = QemuProvider(overlay_dir=tmp_path / "overlays")
    await _check_linux_desktop(provider, image, Resources(**VM_RESOURCES), "172.30.0.1")


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


@pytest.mark.needs_kvm
@pytest.mark.asyncio
async def test_a_sealed_windows_agent_reaches_its_allowlist_through_the_system_proxy(
    tmp_path: Path,
) -> None:
    disk = os.environ.get("ALE_TEST_WINDOWS_IMAGE")
    if not disk or not Path(disk).is_file():
        pytest.skip("set ALE_TEST_WINDOWS_IMAGE to an ALE Windows base disk")
    provider = QemuProvider(image=Path(disk), overlay_dir=tmp_path / "overlays")
    settings = [*POWERSHELL, OWN_SETTINGS]
    async with _proxy() as (proxy, token):
        request = SandboxRequest(
            episode_id=f"allowlist-{uuid.uuid4().hex[:8]}",
            os=OperatingSystem.WINDOWS,
            user=os.environ.get("ALE_TEST_WINDOWS_USER", "user"),
            sudo=False,
            prepared_image=await provider.prepare_image(ImageRef(kind="vm", reference=disk)),
            resources=Resources(cpus=2, memory_mb=8192),
            network=POLICY,
            proxy_url=proxy.url,
            proxy_token=token,
        )
        async with await provider.create(request) as sandbox:
            own = await sandbox.exec(settings, identity=Identity.AGENT)
            await sandbox.close_egress()
            relayed = await sandbox.exec(settings, identity=Identity.AGENT)
            variables = await sandbox.exec(
                [*POWERSHELL, "$env:HTTPS_PROXY"], identity=Identity.AGENT
            )
            allowed = await sandbox.exec(
                _fetch("https://example.com/"), identity=Identity.AGENT, timeout_sec=120
            )
            refused = await sandbox.exec(
                _fetch("https://www.iana.org/"), identity=Identity.AGENT, timeout_sec=120
            )
            await sandbox.open_egress()
            restored = await sandbox.exec(settings, identity=Identity.AGENT)

    enabled, server, bypass, _ = relayed.stdout.strip().split("|")
    assert (enabled, server.rpartition(":")[0]) == ("1", "127.0.0.1"), relayed.stdout
    assert bypass == "172.30.0.1;localhost;127.0.0.1"
    assert variables.stdout.strip() == f"http://{server}"
    assert allowed.stdout.strip() == "200", allowed.stdout + allowed.stderr
    assert "(403)" in refused.stdout, refused.stdout + refused.stderr
    # The framework's phases run as the agent's account, so they get its own settings back.
    assert own.ok and restored.stdout == own.stdout
