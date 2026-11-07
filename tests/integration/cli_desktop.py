"""One desktop screenshot through each CLI's own MCP client, checked from end to end.

A desktop Task's agent sees the screen only through the built-in ``cua-desktop`` MCP
server, which each Harness configures in its CLI's native MCP settings. ``CLIS`` says how
a model reaches its screenshot through each CLI, and ``CASES`` runs every CLI in every
Gateway dialect it accepts. All cases share one desktop Sandbox, a container of the
current base or a macOS VM (``tests.support.cli_sandbox``), behind one Gateway: each CLI
is installed once, and a case launches it with a scripted model after clearing what its
previous case left. A CLI that saves a full-size screenshot to a file and names the file
to the model is scripted to read that file, and runs again with a screenshot capped small
enough to inline.

A case checks what the model received and what the trajectory says happened:

- no request carries an image before the call that returns the screenshot, and the first
  request that holds that call's result carries one;
- each image sits where its dialect's provider reads one, declares the type its bytes
  have, decodes completely, has the screen's proportions and fits the Run's cap;
- the trajectory parses cleanly: the prompt, one call per scripted step, each with a
  result, one ``cua-desktop`` screenshot among them, and the scripted final answer;
- the trajectory records an image as the result of the call that returned it.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import inspect
import json
import pkgutil
import re
import shlex
import struct
import zlib
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, get_args

import pytest

import ale.run.harnesses
from ale.core.config import Dialect
from ale.core.errors import ConfigError
from ale.core.harness import EffectiveAgentResources, HarnessSession, TrajectoryParseContext
from ale.core.sandbox import Identity, Sandbox
from ale.core.trajectory import AtifContentPart, AtifTrajectory
from ale.run.gateway.server import REQUEST_PATHS, Gateway
from ale.run.gateway.session import GatewaySession, Upstream
from ale.run.harnesses.antigravity_cli import AntigravityCliHarness
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
from ale.run.protocol.log_mirror import LogMirror
from ale.run.recording import EpisodeRecording
from ale.run.tools import CUA_DESKTOP_NAME, resolved_cua_desktop, stage_cua_desktop
from tests.integration.scripted_model import FINAL, Call, ScriptedModel, Step, tool_results
from tests.support import cli_os, cli_sandbox

PROMPT = "Take one screenshot of the desktop."
#: A vision model of each dialect, by its real name: several CLIs decide from the name
#: whether the model takes images, as they do in a real run.
MODELS = {
    "anthropic": "claude-sonnet-4-5",
    # Not of the GPT-5 family, which the pinned OpenHands SDK sends only over Responses.
    "openai-chat-completions": "claude-sonnet-4-5",
    "openai-responses": "gpt-5-mini",
    "gemini": "gemini-3.8-flash",
}
_IN_TOOL_MESSAGES = (
    "sends a tool's image inside the tool message, which OpenAI's Chat Completions API "
    "refuses: it takes images in user messages only"
)
#: Why a CLI cannot hand the model a screenshot, by Harness or by case. Those cases are
#: expected failures, so a test notices when a new version can.
LIMITATIONS = {
    "grok-build": (
        "Grok Build 0.2.112 turns an MCP image into data-URL text in the tool's output, and "
        "splits images out only from an output over its MCP output limit, after truncating "
        "it, so the image is dropped as broken"
    ),
    "hermes-openai-chat-completions": f"Hermes 0.22.5 {_IN_TOOL_MESSAGES}",
    "kimi-code-openai-chat-completions": f"Kimi Code 2.3.2 {_IN_TOOL_MESSAGES}",
    "openhands-cli-openai-chat-completions": f"OpenHands CLI 1.16.0 {_IN_TOOL_MESSAGES}",
}

_SAVED = re.compile(r"(?:file://)?(/[^\s\]\)\"'`]+\.(?:png|jpe?g|webp))")


def _reading(tool: str, argument: str) -> Callable[[str], Call | None]:
    """A step that reads the image file a tool result names, with ``tool``'s ``argument``."""

    def read(result: str) -> Call | None:
        found = _SAVED.search(result)
        return (tool, {argument: found.group(1)}) if found else None

    return read


