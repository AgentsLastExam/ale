"""Provider-side network tools are forwarded only for a Task whose network is open.

Each dialect names the tools and fields its providers run on their own side that reach
the network; the Gateway refuses a request that asks for one unless its session allows
them. Gemini's grounding is covered with the rest of that dialect in its own tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from ale.core.trace import read_jsonl
from ale.run.gateway import anthropic, gemini, openai_chat_completions, openai_responses
from ale.run.gateway.server import REQUEST_PATHS, Gateway
from ale.run.gateway.session import GatewaySession
from ale.run.recording import EpisodeRecording

pytestmark = pytest.mark.unit

FUNCTION = {"type": "function", "name": "run", "parameters": {"type": "object"}}
CHAT_FUNCTION = {"type": "function", "function": {"name": "run", "parameters": {}}}
CLIENT_TOOL = {"name": "run", "input_schema": {"type": "object"}}
PNG = "data:image/png;base64,iVBORw0KGgo="
URL = "https://example.com/answer.png"


def anthropic_image(source: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": [{"type": "image", "source": source}]}]}


def responses_input(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"input": [{"type": "message", "role": "user", "content": list(parts)}]}


def user_message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": list(parts)}]}


#: Per dialect: requests that ask the provider to reach the network, with what the Gateway
#: names in its refusal, and requests that do not.
ASKING: dict[str, list[tuple[dict[str, Any], list[str]]]] = {
    "anthropic": [
        (
            {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
            ["web_search_20250305"],
        ),
        (
            {"tools": [CLIENT_TOOL, {"type": "web_search_20260318", "name": "web_search"}]},
            ["web_search_20260318"],
        ),
        ({"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]}, ["web_fetch_20250910"]),
        ({"tools": [{"type": "web_fetch_20260309", "name": "web_fetch"}]}, ["web_fetch_20260309"]),
        (
            {
                "mcp_servers": [{"type": "url", "url": "https://mcp.example", "name": "x"}],
                "tools": [{"type": "mcp_toolset", "mcp_server_name": "x"}],
            },
            ["mcp_servers", "mcp_toolset"],
        ),
        (anthropic_image({"type": "url", "url": URL}), ["image source url"]),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t",
                                "content": [
                                    {"type": "document", "source": {"type": "url", "url": URL}}
                                ],
                            }
                        ],
                    }
                ]
            },
            ["document source url"],
        ),
    ],
    "openai-responses": [
        ({"tools": [{"type": "web_search"}]}, ["web_search"]),
        (
            {"tools": [{"type": "web_search", "external_web_access": False}]},
            ["web_search"],
        ),
        ({"tools": [FUNCTION, {"type": "web_search_preview"}]}, ["web_search_preview"]),
        ({"tools": [{"type": "web_search_2025_08_26"}]}, ["web_search_2025_08_26"]),
        ({"tools": [{"type": "web_search_preview_2025_03_11"}]}, ["web_search_preview_2025_03_11"]),
        (
            {"tools": [{"type": "mcp", "server_label": "x", "server_url": "https://mcp.example"}]},
            ["mcp"],
        ),
        ({"tools": [{"type": "mcp", "connector_id": "connector_gmail"}]}, ["mcp"]),
        ({"tools": [{"type": "x_search"}]}, ["x_search"]),
        (
            {"tools": [{"type": "web_extractor"}, {"type": "image_search"}]},
            ["image_search", "web_extractor"],
        ),
        (
            {"input": [{"type": "tool_search_output", "tools": [{"type": "web_search"}]}]},
            ["web_search"],
        ),
        (
            {
                "tools": [
                    {
                        "type": "shell",
                        "environment": {
                            "type": "container_auto",
                            "network_policy": {"type": "allowlist", "allowed_domains": ["*"]},
                        },
                    }
                ]
            },
            ["shell network_policy allowlist"],
        ),
        (
            {
                "tools": [
                    {
                        "type": "code_interpreter",
                        "container": {"type": "auto", "network_policy": {"type": "allowlist"}},
                    }
                ]
            },
            ["code_interpreter network_policy allowlist"],
        ),
        (responses_input({"type": "input_image", "image_url": URL}), ["input_image url"]),
        (
            responses_input({"type": "input_file", "file_url": "https://example.com/a.pdf"}),
            ["input_file url"],
        ),
    ],
    "openai-chat-completions": [
        ({"web_search_options": {}}, ["web_search_options"]),
        ({"search_parameters": {"mode": "auto"}}, ["search_parameters"]),
        ({"search_parameters": {}}, ["search_parameters"]),
        ({"enable_search": True}, ["enable_search"]),
        ({"tools": [CHAT_FUNCTION, {"type": "web_search", "web_search": {}}]}, ["web_search"]),
        (
            {"tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}]},
            ["builtin_function $web_search"],
        ),
        (user_message({"type": "image_url", "image_url": {"url": URL}}), ["image_url url"]),
        (
            user_message({"type": "video_url", "video_url": {"url": "https://example.com/v"}}),
            ["video_url url"],
        ),
    ],
    "gemini": [
        ({"tools": [{"googleSearch": {}}]}, ["googleSearch"]),
        ({"tools": [{"url_context": {}}]}, ["url_context"]),
        (
            {"tools": [{"mcpServers": [{"name": "x", "streamableHttpTransport": {"url": URL}}]}]},
            ["mcpServers"],
        ),
        (
            {"contents": [{"role": "user", "parts": [{"fileData": {"fileUri": URL}}]}]},
            ["fileData url"],
        ),
    ],
}
PLAIN: dict[str, list[dict[str, Any]]] = {
    "anthropic": [
        {},
        {"tools": [CLIENT_TOOL]},
        {"tools": [{"type": "custom", "name": "web_search", "input_schema": {}}]},
        {"tools": [{"type": "bash_20250124", "name": "bash"}]},
        {"tools": [{"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}]},
        {"tools": [{"type": "code_execution_20250825", "name": "code_execution"}]},
        {"tools": [{"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}]},
        {"tools": [{"type": "memory_20250818", "name": "memory"}]},
        anthropic_image({"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}),
        # A search result the client supplies names its source; nothing is fetched.
        user_message({"type": "search_result", "source": URL, "title": "t", "content": []}),
    ],
    "openai-responses": [
        {},
        {"tools": [FUNCTION]},
        {"tools": [{"type": "custom", "name": "apply_patch"}, {"type": "local_shell"}]},
        {"tools": [{"type": "namespace", "name": "mcp__x", "tools": [FUNCTION]}]},
        {"tools": [{"type": "image_generation"}, {"type": "file_search"}]},
        {"tools": [{"type": "tool_search"}, {"type": "computer_use_preview"}]},
        {"tools": [{"type": "shell", "environment": {"type": "local"}}]},
        {
            "tools": [
                {
                    "type": "shell",
                    "environment": {
                        "type": "container_auto",
                        "network_policy": {"type": "disabled"},
                    },
                },
                {"type": "code_interpreter", "container": {"type": "auto"}},
            ]
        },
        responses_input({"type": "input_image", "image_url": PNG}),
        # A search the model ran earlier is history, not a request for another.
        {"input": [{"type": "web_search_call", "id": "ws_1", "status": "completed"}]},
    ],
    "openai-chat-completions": [
        {},
        {"tools": [CHAT_FUNCTION]},
        {"tools": [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]},
        {"enable_search": False},
        {"search_parameters": {"mode": "off"}},
        user_message({"type": "image_url", "image_url": {"url": PNG}}),
    ],
    "gemini": [
        {},
        {"tools": [{"functionDeclarations": [{"name": "run"}]}]},
        {"tools": [{"codeExecution": {}}, {"computerUse": {}}]},
        {"contents": [{"role": "user", "parts": [{"inlineData": {"data": "iVBORw0KGgo="}}]}]},
    ],
}
DIALECTS = {
    "anthropic": anthropic,
    "gemini": gemini,
    "openai-chat-completions": openai_chat_completions,
    "openai-responses": openai_responses,
}


@pytest.mark.parametrize(
    ("dialect", "payload", "named"),
    [(dialect, payload, named) for dialect, cases in ASKING.items() for payload, named in cases],
)
def test_each_dialect_names_the_network_tools_a_request_asks_for(
    dialect: str, payload: dict[str, Any], named: list[str]
) -> None:
    assert DIALECTS[dialect].web_tools(payload) == named


@pytest.mark.parametrize(
    ("dialect", "payload"),
    [(dialect, payload) for dialect, cases in PLAIN.items() for payload in cases],
)
def test_client_tools_and_offline_provider_tools_are_not_network_tools(
    dialect: str, payload: dict[str, Any]
) -> None:
    assert DIALECTS[dialect].web_tools(payload) == []


# --- through the Gateway ---


class Upstream:
    """Answers every fixed-path dialect with a minimal completed response."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        for path in REQUEST_PATHS.values():
            app.router.add_post(path, self._answer)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.json())
        return web.json_response({"id": "x", "usage": {}})


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[Upstream]:
    stub = Upstream()
    await stub.start()
    try:
        yield stub
    finally:
        await stub.stop()


