"""A stepwise agent that looks at the screen and acts.

The smallest thing that is genuinely an agent: send the screenshot and the instruction to
a model, read back the tool calls it wants, translate them into our actions. No planning
layer, no memory beyond the conversation, no retries beyond the gateway's — because the
point of this harness is to exercise the stepwise path end to end, and every extra part
would be one more thing to blame when it fails.

It talks Anthropic's computer-use tool, so the model's own action vocabulary is the wire
format and there is no prompt engineering to get wrong. Traffic goes to the gateway like
every other harness's, which is what puts its calls in the transport trace.

Coordinates are the one real conversion. The model is told the screen is 1000x1000 so it
answers in the space our actions already use; the guest scales to real pixels at dispatch.
Telling it the true resolution and rescaling here would put a rounding step between what
the model meant and what we recorded.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ale.core.env import Observation
from ale.core.errors import AgentError
from ale.core.harness import HarnessSession, StepwisePolicy
from ale.core.trace import DesktopAction

__all__ = ["ComputerUseHarness", "translate_action"]

#: The normalised space we hand the model, matching our own action coordinates.
GRID = 1000

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


def translate_action(raw: dict[str, Any]) -> DesktopAction | None:
    """Convert one computer-use tool call. ``None`` means "nothing to dispatch"."""
    name = str(raw.get("action", "")).lower()
    if name not in _ACTIONS:
        raise AgentError(f"unsupported computer-use action {name!r}")
    kind = _ACTIONS[name]
    if kind is None:
        return None

    coordinate = raw.get("coordinate")
    keys = raw.get("text") if kind == "key" else None
    return DesktopAction(
        type=kind,  # type: ignore[arg-type]
        coordinate=tuple(coordinate) if coordinate else None,  # type: ignore[arg-type]
        to=tuple(raw["to"]) if raw.get("to") else None,
        text=raw.get("text") if kind == "type" else None,
        keys=tuple(part.strip() for part in keys.replace("-", "+").split("+")) if keys else None,
        direction=raw.get("scroll_direction"),
        amount=raw.get("scroll_amount"),
    )


class ComputerUseHarness(StepwisePolicy):
    """Claude, driving a desktop one observation at a time."""

    name = "computer-use"

    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 2048) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._messages: list[dict[str, Any]] = []
        self._session: HarnessSession | None = None
        self._pending_tool_id: str | None = None

    def version(self) -> str:
        return self.model

    def integrity(self) -> str:
        return f"model:{self.model}"

    async def start(self, session: HarnessSession) -> None:
        self._session = session
        self._messages = []

    async def decide(self, observation: Observation) -> list[DesktopAction]:
        if self._session is None:
            raise AgentError("decide() was called before start()")

        self._messages.append(self._user_turn(observation))
        reply = await self._ask()

        # Keep the assistant turn: the model's next decision depends on what it just did,
        # and dropping it would make every step look like the first.
        self._messages.append({"role": "assistant", "content": reply.get("content", [])})

        actions: list[DesktopAction] = []
        used_tool = False
        for block in reply.get("content", []):
            if block.get("type") != "tool_use":
                continue
            used_tool = True
            self._pending_tool_id = block.get("id")
            action = translate_action(block.get("input") or {})
            if action is not None:
                actions.append(action)

        # No tool call means the model answered in prose rather than acting, which is how
        # it signals it is done.
        return actions if used_tool else []

    # --- internals ---

    def _user_turn(self, observation: Observation) -> dict[str, Any]:
        """What came back, framed as the result of the tool call that asked for it.

        An observation carries an image only when the agent asked for one. Steps that did
        something else report that they were carried out — sending an empty image block
        instead would be a malformed request, not an empty screen.
        """
        image: dict[str, Any]
        if observation.screenshot_png:
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
            "tools": [
                {
                    "type": "computer_20250124",
                    "name": "computer",
                    "display_width_px": GRID,
                    "display_height_px": GRID,
                }
            ],
        }
        headers = {
            "authorization": f"Bearer {session.token}",
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "computer-use-2025-01-24",
        }
        url = session.gateway_url.rstrip("/") + "/v1/messages"

        async with (
            aiohttp.ClientSession() as http,
            http.post(url, data=json.dumps(payload), headers=headers) as response,
        ):
            body = await response.read()
            if response.status != 200:
                raise AgentError(
                    f"the model call failed ({response.status}): "
                    f"{body.decode('utf-8', 'replace')[:300]}"
                )
            return json.loads(body)
