"""Vendor CLIs installed in a real sandbox, talking to a scripted model through the Gateway.

Each case installs the pinned official program, stages a Skill and a stdio MCP server,
has the program check the generated configuration, seals egress, then launches and
resumes twice. The scripted upstream answers in the selected Gateway dialect, so the
program's own requests, the Gateway's credential swap, the native session files and the
parser are exercised together without a billed model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
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
from ale.core.sandbox import Identity, Sandbox, SandboxRequest
from ale.core.taskspec import NetworkPolicy, Resources, StdioMcpServer
from ale.core.trace import read_jsonl
from ale.run.environments.log_mirror import LogMirror
from ale.run.gateway.server import Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.harnesses.base import CONTINUE_PROMPT, CliHarness
from ale.run.harnesses.kimi_code import KimiCodeHarness
from ale.run.harnesses.muse_code import MuseCodeHarness
from ale.run.harnesses.qwen_code import QwenCodeHarness
from ale.run.harnesses.zcode import ZCodeHarness
from ale.run.providers.docker import DockerProvider
from ale.run.recording import EpisodeRecording
from tests.support import CONTAINER_BASE, prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_cli_install]

HOST_SECRET = "host-only-secret"
MODEL = "scripted-model"
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


@dataclass
class ScriptedUpstream:
    """Answers the agent loop's requests from a script, in order; others get plain text.

    A request belongs to the agent loop when it offers the ``marker`` tool, which keeps a
    CLI's own side requests, such as titling or memory extraction, off the script.
    """

    steps: list[tuple[str, Any]]
    marker: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    _runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/messages", self._anthropic)
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_post("/v1/responses", self._responses)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _next(self, request: web.Request) -> tuple[dict[str, Any], tuple[str, Any]]:
        body = await request.json()
        self.requests.append(
            {
                "path": request.path,
                "auth": request.headers.get("x-api-key")
                or request.headers.get("authorization", "").removeprefix("Bearer "),
                "body": body,
            }
        )
        offered = {
            tool.get("name") or (tool.get("function") or {}).get("name")
            for tool in body.get("tools") or ()
        }
        if self.marker in offered and self.steps:
            return body, self.steps.pop(0)
        return body, ("text", "ok")

    async def _sse(self, request: web.Request, events: list[str]) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        for event in events:
            await response.write(event.encode())
        await response.write_eof()
        return response

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body, (kind, value) = await self._next(request)
        if kind == "tool":
            delta: dict[str, Any] = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {"name": value[0], "arguments": json.dumps(value[1])},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": value}, "stop"
        if not body.get("stream"):
            message = {"role": "assistant", "content": delta.get("content")}
            if kind == "tool":
                message["tool_calls"] = [
                    {key: item[key] for key in ("id", "type", "function")}
                    for item in delta["tool_calls"]
                ]
            return web.json_response(
                {
                    "id": "chatcmpl-scripted",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
            )

        def chunk(choices: list[dict[str, Any]], **extra: Any) -> str:
            value = {
                "id": "chatcmpl-scripted",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": choices,
                **extra,
            }
            return f"data: {json.dumps(value)}\n\n"

        return await self._sse(
            request,
            [
                chunk([{"index": 0, "delta": {"role": "assistant", "content": ""}}]),
                chunk([{"index": 0, "delta": delta}]),
                chunk([{"index": 0, "delta": {}, "finish_reason": finish}]),
                chunk([], usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}),
                "data: [DONE]\n\n",
            ],
        )

    async def _anthropic(self, request: web.Request) -> web.StreamResponse:
        _, (kind, value) = await self._next(request)

        def event(name: str, data: dict[str, Any]) -> str:
            return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

        if kind == "tool":
            block = {"type": "tool_use", "id": f"toolu_{len(self.requests)}", "name": value[0]}
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(value[1])}
            stop = "tool_use"
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": value}
            stop = "end_turn"
        message = {
            "id": f"msg_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 11, "output_tokens": 0},
        }
        return await self._sse(
            request,
            [
                event("message_start", {"message": message}),
                event("content_block_start", {"index": 0, "content_block": start}),
                event("content_block_delta", {"index": 0, "delta": delta}),
                event("content_block_stop", {"index": 0}),
                event(
                    "message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 7}}
                ),
                event("message_stop", {}),
            ],
        )

    async def _responses(self, request: web.Request) -> web.StreamResponse:
        _, (kind, value) = await self._next(request)
        number = len(self.requests)
        sequence = iter(range(1 << 16))

        def event(data: dict[str, Any]) -> str:
            data = {**data, "sequence_number": next(sequence)}
            return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

        if kind == "tool":
            item = {
                "type": "function_call",
                "id": f"fc_{number}",
                "call_id": f"call_{number}",
                "name": value[0],
                "arguments": json.dumps(value[1]),
                "status": "completed",
            }
            middle = [
                event(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "arguments": "", "status": "in_progress"},
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "delta": item["arguments"],
                    }
                ),
                event(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": 0,
                        "arguments": item["arguments"],
                    }
                ),
            ]
        else:
            item = {
                "type": "message",
                "id": f"msg_{number}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": value, "annotations": []}],
            }
            located = {"item_id": item["id"], "output_index": 0, "content_index": 0}
            middle = [
                event(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "content": [], "status": "in_progress"},
                    }
                ),
                event(
                    {
                        "type": "response.content_part.added",
                        **located,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }
                ),
                event({"type": "response.output_text.delta", **located, "delta": value}),
                event({"type": "response.output_text.done", **located, "text": value}),
                event(
                    {
                        "type": "response.content_part.done",
                        **located,
                        "part": item["content"][0],
                    }
                ),
            ]
        response = {
            "id": f"resp_{number}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": MODEL,
            "output": [item],
            "usage": {
                "input_tokens": 11,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 18,
            },
        }
        return await self._sse(
            request,
            [
                event(
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    }
                ),
                *middle,
                event({"type": "response.output_item.done", "output_index": 0, "item": item}),
                event({"type": "response.completed", "response": response}),
            ],
        )


@dataclass(frozen=True)
class Case:
    factory: Callable[[str], CliHarness]
    dialect: str
    shell: tuple[str, dict[str, Any]]
    mcp_tool: str
    check_config: Callable[[Sandbox, HarnessSession], Awaitable[None]]
    #: The native calls that reach the MCP tool, when the CLI defers it behind others.
    mcp_calls: tuple[tuple[str, Any], ...] = ()
    #: The offered tool that marks an agent-loop request, when it is not the shell itself.
    marker: str | None = None


async def _kimi_doctor(sandbox: Sandbox, session: HarnessSession) -> None:
    result = await sandbox.exec(
        ["sh", "-c", "kimi doctor config"],
        env={
            "PATH": f"{session.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "KIMI_CODE_HOME": f"{session.home}/.kimi-ale",
        },
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    assert result.ok, result.stdout + result.stderr
    assert "All checked config files are valid." in result.stdout


async def _qwen_mcp_list(sandbox: Sandbox, session: HarnessSession) -> None:
    home = f"{session.home}/.qwen-ale"
    result = await sandbox.exec(
        ["sh", "-c", "qwen mcp list"],
        env={
            "PATH": f"{session.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "QWEN_HOME": home,
            "QWEN_RUNTIME_DIR": home,
            "QWEN_CODE_SYSTEM_SETTINGS_PATH": f"{home}/system-settings.json",
            "QWEN_CODE_SYSTEM_DEFAULTS_PATH": f"{home}/system-defaults.json",
            "QWEN_CODE_MODELS_DEV_REFRESH": "off",
            "NO_COLOR": "1",
        },
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    assert result.ok, result.stdout + result.stderr
    assert "proof: python3 /home/user/mcp.py (stdio) - Connected" in result.stdout


async def _muse_skills(sandbox: Sandbox, session: HarnessSession) -> None:
    result = await sandbox.exec(
        [f"{session.home}/.local/bin/muse", "skills", "list", "--json"],
        env={
            "XDG_CONFIG_HOME": f"{session.home}/.config",
            "XDG_DATA_HOME": f"{session.home}/.local/share",
        },
        cwd=session.home,
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    assert result.ok, result.stdout + result.stderr
    listed = json.loads(result.stdout)
    assert SKILL in {skill.get("name") for skill in listed["skills"]}


async def _zcode_skills(sandbox: Sandbox, session: HarnessSession) -> None:
    result = await sandbox.exec(
        [
            "node",
            f"{session.home}/.local/lib/zcode/zcode.cjs",
            "skills",
            "list",
            "--cwd",
            session.home,
            "--json",
        ],
        env={
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": (
                f"{session.home}/.local/lib/zcode/zcode-builtin.json"
            ),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": f"{session.home}/.zcode-ale/provider.json",
        },
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    assert result.ok, result.stdout + result.stderr
    listed = json.loads(result.stdout)
    assert SKILL in {skill.get("name") for skill in listed["skills"]}


DIALECTS = ("openai-chat-completions", "anthropic", "openai-responses")
CASES = {
    **{
        f"kimi-code-{dialect}": Case(
            factory=lambda dialect: KimiCodeHarness(gateway_dialect=dialect),
            dialect=dialect,
            shell=("Bash", {"command": "echo shell-proof"}),
            mcp_tool="mcp__proof__echo",
            check_config=_kimi_doctor,
        )
        for dialect in DIALECTS
    },
    **{
        f"qwen-code-{dialect}": Case(
            factory=lambda dialect: QwenCodeHarness(gateway_dialect=dialect),
            dialect=dialect,
            shell=("run_shell_command", {"command": "echo shell-proof"}),
            mcp_tool="mcp__proof__echo",
            check_config=_qwen_mcp_list,
            # Qwen defers MCP tools: they are reviewed with tool_search, then called
            # through tool_call.
            mcp_calls=(
                ("tool_search", {"query": "select:mcp__proof__echo"}),
                ("tool_call", {"name": "mcp__proof__echo", "arguments": {"text": "mcp-proof"}}),
            ),
        )
        for dialect in DIALECTS
    },
    **{
        f"zcode-{dialect}": Case(
            factory=lambda dialect: ZCodeHarness(gateway_dialect=dialect),
            dialect=dialect,
            shell=("Bash", {"command": "echo shell-proof", "description": "Print proof"}),
            mcp_tool="mcp__proof__echo",
            check_config=_zcode_skills,
        )
        for dialect in DIALECTS
    },
    # Muse speaks only Meta's Responses API and offers its tools in a `muse` namespace.
    # Reminder agents are off: their own model calls would race the scripted loop.
    "muse-code-openai-responses": Case(
        factory=lambda dialect: MuseCodeHarness(settings={"reminders": False}),
        dialect="openai-responses",
        shell=("bash", {"command": "echo shell-proof"}),
        mcp_tool="mcp__proof__echo",
        check_config=_muse_skills,
        marker="muse",
    ),
}


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


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
async def test_cli_runs_through_the_gateway_with_resources_and_resumes(
    tmp_path: Path, case: Case
) -> None:
    harness = case.factory(case.dialect)
    first_prompt = "Use the shell, then the MCP tool.\nSecond line: 'quoted' $HOME"
    mcp_calls = case.mcp_calls or ((case.mcp_tool, {"text": "mcp-proof"}),)
    upstream = ScriptedUpstream(
        [
            ("tool", case.shell),
            *(("tool", call) for call in mcp_calls),
            ("text", "FIRST-DONE"),
            ("text", "SECOND-DONE"),
            ("text", "THIRD-DONE"),
        ],
        marker=case.marker or case.shell[0],
    )
    await upstream.start()
    gateway = Gateway(api_key=HOST_SECRET, upstream=upstream.url, dialect=case.dialect)
    await gateway.start()
    recording = EpisodeRecording(tmp_path / "episode")
    gateway_session = gateway.open_session(
        GatewaySession(episode_id=f"vendor-{harness.name}", model=MODEL), recording.transport
    )
    resources = _resources(tmp_path)
    provider = DockerProvider()
    sandbox = await provider.create(
        SandboxRequest(
            episode_id=f"vendor-{harness.name}",
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
            episode_id=f"vendor-{harness.name}",
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
        await case.check_config(sandbox, session)

        first = await harness.launch(first_prompt, sandbox, session, timeout_sec=300)
        assert first.continuation is not None
        assert first.final_message == "FIRST-DONE"
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

    # The program reached the model only through the Gateway, which swapped in the host key.
    assert upstream.requests
    assert {request["auth"] for request in upstream.requests} == {HOST_SECRET}
    assert not upstream.steps
    offered = json.dumps([request["body"] for request in upstream.requests])
    assert case.mcp_tool in offered or '"echo"' in offered
    assert SKILL in offered
    transport = read_jsonl(recording.directory / "trace.transport.jsonl").records
    assert len([record for record in transport if record["kind"] == "call"]) == len(
        upstream.requests
    )

    trajectory = harness.parse_trajectory(
        TrajectoryParseContext(
            episode_id=session.episode_id,
            trajectory_id="vendor-trajectory",
            instruction=first_prompt,
            logs_dir=logs,
            model=MODEL,
            agent_version=version,
            blobs=recording.blobs,
            session_id=third.continuation.native_session_id,
        )
    )
    assert (
        harness.parse_trajectory(
            TrajectoryParseContext(
                episode_id=session.episode_id,
                trajectory_id="vendor-trajectory",
                instruction=first_prompt,
                logs_dir=logs,
                model=MODEL,
                agent_version=version,
                blobs=recording.blobs,
                session_id=third.continuation.native_session_id,
            )
        )
        == trajectory
    )
    assert "parse_issues" not in (trajectory.extra or {}).get("ale", {})
    assert [step.message for step in trajectory.steps if step.source == "user"] == [
        first_prompt,
        "second",
        CONTINUE_PROMPT,
    ]
    calls = {
        call.function_name: (call, result)
        for step in trajectory.steps
        for call in step.tool_calls or ()
        for result in (step.observation.results if step.observation else ())
        if result.source_call_id == call.tool_call_id
    }
    assert "shell-proof" in json.dumps(calls[case.shell[0]][1].content)
    mcp_call, mcp_result = calls[case.mcp_tool]
    assert (mcp_call.extra or {})["ale"]["mcp"] == {"server": "proof", "tool": "echo"}
    assert "echo:mcp-proof" in json.dumps(mcp_result.content)
    assert [step.message for step in trajectory.steps if step.source == "agent"][-3:] == [
        "FIRST-DONE",
        "SECOND-DONE",
        "THIRD-DONE",
    ]
