"""A scripted model upstream, so a real agent CLI can run through the real Gateway unbilled.

It speaks the four Gateway dialects, streamed or not. Each reply is decided from the
request alone. With a ``script``, a request whose conversation holds n tool results makes
the call ``script[n]`` if it offers that tool, and otherwise, as past the script's end,
answers with ``FINAL``; so a CLI that reaches a tool through others, or sends a result's
image in a message of its own, still follows the script. A step may instead make its call
from the text of the last tool result, such as a file that result names. Without a
script, it answers with
``FINAL`` after a tool result. Otherwise, when the agent's first user message contains a
``delegations`` marker and the request offers that rule's tool, it calls the tool with the
rule's arguments, so a CLI can be made to spawn subagents; when the request offers a tool
matching ``tool_pattern`` it calls that tool, filling the first command-like string
argument with ``command``; otherwise it answers with ``FINAL``. A tool the Responses API
groups in a namespace is named ``<namespace>.<tool>``; a call names it so with
``dotted_namespaces``, as Meta's API does, and otherwise in OpenAI's separate
``namespace`` field. Every request is kept in ``requests`` for assertions.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

FINAL = "Scripted run finished."
#: One call of a tool: its name and its arguments.
Call = tuple[str, dict[str, Any]]
#: A call, or what makes one from the text of the last tool result; ``None`` makes none.
Step = Call | Callable[[str], Call | None]
SHELL_TOOLS = r"^(bash|shell|execute|terminal|run_command|exec_command|shell_command)$"
_COMMAND_KEYS = ("command", "cmd", "script")


@dataclass
class ScriptedModel:
    tool_pattern: str = SHELL_TOOLS
    command: str = "echo scripted > scripted-proof.txt && cat scripted-proof.txt"
    #: ``(marker, tool pattern, arguments)``, tried in order before ``tool_pattern``.
    delegations: tuple[tuple[str, str, dict[str, Any]], ...] = ()
    #: Calls made in order, each once the conversation holds as many tool results as precede it.
    script: tuple[Step, ...] = ()
    #: Whether a call names a namespaced tool ``<namespace>.<tool>``, as Meta's API does.
    dotted_namespaces: bool = False
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_post("/v1/responses", self._responses)
        app.router.add_post("/v1/messages", self._messages)
        app.router.add_post("/v1beta/models/{target}", self._gemini)
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
        self, tools: list[Any], results: list[str], after_result: bool, task: str
    ) -> Call | None:
        offered = _offered(tools)
        if self.script:
            if len(results) < len(self.script):
                step = self.script[len(results)]
                call = step(results[-1] if results else "") if callable(step) else step
                if call is not None and call[0] in offered:
                    return call
            return None
        if after_result:
            return None
        for marker, tool_pattern, arguments in self.delegations:
            if marker in task:
                call = self._matching(offered, tool_pattern)
                if call is not None:
                    return call[0], arguments
        return self._matching(offered, self.tool_pattern)

    def _matching(self, offered: dict[str, dict[str, Any]], tool_pattern: str) -> Call | None:
        pattern = re.compile(tool_pattern, re.IGNORECASE)
        for name, schema in offered.items():
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
            tool_results("openai-chat-completions", body),
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
        tool = self._tool(
            body.get("tools") or [],
            tool_results("openai-responses", body),
            after,
            _first_user(items),
        )
        if tool:
            namespace, _, name = (
                ("", "", tool[0]) if self.dotted_namespaces else tool[0].rpartition(".")
            )
            item: dict[str, Any] = {
                "id": f"fc_{uuid.uuid4().hex[:12]}",
                "type": "function_call",
                "status": "completed",
                "call_id": f"call_{uuid.uuid4().hex[:12]}",
                **({"namespace": namespace} if namespace else {}),
                "name": name,
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
        # As OpenAI streams it, a call's arguments arrive in their own events, not with the
        # item that opens it.
        opened = {**item, "status": "in_progress", "arguments": ""} if tool else item
        events: list[tuple[str | None, Any]] = [
            ("response.created", {"type": "response.created", "response": pending}),
            (
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": opened},
            ),
        ]
        if tool:
            arguments = {"item_id": item["id"], "output_index": 0}
            events += [
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        **arguments,
                        "delta": item["arguments"],
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        **arguments,
                        "arguments": item["arguments"],
                    },
                ),
            ]
        else:
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
        last = messages[-1].get("content") if messages else None
        tool = self._tool(
            body.get("tools") or [],
            tool_results("anthropic", body),
            isinstance(last, list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in last
            ),
            _first_user(messages),
        )
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

    # --- Gemini ---

    async def _gemini(self, request: web.Request) -> web.StreamResponse:
        body = await self._read(request)
        contents = body.get("contents") or []
        task = next(
            (
                "".join(str(part.get("text") or "") for part in content.get("parts") or ())
                for content in contents
                if content.get("role") == "user"
            ),
            "",
        )
        tool = self._tool(
            body.get("tools") or [],
            tool_results("gemini", body),
            bool(contents)
            and any("functionResponse" in part for part in contents[-1].get("parts") or ()),
            task,
        )
        part = (
            # Gemini 3 signs each call it makes, and a client returns the signature with it.
            {"functionCall": {"name": tool[0], "args": tool[1]}, "thoughtSignature": "c2ln"}
            if tool
            else {"text": FINAL}
        )
        chunk = {
            "candidates": [
                {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "totalTokenCount": 18,
            },
            "modelVersion": request.match_info["target"].partition(":")[0],
        }
        if not request.match_info["target"].endswith(":streamGenerateContent"):
            return web.json_response(chunk)
        return await _sse(request, [(None, chunk)])


def tool_results(dialect: str, body: dict[str, Any]) -> list[str]:
    """The text of each tool result in a request's conversation, in order."""
    if dialect == "openai-chat-completions":
        return [
            _text(message.get("content"))
            for message in body.get("messages") or ()
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
    if dialect == "openai-responses":
        items = body.get("input") if isinstance(body.get("input"), list) else []
        return [
            _text(item.get("output"))
            for item in items
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
    if dialect == "anthropic":
        return [
            _text(block.get("content"))
            for message in body.get("messages") or ()
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
    return [
        json.dumps(part["functionResponse"].get("response") or {})
        for content in body.get("contents") or ()
        if isinstance(content, dict)
        for part in content.get("parts") or ()
        if isinstance(part, dict) and isinstance(part.get("functionResponse"), dict)
    ]


def _text(content: Any) -> str:
    """A message's or tool result's text, whether a string or a list of parts."""
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text") or "")
        for part in content or ()
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _offered(tools: list[Any]) -> dict[str, dict[str, Any]]:
    """The parameter schema of each tool a request offers, by the name a call gives."""
    offered: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        for declaration in tool.get("functionDeclarations") or ():
            if isinstance(declaration, dict) and declaration.get("name"):
                schema = declaration.get("parameters") or declaration.get("parametersJsonSchema")
                offered[str(declaration["name"])] = schema or {}
        if tool.get("type") == "namespace":
            for inner in tool.get("tools") or ():
                schema = inner.get("parameters") or {}
                offered[f"{tool.get('name')}.{inner.get('name')}"] = schema
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if function.get("name"):
            schema = function.get("parameters") or function.get("input_schema") or {}
            offered[str(function["name"])] = schema
    return offered


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
