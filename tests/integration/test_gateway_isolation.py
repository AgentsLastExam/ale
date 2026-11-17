"""The two halves of the isolation bargain, together.

Deny-all networking and a controlled egress are each easy to get right alone and easy
to get wrong together: a sandbox that cannot reach the gateway is useless, and one that
can reach anything else is not isolated. This exercises both properties against a real
container and a real gateway process.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from ale.core.sandbox import Identity, Sandbox, SandboxRequest
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.providers.docker import DockerProvider
from tests.support import (
    CONTAINER_BASE,
    prepare_reference,
    skip_without_gvisor,
    skip_without_kata,
)

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = CONTAINER_BASE


@pytest.fixture(
    params=[
        "runc",
        pytest.param("kata", marks=pytest.mark.needs_kvm),
        "gvisor",
        "gvisor-nested",
    ]
)
def sandboxes(request: pytest.FixtureRequest) -> tuple[DockerProvider, Resources]:
    """Each Docker runtime, since each confines the sandbox its own way, and gVisor's for
    container engines, under which the Gateway test runs one that sets its rules in
    netstack's nf_tables: the leaf and what its Sandbox asks for."""
    runtime, _, engine = request.param.partition("-")
    if runtime == "kata":
        skip_without_kata()
    if runtime == "gvisor":
        skip_without_gvisor()
    if not engine:
        return DockerProvider(runtime=runtime), Resources(cpus=1, memory_mb=512)
    # The engine installs and runs beside the sandbox's own processes.
    features = (ContainerFeature.NESTED_CONTAINERS,)
    return DockerProvider(runtime=runtime), Resources(
        cpus=1, memory_mb=2048, container_features=features
    )


#: Installs Docker from the image's distribution, starts it and runs a container that
#: publishes a port: its rules are in netstack's nf_tables, beside ALE's chains in its
#: iptables tables.
_ENGINE = """
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq docker.io >/dev/null || exit 2
(dockerd >/tmp/dockerd.log 2>&1 &)
for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker run -d -p 18080:80 busybox:1.36.1 httpd -f -p 80 >/dev/null
"""


async def _run_an_engine(sandbox: Sandbox) -> None:
    """Install and start a container engine, with egress open only while it installs."""
    await sandbox.open_egress()
    started = await sandbox.exec(["sh", "-c", _ENGINE], timeout_sec=600)
    assert started.ok, started.stderr[-2000:]
    await sandbox.close_egress()


async def _stub_upstream() -> tuple[web.AppRunner, str]:
    async def messages(request: web.Request) -> web.Response:
        return web.json_response(
            {"type": "message", "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_sandbox_reaches_the_gateway_and_nothing_else(
    sandboxes: tuple[DockerProvider, Resources],
) -> None:
    provider, resources = sandboxes
    upstream_runner, upstream_url = await _stub_upstream()
    gateway = Gateway(api_key="host-only-credential", upstream=upstream_url, host="0.0.0.0")
    await gateway.start()
    session = gateway.open_session(GatewaySession(episode_id="iso", model="claude-opus-4-8"))

    prepared = await prepare_reference(provider, IMAGE)
    request = SandboxRequest(
        episode_id="iso",
        prepared_image=prepared,
        resources=resources,
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
        gateway_url=gateway.base_url,
    )

    try:
        async with await provider.create(request) as sandbox:
            if resources.container_features:
                await _run_an_engine(sandbox)
            script = (
                "import json,os,urllib.request\n"
                "url=os.environ['ALE_GATEWAY_URL']\n"
                "req=urllib.request.Request(\n"
                "    url+'/v1/messages',\n"
                "    data=json.dumps({'messages':[]}).encode(),\n"
                "    headers={'authorization':'Bearer '+os.environ['TOKEN'],\n"
                "             'content-type':'application/json'})\n"
                "print('gateway', urllib.request.urlopen(req, timeout=20).status)\n"
            )
            allowed = await sandbox.exec(
                ["python3", "-c", script],
                env={"TOKEN": session.token},
                timeout_sec=60,
            )
            blocked = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    "import socket;socket.setdefaulttimeout(5);"
                    "socket.create_connection(('1.1.1.1', 443))",
                ],
                timeout_sec=30,
            )
            credential_hunt = await sandbox.exec(
                ["sh", "-c", "env | grep -c 'host-only-credential' || true"],
                timeout_sec=30,
            )

        assert allowed.ok, f"the sandbox could not reach its gateway: {allowed.stderr[-300:]}"
        assert "gateway 200" in allowed.stdout
        assert blocked.exit_code != 0, "the sandbox reached the internet directly"
        assert credential_hunt.stdout.strip() == "0", "the real credential leaked into the sandbox"
    finally:
        await gateway.stop()
        await upstream_runner.cleanup()


@pytest.mark.asyncio
async def test_sandbox_reaches_https_through_the_episode_proxy(
    sandboxes: tuple[DockerProvider, Resources],
) -> None:
    provider, resources = sandboxes
    registry = SessionRegistry()
    proxy = EgressProxy(registry, host="0.0.0.0")
    proxy_url = await proxy.start()
    session = registry.open(
        GatewaySession(
            episode_id="proxy",
            model="test",
            allowed_hosts=frozenset({"api.anthropic.com"}),
        )
    )
    prepared = await prepare_reference(provider, IMAGE)
    request = SandboxRequest(
        episode_id="proxy",
        prepared_image=prepared,
        resources=resources,
        network=NetworkPolicy(
            mode=NetworkMode.ALLOWLIST,
            allowed_hosts=("api.anthropic.com",),
        ),
        proxy_url=proxy_url,
        proxy_token=session.token,
    )

    try:
        async with await provider.create(request) as sandbox:
            await sandbox.close_egress()
            result = await sandbox.exec(
                [
                    "curl",
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "https://api.anthropic.com",
                ],
                identity=Identity.AGENT,
                timeout_sec=30,
            )
        assert result.ok, result.stderr
        assert result.stdout.strip().isdigit()
    finally:
        await proxy.stop()