@dataclass(frozen=True)
class Reach:
    """How a model reaches ``cua-desktop``'s screenshot through one CLI."""

    #: The native calls that take the screenshot, in order.
    calls: tuple[Step, ...]
    #: The Gateway dialect of a CLI that speaks only one; otherwise every one it accepts.
    dialect: str | None = None
    #: The CLI's settings, among them how it is told that the model takes images where it
    #: has one.
    settings: dict[str, Any] = field(default_factory=dict)
    #: The model, by dialect, where the CLI needs another than ``MODELS`` names.
    models: dict[str, str] = field(default_factory=dict)
    #: Whether the CLI's provider names a namespaced call ``<namespace>.<tool>``.
    dotted_namespaces: bool = False
    #: For a CLI that saves a full-size screenshot to a file and names the file to the
    #: model instead of inlining it: the call that reads it back, and the cap, in pixels,
    #: that makes a screenshot small enough to inline.
    read: Callable[[str], Call | None] | None = None
    inlined: int | None = None


#: How a model reaches the screenshot, for every CLI Harness.
CLIS: dict[type[CliHarness], Reach] = {
    ClaudeCodeHarness: Reach((("mcp__cua-desktop__screenshot", {}),), dialect="anthropic"),
    # Codex offers each MCP server's tools in a Responses namespace of its own.
    CodexCliHarness: Reach((("mcp__cua_desktop.screenshot", {}),), dialect="openai-responses"),
    # Muse offers a Meta model namespaced tools, and Meta's API names a call of one with
    # dots. Reminder agents are off: their own model calls would race the scripted loop,
    # and the parser leaves their sessions unconverted, a parse issue.
    MuseCodeHarness: Reach(
        (("mcp__cua_desktop.screenshot", {}),),
        dialect="openai-responses",
        settings={"reminders": False},
        models={"openai-responses": "muse-spark-1.3"},
        dotted_namespaces=True,
    ),
    OpenClawCliHarness: Reach(
        (("cua-desktop__screenshot", {}),),
        dialect="openai-responses",
        settings={"input": ["text", "image"]},
    ),
    # Antigravity calls every MCP tool through call_mcp_tool.
    AntigravityCliHarness: Reach(
        (
            (
                "call_mcp_tool",
                {"ServerName": "cua-desktop", "ToolName": "screenshot", "Arguments": {}},
            ),
        ),
        dialect="gemini",
        read=_reading("read_file", "Path"),
        inlined=64,
    ),
    # Grok Build reaches MCP tools only through search_tool, then use_tool.
    GrokBuildHarness: Reach(
        (
            ("search_tool", {"query": "cua-desktop screenshot"}),
            ("use_tool", {"tool_name": "cua-desktop__screenshot", "tool_input": {}}),
        )
    ),
    # Droid defers MCP tools: ToolSearch loads one, which is then offered directly.
    DroidHarness: Reach(
        (
            ("ToolSearch", {"query": "select:cua-desktop___screenshot"}),
            ("cua-desktop___screenshot", {}),
        ),
        settings={"no_image_support": False},
    ),
    ForgecodeHarness: Reach(
        (("mcp_cua_desktop_tool_screenshot", {}),),
        settings={"input_modalities": ["text", "image"]},
    ),
    # Hermes defers MCP tools: tool_describe loads one, which tool_call then calls.
    HermesHarness: Reach(
        (
            ("tool_describe", {"names": ["mcp__cua_desktop__screenshot"]}),
            ("tool_call", {"calls": [{"name": "mcp__cua_desktop__screenshot", "arguments": {}}]}),
        ),
        settings={"supports_vision": True},
    ),
    KimiCodeHarness: Reach((("mcp__cua-desktop__screenshot", {}),)),
    # OpenHands names a lone server's tools plainly and asks every call for its risk.
    OpenHandsCliHarness: Reach((("screenshot", {"security_risk": "LOW"}),)),
    PiCliHarness: Reach(
        (("mcp__cua_desktop__screenshot", {}),), settings={"input": ["text", "image"]}
    ),
    # Qwen defers MCP tools: tool_search reviews one, which tool_call then calls.
    QwenCodeHarness: Reach(
        (
            ("tool_search", {"query": "select:mcp__cua-desktop__screenshot"}),
            ("tool_call", {"name": "mcp__cua-desktop__screenshot", "arguments": {}}),
        )
    ),
    # ZCode's catalog has Claude Sonnet 4.5 take no images, so this also shows the setting
    # overriding it.
    ZCodeHarness: Reach(
        (("mcp__cua-desktop__screenshot", {}),),
        settings={"input_format": {"supports_image": True}},
        read=_reading("Read", "file_path"),
        inlined=256,
    ),
}


