"""A stepwise agent that looks at the screen and acts.

The smallest thing that is genuinely an agent: send the screenshot and the instruction to
a model, read back the tool calls it wants, translate them into our actions. No planning
layer, no memory beyond the conversation, no retries beyond the gateway's — because the
point of this harness is to exercise the stepwise path end to end, and every extra part
would be one more thing to blame when it fails.

It talks Anthropic's computer-use tool, so the model's own action vocabulary is the wire
format and there is no prompt engineering to get wrong. Traffic goes to the gateway like
every other harness's, which is what puts its calls in the transport trace.

Claude's computer toolset uses screenshot pixels. Convert those coordinates to ALE's
normalised action space; the guest scales them back to display pixels at dispatch.
Generic computer tools continue to declare the normalised space directly.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

from ale.core.env import Observation, TaskEnv
from ale.core.errors import AgentError
from ale.core.harness import HarnessSession, StepwisePolicy
from ale.core.taskspec import OperatingSystem
from ale.core.trace import DesktopAction
from ale.run.recording import Redactor

from ._diagnostics import error_text

__all__ = ["ComputerUseHarness", "translate_action"]

#: The normalised space used by ALE desktop actions and generic computer tools.
GRID = 1000

#: Members whose results or stateful inputs ALE's stepwise environment cannot represent.
_DISABLED_MEMBERS = (
    "zoom",
    "middle_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "cursor_position",
    "hold_key",
)

#: Anthropic's computer-use action names mapped onto ours. Anything missing is refused
#: rather than approximated: dispatching something near what the model asked for is how a
#: trajectory quietly stops being evidence of what happened.
_ACTIONS = {
    "key": "key",
    "type": "type",
    "mouse_move": "move",
    "left_click": "click",
    "left_click_drag": "drag",
    "right_click": "right_click",
    "double_click": "double_click",
    # A real action now, not a no-op: the environment photographs the screen only when
    # asked, so a model that wants to see must say so and this is how it says it.
    "screenshot": "screenshot",
    "cursor_position": None,
    "scroll": "scroll",
    "wait": "wait",
}


def _computer_tool(model: str) -> dict[str, Any]:
    if model.lower().startswith("claude"):
        return {
            "type": "computer_toolset_20260801",
            "configs": {name: {"enabled": False} for name in _DISABLED_MEMBERS},
        }
    return {
        "name": "computer",
        "description": (
            "Control the visible desktop with exactly one action per call. Coordinates "
            "use a 0-1000 grid. Use screenshot after an action when you need to see its result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS)},
                "coordinate": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": GRID},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "to": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": GRID},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "text": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "scroll_direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "scroll_amount": {"type": "integer", "minimum": 1},
            },
            "required": ["action"],
        },
    }


def translate_action(raw: dict[str, Any]) -> DesktopAction | None:
    """Convert one computer-use tool call. ``None`` means "nothing to dispatch"."""
    name = str(raw.get("action", "")).lower()
    if name not in _ACTIONS:
        raise AgentError(f"unsupported computer-use action {name!r}")
    kind = _ACTIONS[name]
    if kind is None:
        return None

    coordinate = raw.get("coordinate")
    raw_keys = raw.get("keys", raw.get("text")) if kind == "key" else None
    if isinstance(raw_keys, str):
        try:
            decoded_keys = json.loads(raw_keys)
        except json.JSONDecodeError:
            decoded_keys = raw_keys
    else:
        decoded_keys = raw_keys
    key_values = decoded_keys if isinstance(decoded_keys, list) else [decoded_keys]
    key_parts = [
        part
        for value in key_values
        if isinstance(value, str)
        for part in value.replace("-", "+").replace(" ", "+").split("+")
        if part.strip()
    ]
    return DesktopAction(
        type=kind,  # type: ignore[arg-type]
        coordinate=tuple(coordinate) if coordinate else None,  # type: ignore[arg-type]
        to=tuple(raw["to"]) if raw.get("to") else None,
        text=raw.get("text") if kind == "type" else None,
        keys=tuple(part.strip() for part in key_parts) or None,
        direction=raw.get("scroll_direction"),
        amount=raw.get("scroll_amount"),
        duration_ms=raw.get("duration_ms"),
    )


def _toolset_actions(
    name: str, arguments: dict[str, Any], screen_size: tuple[int, int] | None
) -> list[DesktopAction]:
    if name in _DISABLED_MEMBERS or name not in _ACTIONS:
        raise AgentError(f"unsupported computer-use member {name!r}")
    raw = dict(arguments, action=name)
    if name not in {"type", "key"} and raw.get("text"):
        raise AgentError("computer-use mouse modifiers are not supported")
    if name == "left_click_drag":
        raw["to"] = raw.get("coordinate")
        raw["coordinate"] = raw.get("start_coordinate")
    for key in ("coordinate", "to"):
        coordinate = raw.get(key)
        if coordinate is None:
            continue
        if screen_size is None:
            raise AgentError("computer-use coordinates require a screenshot first")
        if (
            not isinstance(coordinate, list)
            or len(coordinate) != 2
            or any(
                not isinstance(v, (int, float)) or not 0 <= v < size
                for v, size in zip(coordinate, screen_size, strict=True)
            )
        ):
            raise AgentError("computer-use coordinates are outside the screenshot")
        raw[key] = [v * GRID / size for v, size in zip(coordinate, screen_size, strict=True)]
    if name in {"mouse_move", "left_click_drag"} and (
        raw.get("coordinate") is None or (name == "left_click_drag" and raw.get("to") is None)
    ):
        raise AgentError("computer-use mouse actions require explicit coordinates")
    repeat = raw.get("repeat", 1) if name == "key" else 1
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise AgentError("computer-use key repeat must be between 1 and 100")
    if name == "wait":
        duration = raw.get("duration")
        if not isinstance(duration, (int, float)) or not 0 <= duration <= 300:
            raise AgentError("computer-use wait duration must be between 0 and 300 seconds")
        raw["duration_ms"] = round(duration * 1000)
    action = translate_action(raw)
    assert action is not None
    return [action] * repeat


class ComputerUseHarness(StepwisePolicy):
    """Claude, driving a desktop one observation at a time."""

    name = "computer-use"
    supported_os = (OperatingSystem.LINUX, OperatingSystem.WINDOWS, OperatingSystem.MACOS)

    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 2048) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._messages: list[dict[str, Any]] = []
        self._session: HarnessSession | None = None
        self._pending_tool_id: str | None = None
        self._screen_size: tuple[int, int] | None = None

    def version(self) -> str:
        return self.model

    def integrity(self) -> str:
        return f"model:{self.model}"

    async def rollout(self, env: TaskEnv, session: HarnessSession) -> str | None:
        """Drive one episode on its own copy.

        One harness instance serves every concurrent episode of a run, while the
        conversation and the pending tool call belong to a single episode.
        """
        episode = ComputerUseHarness(model=self.model, max_tokens=self.max_tokens)
        return await StepwisePolicy.rollout(episode, env, session)

    async def start(self, session: HarnessSession) -> None:
        self._session = session
        self._messages = []
        self._pending_tool_id = None
        self._screen_size = None

    async def decide(self, observation: Observation) -> list[DesktopAction]:
        if self._session is None:
            raise AgentError("decide() was called before start()")

        self._messages.append(self._user_turn(observation))
        reply = await self._ask()

        # Keep the assistant turn: the model's next decision depends on what it just did,
        # and dropping it would make every step look like the first.
        self._messages.append({"role": "assistant", "content": reply.get("content", [])})

        # No tool call means the model answered in prose rather than acting, which is how
        # it signals it is done.
        calls = [block for block in reply["content"] if block.get("type") == "tool_use"]
        if not calls:
            return []
        if len(calls) != 1:
            raise AgentError("computer-use requires one tool call per turn")
        block = calls[0]
        if not isinstance(block.get("id"), str) or not block["id"]:
            raise AgentError("computer-use tool call has no id")
        self._pending_tool_id = block["id"]
        if self.model.lower().startswith("claude"):
            if block.get("toolset_name") != "computer":
                raise AgentError("computer-use received an unexpected toolset")
            return _toolset_actions(
                block.get("name", ""), block.get("input") or {}, self._screen_size
            )
        if block.get("name") != "computer":
            raise AgentError("computer-use received an unexpected tool")
        action = translate_action(block.get("input") or {})
        return [action] if action is not None else []

    # --- internals ---

    def _user_turn(self, observation: Observation) -> dict[str, Any]:
        """What came back, framed as the result of the tool call that asked for it.

        An observation carries an image only when the agent asked for one. Steps that did
        something else report that they were carried out — sending an empty image block
        instead would be a malformed request, not an empty screen.
        """
        image: dict[str, Any]
        if observation.screenshot_png:
            if self.model.lower().startswith("claude"):
                png = observation.screenshot_png
                if (
                    len(png) < 24
                    or not png.startswith(b"\x89PNG\r\n\x1a\n")
                    or png[12:16] != b"IHDR"
                ):
                    raise AgentError("computer-use received an invalid PNG screenshot")
                self._screen_size = struct.unpack(">II", png[16:24])
                if 0 in self._screen_size:
                    raise AgentError("computer-use received an empty screenshot")
            image = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(observation.screenshot_png).decode(),
                },
            }
        else:
            image = {"type": "text", "text": "Done. Take a screenshot to see the result."}
        if self._pending_tool_id is not None:
            block = {
                "type": "tool_result",
                "tool_use_id": self._pending_tool_id,
                "content": [image],
            }
            if self.model.lower().startswith("claude"):
                block["toolset_name"] = "computer"
            self._pending_tool_id = None
            return {"role": "user", "content": [block]}

        opening: list[dict[str, Any]] = [image]
        if observation.instruction:
            opening.insert(0, {"type": "text", "text": observation.instruction})
        return {"role": "user", "content": opening}

    async def _ask(self) -> dict[str, Any]:
        import aiohttp

        session = self._session
        assert session is not None
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._messages,
            "tools": [_computer_tool(self.model)],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        headers = {
            "authorization": f"Bearer {session.token}",
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        url = session.gateway_url.rstrip("/") + "/v1/messages"

        async with (
            aiohttp.ClientSession() as http,
            http.post(url, data=json.dumps(payload), headers=headers) as response,
        ):
            body = await response.read()
            redact = Redactor((session.token,))
            try:
                reply = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise AgentError(
                    f"computer-use received invalid model JSON (HTTP {response.status}): "
                    f"{redact(body.decode('utf-8', 'replace'))[:600]}"
                ) from exc
            if response.status != 200:
                raise AgentError(
                    f"the model call failed (HTTP {response.status}): "
                    f"{redact(error_text(reply) or body.decode('utf-8', 'replace'))[:1600]}"
                )
            if not isinstance(reply, dict):
                raise AgentError("computer-use model response is not a JSON object (HTTP 200)")
            if reply.get("type") == "error" or reply.get("error"):
                raise AgentError(
                    f"computer-use model failed (HTTP 200): {redact(error_text(reply))[:1600]}"
                )
            if not isinstance(reply.get("content"), list):
                raise AgentError("computer-use model response has no content array (HTTP 200)")
            return reply
