"""A scripted model upstream, so a real agent CLI can run through the real Gateway unbilled.

It speaks the three Gateway dialects, streamed or not. Each reply is decided from the
request alone: after a tool result it answers with ``FINAL``. Otherwise, when the agent's
first user message contains a ``delegations`` marker and the request offers that rule's
tool, it calls the tool with the rule's arguments, so a CLI can be made to spawn
subagents; when the request offers a tool matching ``tool_pattern`` it calls that tool,
filling the first command-like string argument with ``command``; otherwise it answers
with ``FINAL``. Every request is kept in ``requests`` for assertions.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

FINAL = "Scripted run finished."
SHELL_TOOLS = r"^(bash|shell|execute|terminal|run_command|exec_command|shell_command)$"
_COMMAND_KEYS = ("command", "cmd", "script")


@dataclass
class ScriptedModel:
    tool_pattern: str = SHELL_TOOLS
    command: str = "echo scripted > scripted-proof.txt && cat scripted-proof.txt"
    #: ``(marker, tool pattern, arguments)``, tried in order before ``tool_pattern``.
    delegations: tuple[tuple[str, str, dict[str, Any]], ...] = ()
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_post("/v1/responses", self._responses)
        app.router.add_post("/v1/messages", self._messages)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # --- the script ---

    def _tool(
        self, tools: list[Any], after_result: bool, task: str
    ) -> tuple[str, dict[str, Any]] | None:
        if after_result:
            return None
        for marker, tool_pattern, arguments in self.delegations:
            if marker in task:
                name = self._offered(tools, tool_pattern)
                if name is not None:
                    return name[0], arguments
        return self._offered(tools, self.tool_pattern)

    def _offered(self, tools: list[Any], tool_pattern: str) -> tuple[str, dict[str, Any]] | None:
        pattern = re.compile(tool_pattern, re.IGNORECASE)
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(function.get("name") or "")
            schema = function.get("parameters") or function.get("input_schema") or {}
            if pattern.search(name):
                return name, self._arguments(schema)
        return None

    def _arguments(self, schema: dict[str, Any]) -> dict[str, Any]:
        properties = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        key = next((name for name in _COMMAND_KEYS if name in properties), None) or next(
            (name for name in required if (properties.get(name) or {}).get("type") == "string"),
            None,
        )
        arguments: dict[str, Any] = {key: self.command} if key else {}
        defaults = {"string": "scripted", "boolean": False, "integer": 60, "number": 60}
        for name in required:
            spec = properties.get(name) or {}
            if name in arguments:
                continue
            if spec.get("enum"):
                arguments[name] = spec["enum"][0]
            elif spec.get("type") in defaults:
                arguments[name] = defaults[spec["type"]]
        return arguments

    async def _read(self, request: web.Request) -> dict[str, Any]:
        body = await request.json()
        self.requests.append((request.path, body))
        return body

    # --- OpenAI Chat Completions ---

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await self._read(request)
        messages = body.get("messages") or []
        tool = self._tool(
            body.get("tools") or [],
            bool(messages) and messages[-1].get("role") == "tool",
            _first_user(messages),
        )
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        base = {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "created": 0, "model": body["model"]}
        finish = "tool_calls" if tool else "stop"
        call = (
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
            }
            if tool
            else None
        )
        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": None if tool else FINAL}
            if call:
                message["tool_calls"] = [call]
            return web.json_response(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                }
            )
        chunk = {**base, "object": "chat.completion.chunk"}
        delta: dict[str, Any] = (
            {"role": "assistant", "tool_calls": [{"index": 0, **call}]}
            if call
            else {"role": "assistant", "content": FINAL}
        )
        return await _sse(
            request,
            [
                (None, {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
                (None, {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}),
                (None, {**chunk, "choices": [], "usage": usage}),
                (None, "[DONE]"),
            ],
        )

    # --- OpenAI Responses ---

    async def _responses(self, request: web.Request) -> web.StreamResponse:
        body = await self._read(request)
        items = body.get("input") if isinstance(body.get("input"), list) else []
        after = bool(items) and items[-1].get("type") == "function_call_output"
        tool = self._tool(body.get("tools") or [], after, _first_user(items))
        if tool:
            item: dict[str, Any] = {
                "id": f"fc_{uuid.uuid4().hex[:12]}",
                "type": "function_call",
                "status": "completed",
                "call_id": f"call_{uuid.uuid4().hex[:12]}",
                "name": tool[0],
                "arguments": json.dumps(tool[1]),
            }
        else:
            item = {
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": FINAL, "annotations": []}],
            }
        response = {
            "id": f"resp_{uuid.uuid4().hex[:12]}",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": body["model"],
            "output": [item],
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        if not body.get("stream"):
            return web.json_response(response)
        pending = {**response, "status": "in_progress", "output": []}
        events: list[tuple[str | None, Any]] = [
            ("response.created", {"type": "response.created", "response": pending}),
            (
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": item},
            ),
        ]
        if not tool:
            events.append(
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": FINAL,
                    },
                )
            )
        events += [
            (
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ),
            ("response.completed", {"type": "response.completed", "response": response}),
        ]
        return await _sse(
            request,
            [
                (name, {**event, "sequence_number": index})
                for index, (name, event) in enumerate(events)
            ],
        )

    # --- Anthropic Messages ---

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        body = await self._read(request)
        messages = body.get("messages") or []
        content = messages[-1].get("content") if messages else None
        after = isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
        tool = self._tool(body.get("tools") or [], after, _first_user(messages))
        block: dict[str, Any] = (
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:12]}",
                "name": tool[0],
                "input": tool[1],
            }
            if tool
            else {"type": "text", "text": FINAL}
        )
        stop = "tool_use" if tool else "end_turn"
        usage = {"input_tokens": 11, "output_tokens": 7}
        message = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [block],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            return web.json_response(message)
        start = {**block, "input": {}} if tool else {"type": "text", "text": ""}
        delta = (
            {"type": "input_json_delta", "partial_json": json.dumps(tool[1])}
            if tool
            else {"type": "text_delta", "text": FINAL}
        )
        return await _sse(
            request,
            [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {**message, "content": [], "stop_reason": None},
                    },
                ),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": start},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop},
                        "usage": {"output_tokens": 7},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ],
        )


async def _sse(request: web.Request, events: list[tuple[str | None, Any]]) -> web.StreamResponse:
    response = web.StreamResponse(headers={"content-type": "text/event-stream"})
    await response.prepare(request)
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        await response.write(((f"event: {name}\n" if name else "") + f"data: {text}\n\n").encode())
    await response.write_eof()
    return response


def _first_user(messages: list[Any]) -> str:
    """The text of an agent's first user message: the task it was given."""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "".join(
            str(part.get("text") or "")
            for part in content or ()
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return ""