def cli_harnesses() -> list[type[CliHarness]]:
    """Every concrete CLI Harness ALE ships."""
    for module in pkgutil.iter_modules(ale.run.harnesses.__path__):
        importlib.import_module(f"{ale.run.harnesses.__name__}.{module.name}")
    found: set[type[CliHarness]] = set()
    pending = list(CliHarness.__subclasses__())
    while pending:
        harness = pending.pop()
        pending.extend(harness.__subclasses__())
        if not inspect.isabstract(harness):
            found.add(harness)
    return sorted(found, key=lambda harness: harness.name)


def dialects(harness: type[CliHarness], reach: Reach) -> tuple[str, ...]:
    """The Gateway dialects a CLI is run in: its only one, or every one it accepts."""
    if reach.dialect is not None:
        return (reach.dialect,)
    accepted = []
    for dialect in get_args(Dialect):
        try:
            harness(gateway_dialect=dialect)  # type: ignore[call-arg]
        except ConfigError:
            continue
        accepted.append(dialect)
    return tuple(accepted)


@dataclass(frozen=True)
class Case:
    harness: Callable[[], CliHarness]
    dialect: str
    model: str
    #: The scripted calls: those that take the screenshot, then any that read it back.
    script: tuple[Step, ...]
    dotted_namespaces: bool = False
    #: The Run's cap on the screenshot's longest side.
    cap: int | None = None


def _cases() -> dict[str, Case]:
    cases: dict[str, Case] = {}
    for harness in cli_harnesses():
        reach = CLIS.get(harness)
        if reach is None:
            continue
        for dialect in dialects(harness, reach):
            options: dict[str, Any] = {"settings": reach.settings}
            if reach.dialect is None:
                options["gateway_dialect"] = dialect
            full = Case(
                partial(harness, **options),
                dialect,
                reach.models.get(dialect, MODELS[dialect]),
                reach.calls + ((reach.read,) if reach.read else ()),
                reach.dotted_namespaces,
            )
            cases[f"{harness.name}-{dialect}"] = full
            if reach.inlined:
                cases[f"{harness.name}-{dialect}-inlined"] = Case(
                    full.harness,
                    dialect,
                    full.model,
                    reach.calls,
                    reach.dotted_namespaces,
                    reach.inlined,
                )
    return cases


CASES = _cases()


def expected_to_fail(name: str) -> tuple[pytest.MarkDecorator, ...]:
    """The marks of a case its CLI cannot complete: none, or an expected failure."""
    reason = LIMITATIONS.get(name) or LIMITATIONS.get(CASES[name].harness().name)
    if reason is None:
        return ()
    return (pytest.mark.xfail(raises=AssertionError, reason=reason, strict=True),)


# --- the shared desktop ---


@dataclass(frozen=True)
class Desk:
    """The desktop Sandbox every case runs in, and the Gateway and model behind it."""

    sandbox: Sandbox
    home: str
    gateway: Gateway
    model: ScriptedModel
    upstream: str
    #: The screen's width and height, as ``cua-desktop`` reports them.
    screen: tuple[int, int]
    #: The installed version of each CLI, by Harness.
    versions: dict[str, str]


@asynccontextmanager
async def desktop(work: Path, harnesses: Iterable[CliHarness]) -> AsyncIterator[Desk]:
    """A desktop Sandbox with each of ``harnesses`` installed once, behind one Gateway."""
    model = ScriptedModel()
    upstream = await model.start()
    # A Gemini request names its model and method in its path, which only the Gateway's
    # own upstream keeps; every other dialect reaches the model as a session's upstream.
    gateway = Gateway(api_key="host-only", upstream=upstream, dialect="gemini", host="0.0.0.0")
    await gateway.start()
    try:
        async with cli_sandbox("cli-desktop", gateway.base_url, work) as (sandbox, home):
            await sandbox.open_egress()
            versions = {}
            for harness in harnesses:
                # Once per configuration: an installed CLI is only checked again.
                versions[harness.name] = await harness.install(sandbox)
            await sandbox.close_egress()
            screen = await _screen(sandbox, home)
            yield Desk(sandbox, home, gateway, model, upstream, screen, versions)
    finally:
        await gateway.stop()
        await model.stop()


