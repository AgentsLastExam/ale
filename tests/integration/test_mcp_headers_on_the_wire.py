"""Each pinned CLI, really installed, sends a remote MCP server's declared headers.

The server is a small Streamable HTTP endpoint on the Sandbox's loopback that records what
it receives, so the assertion is about the request itself rather than the generated file.
The model is scripted and answers at once: a CLI lists its MCP tools before that.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from ale.core.harness import EffectiveAgentResources, HarnessSession, ResolvedMcpServer
from ale.core.sandbox import Identity
from ale.core.taskspec import OperatingSystem, StreamableHttpMcpServer
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.base import CliHarness
from ale.run.harnesses.claude_code import ClaudeCodeHarness
from ale.run.harnesses.codex_cli import CodexCliHarness
from ale.run.harnesses.droid import DroidHarness
from ale.run.harnesses.forgecode import ForgecodeHarness
from ale.run.harnesses.grok_build import GrokBuildHarness
from ale.run.harnesses.hermes import HermesHarness
from ale.run.harnesses.kimi_code import KimiCodeHarness
from ale.run.harnesses.muse_code import MuseCodeHarness
from ale.run.harnesses.openclaw_cli import OpenClawCliHarness
from ale.run.harnesses.openhands_cli import OpenHandsCliHarness
from ale.run.harnesses.pi_cli import PiCliHarness
from ale.run.harnesses.qwen_code import QwenCodeHarness
from ale.run.harnesses.zcode import ZCodeHarness
from ale.run.recording import EpisodeRecording
from tests.integration.scripted_model import ScriptedModel
from tests.support import cli_os, cli_sandbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_cli_install,
    pytest.mark.needs_orchard if cli_os() is OperatingSystem.MACOS else pytest.mark.needs_docker,
]

HEADER = "Bearer sk-wire-proof"
PORT = 18765
#: Beside the server, in the agent's home.
LOG = "mcp-headers.txt"
SERVER = f"""\
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), {LOG!r})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._empty(405)

    def do_DELETE(self):
        self._empty(200)

    def do_POST(self):
        with open(LOG, "a") as log:
            log.write(str(self.headers.get("Authorization")) + "\\n")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in body:
            self._empty(202)
            return
        method = body.get("method")
        if method == "initialize":
            result = {{
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "wire", "version": "1"}},
            }}
        elif method == "tools/list":
            schema = {{"type": "object", "properties": {{}}}}
            result = {{"tools": [{{"name": "probe", "description": "d", "inputSchema": schema}}]}}
        else:
            result = {{}}
        reply = json.dumps({{"jsonrpc": "2.0", "id": body["id"], "result": result}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


ThreadingHTTPServer(("127.0.0.1", {PORT}), Handler).serve_forever()
"""

WAIT_FOR_SERVER = (
    f"for _ in $(seq 100); do python3 -c 'import socket; "
    f'socket.create_connection(("127.0.0.1", {PORT}), 1)\' 2>/dev/null && exit 0; sleep 0.1; done; '
    "exit 1"
)

CASES: dict[str, tuple[Callable[[], CliHarness], str, str]] = {
    "claude-code": (ClaudeCodeHarness, "anthropic", "scripted-model"),
    "codex-cli": (CodexCliHarness, "openai-responses", "scripted-model"),
    "droid": (partial(DroidHarness, gateway_dialect="anthropic"), "anthropic", "scripted-model"),
    "forgecode": (
        partial(ForgecodeHarness, gateway_dialect="anthropic"),
        "anthropic",
        "scripted-model",
    ),
    "grok-build": (GrokBuildHarness, "openai-responses", "scripted-model"),
    "hermes": (partial(HermesHarness, gateway_dialect="anthropic"), "anthropic", "scripted-model"),
    "kimi-code": (
        partial(KimiCodeHarness, gateway_dialect="anthropic"),
        "anthropic",
        "scripted-model",
    ),
    "muse-code": (
        partial(MuseCodeHarness, settings={"reminders": False}),
        "openai-responses",
        "scripted-model",
    ),
    "openclaw-cli": (OpenClawCliHarness, "openai-responses", "scripted-model"),
    "openhands-cli": (
        partial(OpenHandsCliHarness, gateway_dialect="anthropic"),
        "anthropic",
        "scripted-model",
    ),
    "pi-cli": (partial(PiCliHarness, gateway_dialect="anthropic"), "anthropic", "scripted-model"),
    "qwen-code": (
        partial(QwenCodeHarness, gateway_dialect="anthropic"),
        "anthropic",
        "scripted-model",
    ),
    "zcode": (partial(ZCodeHarness, gateway_dialect="anthropic"), "anthropic", "scripted-model"),
}


def _resources() -> EffectiveAgentResources:
    return EffectiveAgentResources(
        mcp_servers=(
            ResolvedMcpServer(
                name="wire",
                server=StreamableHttpMcpServer(
                    name="wire",
                    transport="streamable-http",
                    url=f"http://127.0.0.1:{PORT}/mcp",
                ),
                source_layers=("task",),
                declared_sources=("[[environment.mcp_servers]]",),
                digest="sha256:" + "c" * 64,
                reportable=False,
                headers={"Authorization": HEADER},
            ),
        ),
        digest="sha256:" + "d" * 64,
    )


@pytest.mark.parametrize("name", sorted(CASES))
async def test_cli_sends_a_remote_server_s_headers(tmp_path: Path, name: str) -> None:
    factory, dialect, model_name = CASES[name]
    harness = factory()
    model = ScriptedModel(tool_pattern=r"^$")
    upstream = await model.start()
    recording = EpisodeRecording(tmp_path)
    gateway = Gateway(api_key="host-only", upstream=upstream, dialect=dialect, host="0.0.0.0")
    await gateway.start()
    gateway_session = gateway.open_session(
        GatewaySession(episode_id=f"wire-{name}", model=model_name), recording.transport
    )
    try:
        async with cli_sandbox(f"wire-{name}", gateway.base_url, tmp_path) as (sandbox, home):
            await sandbox.open_egress()
            await harness.install(sandbox)
            session = HarnessSession(
                episode_id=f"wire-{name}",
                gateway_url=sandbox.gateway_url or gateway.base_url,
                token=gateway_session.token,
                model=model_name,
                sandbox_id=sandbox.sandbox_id,
                home=home,
                os=cli_os(),
            )
            resources = _resources()
            harness.validate_resources(resources)
            await harness.install_resources(sandbox, session, resources)
            await sandbox.close_egress()
            await sandbox.write_file(f"{home}/wire.py", SERVER.encode(), identity=Identity.AGENT)
            await sandbox.exec(
                ["sh", "-c", f"nohup python3 {home}/wire.py >/dev/null 2>&1 &"],
                identity=Identity.AGENT,
            )
            # A CLI connects once, at start, so the server is up before one launches.
            ready = await sandbox.exec(["sh", "-c", WAIT_FOR_SERVER], identity=Identity.AGENT)
            assert ready.ok, ready.stderr
            await harness.launch("Reply at once.", sandbox, session, timeout_sec=300)
            seen = await sandbox.exec(["cat", f"{home}/{LOG}"], identity=Identity.AGENT)
    finally:
        gateway.close_session(gateway_session)
        await gateway.stop()
        await model.stop()

    assert seen.ok, seen.stderr
    assert set(seen.stdout.split("\n")) - {""} == {HEADER}
