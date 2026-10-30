"""Antigravity CLI installed in a real sandbox, talking to a scripted Gemini model.

The pinned official release is installed and given a Skill and a stdio MCP server, egress
is sealed, then it launches and resumes twice. The scripted upstream speaks the Gemini
API behind the Gateway, so agy's own requests, the Gateway's credential swap and model
rewrite, the hook that denies web search, the native session files and the parser are
exercised together without a billed model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from ale.core.harness import (
    EffectiveAgentResources,
    HarnessSession,
    ResolvedMcpServer,
    ResolvedSkill,
    TrajectoryParseContext,
)
from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources, StdioMcpServer
from ale.core.trace import read_jsonl
from ale.run.environments.log_mirror import LogMirror
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.antigravity_cli import AntigravityCliHarness
from ale.run.harnesses.base import CONTINUE_PROMPT
from ale.run.providers.docker import DockerProvider
from ale.run.recording import EpisodeRecording
from tests.support import CONTAINER_BASE, prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

HOST_SECRET = "host-only-secret"
MODEL = "gemini-3.8-flash"
SKILL = "resource-proof"
#: A minimal stdio MCP server: newline-delimited JSON-RPC with one ``echo`` tool.
MCP_SERVER = """\
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"].get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "proof", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo",
            "description": "Echo the given text.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }]}
    elif method == "tools/call":
        text = message["params"].get("arguments", {}).get("text", "")
        result = {"content": [{"type": "text", "text": "echo:" + text}]}
    elif method == "ping":
        result = {}
    else:
        reply = {"jsonrpc": "2.0", "id": message["id"],
                 "error": {"code": -32601, "message": "unknown method"}}
        print(json.dumps(reply), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
SHELL = (
    "run_command",
    {"CommandLine": "echo shell-proof", "toolSummary": "proof", "WaitMsBeforeAsync": 5000},
)
MCP = (
    "call_mcp_tool",
    {"ServerName": "proof", "ToolName": "echo", "Arguments": {"text": "mcp-proof"}},
)
SEARCH = ("search_web", {"query": "grounding-proof"})


@dataclass
class ScriptedGemini:
    """Answers the agent loop's requests from a script, in order; others get plain text.

    A request belongs to the agent loop when it offers ``run_command``, which keeps agy's
    own title request off the script.
    """

    steps: list[tuple[str, Any]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    _runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1beta/models/{target}", self._generate)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _generate(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(
            {
                "path": request.path_qs,
                "auth": request.headers.get("x-goog-api-key"),
                "body": body,
            }
        )
        offered = {
            declaration.get("name")
            for tool in body.get("tools") or ()
            for declaration in tool.get("functionDeclarations") or ()
        }
        kind, value = self.steps.pop(0) if SHELL[0] in offered and self.steps else ("text", "ok")
        if kind == "tool":
            parts: list[dict[str, Any]] = [
                {"functionCall": {"name": value[0], "args": value[1]}, "thoughtSignature": "c2ln"}
            ]
        else:
            parts = [{"text": value}]
        chunk = {
            "candidates": [
                {"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "totalTokenCount": 18,
            },
            "modelVersion": MODEL,
        }
        if not request.path.endswith(":streamGenerateContent"):
            return web.json_response(chunk)
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        await response.write(f"data: {json.dumps(chunk)}\r\n\r\n".encode())
        await response.write_eof()
        return response


def _resources(tmp_path: Path) -> EffectiveAgentResources:
    skill = tmp_path / SKILL
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: Proves that declared Skills reach the agent.\n---\n"
        "Reply with the word proof.\n"
    )
    return EffectiveAgentResources(
        skills=(
            ResolvedSkill(
                name=SKILL,
                path=skill,
                source_layers=("task",),
                declared_sources=(f"skills/{SKILL}",),
                digest="sha256:" + "b" * 64,
                reportable=True,
            ),
        ),
        mcp_servers=(
            ResolvedMcpServer(
                name="proof",
                server=StdioMcpServer(
                    name="proof", transport="stdio", command="python3", args=("{home}/mcp.py",)
                ),
                source_layers=("task",),
                declared_sources=("mcp/proof.toml",),
                digest="sha256:" + "c" * 64,
                reportable=True,
            ),
        ),
        digest="sha256:" + "d" * 64,
    )


async def test_agy_runs_through_the_gateway_with_resources_and_resumes(tmp_path: Path) -> None:
    harness = AntigravityCliHarness()
    first_prompt = "Use the shell, the MCP tool and a web search.\nSecond line: 'quoted' $HOME"
    upstream = ScriptedGemini(
        [
            ("tool", SHELL),
            ("tool", MCP),
            ("tool", SEARCH),
            ("text", "FIRST-DONE"),
            ("text", "SECOND-DONE"),
            ("text", "THIRD-DONE"),
        ]
    )
    await upstream.start()
    gateway = Gateway(api_key=HOST_SECRET, upstream=upstream.url, dialect="gemini")
    await gateway.start()
    recording = EpisodeRecording(tmp_path / "episode")
    gateway_session = gateway.open_session(
        GatewaySession(episode_id="agy", model=MODEL), recording.transport
    )
    resources = _resources(tmp_path)
    provider = DockerProvider()
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="agy",
            prepared_image=await prepare_reference(provider, CONTAINER_BASE),
            resources=Resources(cpus=2, memory_mb=4096),
            network=NetworkPolicy(),
            gateway_url=gateway.base_url,
        )
    )
    try:
        await sandbox.open_egress()
        version = await harness.install(sandbox)
        session = HarnessSession(
            episode_id="agy",
            gateway_url=sandbox.gateway_url or gateway.base_url,
            token=gateway_session.token,
            model=MODEL,
            sandbox_id=sandbox.sandbox_id,
            resources_digest=resources.digest,
            home="/home/user",
        )
        await sandbox.write_file("/home/user/mcp.py", MCP_SERVER.encode(), identity=Identity.AGENT)
        harness.validate_resources(resources)
        await harness.install_resources(sandbox, session, resources)
        await sandbox.close_egress()

        first = await harness.launch(first_prompt, sandbox, session, timeout_sec=300)
        assert first.continuation is not None
        assert first.final_message is not None and first.final_message.endswith("FIRST-DONE")
        second = await harness.resume(
            "second", first.continuation, sandbox, session, timeout_sec=300
        )
        assert second.continuation is not None
        assert second.final_message == "SECOND-DONE"
        third = await harness.resume(None, second.continuation, sandbox, session, timeout_sec=300)
        assert third.continuation is not None
        assert third.final_message == "THIRD-DONE"
        assert {item.native_session_id for item in (second.continuation, third.continuation)} == {
            first.continuation.native_session_id
        }

        logs = tmp_path / "logs"
        mirror = LogMirror(
            sandbox, home=session.home, os=session.os, paths=harness.logs, target=logs
        )
        await mirror.sync(final=True)
        assert not mirror.incomplete
    finally:
        await sandbox.destroy()
        gateway.close_session(gateway_session)
        await gateway.stop()
        await upstream.stop()

    # agy reached the model only through the Gateway, which swapped in the host key and
    # named the run's model on every call; the hook kept agy from asking for a search.
    assert upstream.requests
    assert {request["auth"] for request in upstream.requests} == {HOST_SECRET}
    assert {request["path"].split("/")[-1].split(":")[0] for request in upstream.requests} == {
        MODEL
    }
    assert not upstream.steps
    assert not any(
        "googleSearch" in tool
        for request in upstream.requests
        for tool in request["body"].get("tools") or ()
    )
    offered = json.dumps([request["body"] for request in upstream.requests])
    assert SKILL in offered and "# proof" in offered
    transport = read_jsonl(recording.directory / "trace.transport.jsonl").records
    calls = [record for record in transport if record["kind"] == "call"]
    forwarded = [call for call in calls if call["disposition"] == "forwarded"]
    assert len(forwarded) == len(calls) == len(upstream.requests)

    context = TrajectoryParseContext(
        episode_id=session.episode_id,
        trajectory_id="agy-trajectory",
        instruction=first_prompt,
        logs_dir=logs,
        model=MODEL,
        agent_version=version,
        blobs=recording.blobs,
        session_id=third.continuation.native_session_id,
    )
    trajectory = harness.parse_trajectory(context)
    assert harness.parse_trajectory(context) == trajectory
    # Every native file is converted or a copy of what the converted records hold.
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})
    assert [step.message for step in trajectory.steps if step.source == "user"] == [
        first_prompt,
        "second",
        CONTINUE_PROMPT,
    ]
    results = {
        call.function_name: (call, result)
        for step in trajectory.steps
        for call in step.tool_calls or ()
        for result in (step.observation.results if step.observation else ())
        if result.source_call_id == call.tool_call_id
    }
    assert "shell-proof" in json.dumps(results["run_command"][1].content)
    mcp_call, mcp_result = results["mcp__proof__echo"]
    assert (mcp_call.extra or {})["ale"]["mcp"] == {"server": "proof", "tool": "echo"}
    assert "echo:mcp-proof" in json.dumps(mcp_result.content)
    assert "denied by pre-tool hook" in json.dumps(results["search_web"][1].content)
    assert [step.message for step in trajectory.steps if step.source == "agent"][-3:] == [
        "FIRST-DONE",
        "SECOND-DONE",
        "THIRD-DONE",
    ]
