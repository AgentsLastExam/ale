"""Opt-in Orchard qualification against the shared Sandbox contract.

Run on a Mac whose local Orchard ``scripts/setup_macos.py`` keeps running, with
ALE_TEST_ORCHARD=1. The suite builds its image on that Mac once, as ``ale prepare`` would,
from ``tests/fixtures/tasks/macos-unprivileged``: it signs in ``tester``, an account other
than ``admin``, so the sudo=false cases apply. Every macOS sandbox has that account's
desktop, so the desktop capture and input checks always run. Each test destroys the VMs and
snapshots it creates; none adopts a VM it did not create.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.sandbox import Identity, PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.core.testkit import ProviderConformance
from ale.run.framework import MACOS_PYTHON
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.providers import ProviderRegistry
from ale.run.providers.orchard import OrchardProvider
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks

pytestmark = [
    pytest.mark.conformance,
    pytest.mark.needs_orchard,
    pytest.mark.skipif(
        os.environ.get("ALE_TEST_ORCHARD") != "1",
        reason="set ALE_TEST_ORCHARD=1 on a Mac whose local Orchard setup keeps running",
    ),
]

TASK = Path(__file__).resolve().parents[1] / "fixtures/tasks/macos-unprivileged"


@pytest.fixture(scope="module")
def unprivileged() -> tuple[PreparedTaskImage, str]:
    """The fixture Task's image, built on its base the first time, and its account."""
    task = load_tasks(TASK)[0]
    image = asyncio.run(prepare_task_image(task, ProviderRegistry(RunConfig())))
    return image, task.spec.image.user


class TestOrchardProvider(ProviderConformance):
    """Shared file, identity, lifecycle, retention, resource, network and desktop guarantees.

    Sandboxes are sealed as ``no-network`` unless a case asks otherwise, so the Mac needs
    the Softnet setup installs on macOS 26 or later.
    """

    @pytest.fixture(autouse=True)
    def configure(self, unprivileged: tuple[PreparedTaskImage, str]) -> None:
        self.provider = OrchardProvider()
        self.image, self.user = unprivileged
        self.gui_image = self.image

    async def request(self, **overrides: object) -> SandboxRequest:
        resources = overrides.get("resources", Resources())
        assert isinstance(resources, Resources)
        overrides["resources"] = resources.model_copy(
            update={"cpus": max(2, resources.cpus), "memory_mb": max(4096, resources.memory_mb)}
        )
        overrides.setdefault("os", OperatingSystem.MACOS)
        overrides.setdefault("user", self.user)
        return await super().request(**overrides)

    @pytest.mark.asyncio
    async def test_a_sealed_allowlist_desktop_names_the_relay_in_its_system_proxy(self) -> None:
        """While sealed, the network services' web proxies name guestd's relay, through which
        a client of the system proxy (Python's urllib, reading it as Safari does) reaches the
        declared host and nothing else; open, the system proxy is what it was."""
        sessions = SessionRegistry()
        session = sessions.open(
            GatewaySession(
                episode_id="allowlist", model="none", allowed_hosts=frozenset({"example.com"})
            )
        )
        proxy = EgressProxy(sessions, host="0.0.0.0")
        await proxy.start()
        try:
            request = await self.request(
                network=NetworkPolicy(mode=NetworkMode.ALLOWLIST, allowed_hosts=("example.com",)),
                proxy_url=proxy.url,
                proxy_token=session.token,
            )
            async with await self.provider.create(request) as sandbox:
                agent = Identity.AGENT
                own = await sandbox.exec(["/usr/sbin/scutil", "--proxy"], identity=agent)
                await sandbox.close_egress()
                relayed = await sandbox.exec(["/usr/sbin/scutil", "--proxy"], identity=agent)
                fetched = {
                    url: await sandbox.exec(
                        [*_WITHOUT_PROXY_VARIABLES, MACOS_PYTHON, "-I", "-c", _FETCH, url],
                        identity=agent,
                        timeout_sec=120,
                    )
                    for url in ("https://example.com/", "https://www.iana.org/")
                }
                direct = await sandbox.exec(
                    ["/usr/bin/curl", "-sS", "--noproxy", "*", "-m", "20", "https://example.com/"],
                    identity=agent,
                    timeout_sec=60,
                )
                await sandbox.open_egress()
                restored = await sandbox.exec(["/usr/sbin/scutil", "--proxy"], identity=agent)
        finally:
            await proxy.stop()
        for line in ("HTTPEnable : 1", "HTTPSEnable : 1", "HTTPSProxy : 127.0.0.1"):
            assert line in relayed.stdout, relayed.stdout
        allowed, refused = fetched.values()
        assert allowed.stdout.strip() == "200", allowed.stdout + allowed.stderr
        assert "403" in refused.stdout, refused.stdout + refused.stderr
        assert not direct.ok, direct.stdout
        # networksetup cannot take a web proxy's server away, only turn the proxy off, so one
        # the account never set comes back off, with the relay's address unused.
        assert own.ok and _in_effect(restored.stdout) == _in_effect(own.stdout)


def _in_effect(proxies: str) -> list[str]:
    """``scutil --proxy`` as it takes effect: a web proxy turned off is one not set."""
    return [
        line
        for line in proxies.splitlines()
        if not re.search(r"HTTPS?(Proxy|Port) :|HTTPS?Enable : 0", line)
    ]


#: The agent's commands carry the relay in their proxy variables; without any of them,
#: ``NO_PROXY`` included, Python's urllib takes the system proxy, as a browser does.
_WITHOUT_PROXY_VARIABLES = [
    "/usr/bin/env",
    *(f"-u{name}" for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")),
    *(f"-u{name}" for name in ("https_proxy", "http_proxy", "no_proxy")),
]
_FETCH = (
    "import sys, urllib.request\n"
    "try:\n"
    "    print(urllib.request.urlopen(sys.argv[1], timeout=60).status)\n"
    "except Exception as error:\n"
    "    print(error)\n"
)