async def _screen(sandbox: Sandbox, home: str) -> tuple[int, int]:
    """The screen's size, asked of ``cua-desktop`` as an agent's client would ask it."""
    server = resolved_cua_desktop(cli_os()).server
    await stage_cua_desktop(sandbox, home)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_screen_size", "arguments": {}},
        },
    ]
    inbox = f"{home}/.ale-screen-size.jsonl"
    await sandbox.write_file(
        inbox,
        "".join(json.dumps(request) + "\n" for request in requests).encode(),
        identity=Identity.AGENT,
    )
    argv = [part.replace("{home}", home) for part in (server.command, *server.args)]
    result = await sandbox.exec(
        ["sh", "-c", f"{shlex.join(argv)} < {shlex.quote(inbox)}; rm -f {shlex.quote(inbox)}"],
        env=dict(server.environment),
        identity=Identity.AGENT,
        timeout_sec=120,
    )
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    size = replies[-1]["result"]["structuredContent"]
    return int(size["width"]), int(size["height"])


# --- one case ---


async def screenshot_round_trip(desk: Desk, case: Case, work: Path) -> None:
    """Launch the case's CLI once in ``desk`` and check what reached the model and the
    trajectory; every problem found is reported together."""
    harness = case.harness()
    desk.model.script = case.script
    desk.model.dotted_namespaces = case.dotted_namespaces
    desk.model.requests.clear()
    episode_id = f"desktop-{harness.name}"
    recording = EpisodeRecording(work)
    upstream = (
        None
        if case.dialect == "gemini"
        else Upstream(
            dialect=case.dialect,
            endpoint=desk.upstream + REQUEST_PATHS[case.dialect],
            api_key="host-only",
        )
    )
    # The Gateway keeps every request it forwards, so a failure can be read from them.
    gateway_session = desk.gateway.open_session(
        GatewaySession(
            episode_id=episode_id,
            model=case.model,
            payload_dir=work / "payloads",
            upstream=upstream,
        ),
        recording.transport,
    )
    logs = work / "logs"
    try:
        # As a fresh Sandbox would: no session, log or MCP configuration of an earlier
        # case of this CLI remains for the launch to find or the parser to read.
        await desk.sandbox.exec(
            ["rm", "-rf", "--", *(f"{desk.home}/{path}" for path in harness.logs)],
            identity=Identity.AGENT,
        )
        session = HarnessSession(
            episode_id=episode_id,
            gateway_url=desk.sandbox.gateway_url or desk.gateway.base_url,
            token=gateway_session.token,
            model=case.model,
            sandbox_id=desk.sandbox.sandbox_id,
            home=desk.home,
            os=cli_os(),
        )
        resources = EffectiveAgentResources(
            mcp_servers=(resolved_cua_desktop(cli_os(), screenshot_max_dimension=case.cap),)
        )
        harness.validate_resources(resources)
        await harness.install_resources(desk.sandbox, session, resources)
        run = await harness.launch(PROMPT, desk.sandbox, session, timeout_sec=600)
        assert run.continuation is not None, run
        mirror = LogMirror(
            desk.sandbox, home=session.home, os=session.os, paths=harness.logs, target=logs
        )
        await mirror.sync(final=True)
        assert not mirror.incomplete
    finally:
        desk.gateway.close_session(gateway_session)

    trajectory = harness.parse_trajectory(
        TrajectoryParseContext(
            episode_id=episode_id,
            trajectory_id=f"{episode_id}-trajectory",
            instruction=PROMPT,
            logs_dir=logs,
            model=case.model,
            agent_version=desk.versions[harness.name],
            blobs=recording.blobs,
            session_id=run.continuation.native_session_id,
        )
    )
    requests = [body for _, body in desk.model.requests]
    problems = [
        *_received(requests, case, desk.screen),
        *_recorded(trajectory, case, desk.screen, work),
    ]
    assert not problems, "\n".join(problems)


# --- what the model received ---


@dataclass(frozen=True)
class _Shown:
    """One image in a request, where its dialect's provider reads it."""

    declared: str
    encoded: str
    #: Why the provider would not read it, if it would not.
    misplaced: str | None = None


def _received(requests: list[dict[str, Any]], case: Case, screen: tuple[int, int]) -> list[str]:
    problems = []
    steps = len(case.script)
    holding = [
        index
        for index, body in enumerate(requests)
        if len(tool_results(case.dialect, body)) >= steps
    ]
    for index, body in enumerate(requests):
        anywhere = len(list(_images(body)))
        if anywhere and len(tool_results(case.dialect, body)) < steps:
            problems.append(f"request {index} carries an image before the screenshot's result")
        shown = list(_placed(case.dialect, body))
        if anywhere != len(shown):
            problems.append(
                f"request {index} carries {anywhere - len(shown)} image(s) where a "
                f"{case.dialect} provider reads none"
            )
        for image in shown:
            problems += [f"request {index}: {problem}" for problem in _check(image, case, screen)]
    if not holding:
        problems.append("no request holds the result of the call that returns the screenshot")
    elif not list(_placed(case.dialect, requests[holding[0]])):
        problems.append("no screenshot reached the model")
    return problems