async def post(
    upstream: Upstream, tmp_path: Path, dialect: str, payload: dict[str, Any], *, open_network: bool
) -> tuple[int, dict[str, str], dict[str, Any], list[dict[str, Any]]]:
    """Send one request in ``dialect``; return its status, headers, body and trace calls."""
    gateway = Gateway(api_key="host-key", upstream=upstream.url, dialect=dialect, host="127.0.0.1")
    await gateway.start()
    recording = EpisodeRecording(tmp_path)
    session = gateway.open_session(
        GatewaySession(episode_id="episode", model="run-model", provider_web_tools=open_network),
        recording.transport,
    )
    try:
        async with (
            ClientSession() as client,
            client.post(
                gateway.base_url + REQUEST_PATHS[dialect],
                json={"model": "other", "max_tokens": 10, **payload},
                headers={"authorization": f"Bearer {session.token}"},
            ) as response,
        ):
            reply = (response.status, dict(response.headers), await response.json())
    finally:
        await gateway.stop()
    calls = read_jsonl(recording.directory / "trace.transport.jsonl").records
    return (*reply, [call for call in calls if call["kind"] == "call"])


@pytest.mark.parametrize("dialect", sorted(REQUEST_PATHS))
async def test_a_request_for_network_tools_is_refused_unless_the_network_is_open(
    upstream: Upstream, tmp_path: Path, dialect: str
) -> None:
    """The provider would reach the web outside the Sandbox's network policy."""
    payload, named = ASKING[dialect][0]

    status, headers, body, calls = await post(
        upstream, tmp_path / "closed", dialect, payload, open_network=False
    )

    assert status == 400
    assert headers["x-should-retry"] == "false", "a policy does not lift by waiting"
    assert body["error"]["type"] == "invalid_request_error"
    assert f"refuses {', '.join(named)}" in body["error"]["message"]
    assert upstream.bodies == []
    (call,) = calls
    assert (call["disposition"], call["refusal_limit"]) == ("refused", "web_tools")

    status, _, _, calls = await post(
        upstream, tmp_path / "open", dialect, payload, open_network=True
    )

    assert status == 200
    (forwarded,) = upstream.bodies
    assert {name: forwarded.get(name) for name in payload} == payload, "forwarded unchanged"
    assert [call["disposition"] for call in calls] == ["forwarded"]


@pytest.mark.parametrize("dialect", sorted(REQUEST_PATHS))
async def test_a_request_without_network_tools_is_forwarded_with_the_network_closed(
    upstream: Upstream, tmp_path: Path, dialect: str
) -> None:
    status, _, _, calls = await post(
        upstream, tmp_path, dialect, PLAIN[dialect][1], open_network=False
    )

    assert status == 200
    assert len(upstream.bodies) == 1
    assert [call["disposition"] for call in calls] == ["forwarded"]
