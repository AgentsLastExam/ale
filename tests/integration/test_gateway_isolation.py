"""The two halves of the isolation bargain, together.

Deny-all networking and a controlled egress are each easy to get right alone and easy
to get wrong together: a sandbox that cannot reach the gateway is useless, and one that
can reach anything else is not isolated. This exercises both properties against a real
container and a real gateway process.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.gateway.proxy import EgressProxy
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession, SessionRegistry
from ale.run.providers.docker import DockerProvider
from tests.support import prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

IMAGE = "ghcr.io/agentslastexam/sandbox-base-cli:latest"


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
async def test_sandbox_reaches_the_gateway_and_nothing_else() -> None:
    upstream_runner, upstream_url = await _stub_upstream()
    gateway = Gateway(api_key="host-only-credential", upstream=upstream_url, host="0.0.0.0")
    await gateway.start()
    session = gateway.open_session(GatewaySession(episode_id="iso", model="claude-opus-4-8"))

    provider = DockerProvider()
    prepared = await prepare_reference(provider, IMAGE)
    request = SandboxRequest(
        episode_id="iso",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
        gateway_url=gateway.base_url,
    )

    try:
        async with await provider.create(request) as sandbox:
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
async def test_sandbox_reaches_https_through_the_episode_proxy() -> None:
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
    provider = DockerProvider()
    prepared = await prepare_reference(provider, IMAGE)
    request = SandboxRequest(
        episode_id="proxy",
        prepared_image=prepared,
        resources=Resources(cpus=1, memory_mb=512),
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
