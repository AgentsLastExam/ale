"""The opt-in CUA Desktop MCP server, driven the way an agent's client drives it.

An autonomous agent reaches a screen through MCP tool calls, not through the stepwise
loop. This stages the bridge into a real desktop image and speaks the protocol to it over
a pipe — the same three methods a client uses, in the same order — because a bridge that
only ever gets exercised by a live model is a bridge whose failures arrive as "the model
did not use the tools".
"""

from __future__ import annotations

import base64
import json

import pytest

from ale.core.sandbox import Identity, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.run.providers.docker import DockerProvider
from ale.run.tools import CUA_DESKTOP_NAME, stage_cua_desktop
from tests.support import prepare_reference

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker, pytest.mark.needs_gui]

GUI_IMAGE = "ghcr.io/agentslastexam/container-ubuntu22-base:latest"
WORK_DIR = "/home/user/work"


@pytest.fixture
async def desktop():  # type: ignore[no-untyped-def]
    provider = DockerProvider()
    prepared = await prepare_reference(provider, GUI_IMAGE)
    sandbox = await provider.create(
        SandboxRequest(
            episode_id="mcp",
            prepared_image=prepared,
            resources=Resources(cpus=2, memory_mb=2048),
            network=NetworkPolicy(mode=NetworkMode.BLOCK),
        )
    )
    await sandbox.exec(["mkdir", "-p", WORK_DIR], identity=Identity.AGENT)
    try:
        yield sandbox
    finally:
        await sandbox.destroy()


async def speak(sandbox, server_path: str, requests: list[dict]) -> list[dict]:  # type: ignore[no-untyped-def]
    """Send a batch of MCP requests to the staged server and collect the replies.

    Launched from the same staged path that an MCP client receives.
    """
    argv = ["python3", server_path]

    # Written as a file rather than echoed through the shell: the protocol is
    # line-delimited, and every shell way of producing a newline is one more thing that
    # can quietly produce a backslash and an n instead.
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    inbox = f"{WORK_DIR}/mcp-in.jsonl"
    await sandbox.write_file(inbox, payload.encode("utf-8"), identity=Identity.AGENT)

    result = await sandbox.exec(
        ["sh", "-c", f"{' '.join(argv)} < {inbox}"],
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    assert result.exit_code == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_cua_desktop_is_absent_until_explicitly_staged(desktop) -> None:  # type: ignore[no-untyped-def]
    missing = await desktop.exec(
        ["test", "!", "-e", f"{WORK_DIR}/.ale-cua-desktop/cua_desktop_mcp.py"],
        identity=Identity.AGENT,
    )
    assert missing.ok


@pytest.mark.asyncio
async def test_cua_desktop_answers_the_handshake_and_lists_all_tools(desktop) -> None:  # type: ignore[no-untyped-def]
    root = await stage_cua_desktop(desktop, WORK_DIR)
    replies = await speak(
        desktop,
        f"{root}/cua_desktop_mcp.py",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    )

    assert replies[0]["result"]["serverInfo"] == {
        "name": CUA_DESKTOP_NAME,
        "version": "0.3.0",
    }
    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == [
        "key",
        "key_down",
        "key_up",
        "type",
        "hold_key",
        "mouse_move",
        "click",
        "drag",
        "mouse_down",
        "mouse_up",
        "scroll",
        "wait",
        "screenshot",
        "cursor_position",
        "get_screen_size",
    ]


@pytest.mark.asyncio
async def test_a_screenshot_comes_back_as_a_real_image(desktop) -> None:  # type: ignore[no-untyped-def]
    root = await stage_cua_desktop(desktop, WORK_DIR)
    replies = await speak(
        desktop,
        f"{root}/cua_desktop_mcp.py",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "screenshot", "arguments": {}},
            },
        ],
    )

    blocks = replies[1]["result"]["content"]
    image = next(block for block in blocks if block["type"] == "image")
    png = base64.b64decode(image["data"])
    # Checked as bytes rather than by length: an error page rendered to a PNG would still
    # be thousands of bytes, and a base64 string of anything is still a base64 string.
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), png[:16]


@pytest.mark.asyncio
async def test_actions_reach_the_desktop_and_are_described_back(desktop) -> None:  # type: ignore[no-untyped-def]
    """A reply that says what happened, and a cursor that actually moved."""
    root = await stage_cua_desktop(desktop, WORK_DIR)
    replies = await speak(
        desktop,
        f"{root}/cua_desktop_mcp.py",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "mouse_move", "arguments": {"coordinate": [500, 500]}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_screen_size", "arguments": {}},
            },
        ],
    )

    assert "500" in replies[1]["result"]["content"][0]["text"]
    size = replies[2]["result"]["structuredContent"]

    where = await desktop.exec(
        ["sh", "-c", "xdotool getmouselocation --shell"], identity=Identity.AGENT
    )
    position = dict(line.split("=", 1) for line in where.stdout.strip().splitlines() if "=" in line)
    width, height = size["width"], size["height"]
    # Half of each axis, allowing for the rounding a normalised space costs.
    assert abs(int(position["X"]) - width // 2) <= 2
    assert abs(int(position["Y"]) - height // 2) <= 2


@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_to_the_model_not_the_transport(desktop) -> None:  # type: ignore[no-untyped-def]
    """The agent has to be able to recover; a protocol error would end the session."""
    root = await stage_cua_desktop(desktop, WORK_DIR)
    replies = await speak(
        desktop,
        f"{root}/cua_desktop_mcp.py",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "key",
                    "arguments": {"keys": ["a"], "unknown": True},
                },
            },
        ],
    )

    assert "error" not in replies[1]
    assert replies[1]["result"]["isError"] is True


@pytest.mark.asyncio
async def test_staging_cua_desktop_twice_is_idempotent(desktop) -> None:  # type: ignore[no-untyped-def]
    first = await stage_cua_desktop(desktop, WORK_DIR)
    second = await stage_cua_desktop(desktop, WORK_DIR)
    assert first == second
    assert (await desktop.read_file(f"{first}/cua_desktop_mcp.py")).startswith(b'"""AgentsLastExam')