def _check(image: _Shown, case: Case, screen: tuple[int, int]) -> list[str]:
    if image.misplaced:
        return [image.misplaced]
    data = base64.b64decode(image.encoded)
    actual = _media_type(data)
    if actual != image.declared:
        return [f"an image declared {image.declared} is {actual or 'no PNG or JPEG'}"]
    problems = []
    if case.dialect == "anthropic" and len(image.encoded) > 10 * 1024 * 1024:
        problems.append(f"an image of {len(image.encoded)} base64 bytes exceeds Claude's 10 MB")
    problems += _picture(data, actual, case.cap, screen)
    return problems


def _picture(data: bytes, media_type: str, cap: int | None, screen: tuple[int, int]) -> list[str]:
    """What is wrong with an image as a screenshot of ``screen`` within ``cap``."""
    try:
        width, height = _png(data) if media_type == "image/png" else _jpeg(data)
    except ValueError as exc:
        return [f"a {media_type} does not decode: {exc}"]
    problems = []
    if abs(width / height - screen[0] / screen[1]) > 0.02 * screen[0] / screen[1]:
        problems.append(f"a {width}x{height} image is not of the {screen[0]}x{screen[1]} screen")
    if cap is not None and max(width, height) > cap:
        problems.append(f"a {width}x{height} image exceeds the {cap} px cap")
    return problems


def _placed(dialect: str, body: dict[str, Any]) -> Iterator[_Shown]:
    """Each base64 image in a request at a place its dialect allows one, with its
    declared type; one in a message whose role the provider refuses it in comes back
    ``misplaced``."""
    if dialect == "anthropic":
        # Claude reads images in a user turn, and inside a tool_result block of one.
        for message in body.get("messages") or ():
            role = message.get("role")
            for block in _list(message.get("content")):
                inner = _list(block.get("content")) if block.get("type") == "tool_result" else []
                for image in (block, *inner):
                    source = image.get("source") or {}
                    if image.get("type") == "image" and source.get("type") == "base64":
                        yield _Shown(
                            str(source.get("media_type")),
                            str(source.get("data")),
                            None if role == "user" else f"an image in a {role} message",
                        )
    elif dialect == "openai-chat-completions":
        # Only a user message takes an image part; a tool message takes text alone.
        for message in body.get("messages") or ():
            role = message.get("role")
            for part in _list(message.get("content")):
                if part.get("type") == "image_url":
                    yield _data_url(
                        str((part.get("image_url") or {}).get("url") or ""),
                        None if role == "user" else f"an image in a {role} message",
                    )
    elif dialect == "openai-responses":
        # A user message, or a function call's output given as a list of content parts.
        for item in body.get("input") if isinstance(body.get("input"), list) else ():
            parts = (
                _list(item.get("output"))
                if item.get("type") == "function_call_output"
                else _list(item.get("content"))
            )
            role = "user" if item.get("type") == "function_call_output" else item.get("role")
            for part in parts:
                if part.get("type") == "input_image":
                    yield _data_url(
                        str(part.get("image_url") or ""),
                        None if role == "user" else f"an image in a {role} message",
                    )
    else:
        # A turn's parts, or a function response's own parts.
        for content in body.get("contents") or ():
            for part in _list(content.get("parts")):
                response = part.get("functionResponse") or {}
                for inner in (part, *_list(response.get("parts"))):
                    data = inner.get("inlineData") or {}
                    if data:
                        yield _Shown(str(data.get("mimeType")), str(data.get("data")))


def _data_url(url: str, misplaced: str | None) -> _Shown:
    header, _, encoded = url.partition(",")
    declared = header.removeprefix("data:").removesuffix(";base64")
    return _Shown(declared, encoded, misplaced)


def _list(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _images(value: Any) -> Iterator[str]:
    """Each base64 image anywhere in a request body, by its leading bytes."""
    if isinstance(value, dict):
        for entry in value.values():
            yield from _images(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from _images(entry)
    elif isinstance(value, str) and len(value) > 64:
        encoded = value.partition(";base64,")[2] if value.startswith("data:") else value
        try:
            head = base64.b64decode(encoded[:16], validate=True)
        except (binascii.Error, ValueError):
            return
        if _media_type(head):
            yield encoded


def _media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _png(data: bytes) -> tuple[int, int]:
    """The size of a whole PNG: every chunk complete with its checksum, up to its end
    chunk, and pixel data that inflates to exactly its rows."""
    offset, compressed, header = 8, bytearray(), None
    while True:
        if offset + 8 > len(data):
            raise ValueError("it ends before its end chunk")
        (length,), kind = (
            struct.unpack(">I", data[offset : offset + 4]),
            data[offset + 4 : offset + 8],
        )
        body = data[offset + 8 : offset + 8 + length]
        checksum = data[offset + 8 + length : offset + 12 + length]
        if len(body) != length or len(checksum) != 4:
            raise ValueError(f"its {kind!r} chunk is cut short")
        if zlib.crc32(kind + body) != struct.unpack(">I", checksum)[0]:
            raise ValueError(f"its {kind!r} chunk fails its checksum")
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break
        offset += 12 + length
    if header is None:
        raise ValueError("it has no header chunk")
    width, height, depth, color, _, _, interlaced = header
    try:
        pixels = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ValueError(f"its pixel data does not inflate: {exc}") from exc
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color, 0)
    rows = height * (1 + (width * channels * depth + 7) // 8)
    if not interlaced and len(pixels) != rows:
        raise ValueError(f"its pixel data holds {len(pixels)} bytes, not {rows}")
    return width, height


def _jpeg(data: bytes) -> tuple[int, int]:
    """The size of a whole JPEG: a frame header, and the end marker last."""
    if not data.endswith(b"\xff\xd9"):
        raise ValueError("it does not end with its end marker")
    offset = 2
    while offset + 4 <= len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    raise ValueError("it has no frame header")


# --- what the trajectory says ---


def _recorded(
    trajectory: AtifTrajectory, case: Case, screen: tuple[int, int], work: Path
) -> list[str]:
    ale = (trajectory.extra or {}).get("ale") or {}
    if ale.get("parse_issues"):
        return [f"the trajectory has parse issues: {ale['parse_issues']}"]
    problems = []
    steps = trajectory.steps
    if not any(step.source == "user" and PROMPT in _text(step.message) for step in steps):
        problems.append("the trajectory has no user step with the prompt")
    calls = [call for step in steps for call in step.tool_calls or ()]
    results = {
        result.source_call_id: result
        for step in steps
        for result in (step.observation.results if step.observation else ())
    }
    names = [call.function_name for call in calls]
    if len(calls) != len(case.script):
        problems.append(f"the trajectory records {names}, not {len(case.script)} scripted calls")
    for call in calls:
        result = results.get(call.tool_call_id)
        if result is None:
            problems.append(f"the trajectory records no result of {call.function_name}")
        elif ((result.extra or {}).get("ale") or {}).get("is_error"):
            problems.append(f"{call.function_name} failed: {_text(result.content)[:300]}")
    shots = [
        call
        for call in calls
        if ((call.extra or {}).get("ale") or {}).get("mcp")
        == {"server": CUA_DESKTOP_NAME, "tool": "screenshot"}
    ]
    if len(shots) != 1:
        problems.append(
            f"the trajectory attributes {len(shots)} calls of {names} to the screenshot"
        )
    replies = [step.message for step in steps if step.source == "agent"]
    if not replies or _text(replies[-1]) != FINAL:
        problems.append(f"the last reply is {_text(replies[-1]) if replies else None!r}")
    last = results.get(calls[-1].tool_call_id) if calls else None
    images = [
        part
        for part in (last.content if last and isinstance(last.content, list) else ())
        if part.type == "image"
    ]
    if not images:
        problems.append(f"the trajectory records no image as the result of {names[-1:]}")
    for part in images:
        assert part.source is not None
        data = (work / part.source.path).read_bytes()
        if _media_type(data) != part.source.media_type:
            problems.append(f"the recorded {part.source.media_type} is {_media_type(data)}")
        else:
            problems += [
                f"the recorded image: {problem}"
                for problem in _picture(data, part.source.media_type, case.cap, screen)
            ]
    return problems


def _text(content: str | list[AtifContentPart] | None) -> str:
    if isinstance(content, list):
        return "".join(part.text or "" for part in content if part.type == "text")
    return content or ""
