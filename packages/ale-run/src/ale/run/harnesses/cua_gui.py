"""The GUI agent migrated from cua-lite, driven by our loop.

The adaptation is not a wrapper, because the shapes do not line up. cua-lite's desktop
agent owns its own loop — ``sample(env, max_steps)`` drives an environment and its
single-step ``predict`` is deliberately unwired — while a :class:`PolicyHarness` is
asked for one decision at a time. Something has to invert.

So the agent runs in a background task against an environment shim, and that shim's
``reset``/``step`` are the inversion point: each blocks on a queue that our loop feeds.
The agent believes it is stepping an environment; the framework is in fact handing it
one observation at a time and taking its actions back. Every observation and every
action therefore still passes through the framework, which is the property that makes
this agent's trajectory comparable with an autonomous one (FR-010b).

cua-lite is not a dependency of this engine, and importing it costs a heavy tree
(litellm, torch-adjacent extras). It is imported lazily, so this module is safe to
import anywhere and only a run that actually selects this harness pays.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
from dataclasses import dataclass, field
from typing import Any

from ale.core.errors import AgentError
from ale.core.harness import HarnessSession, Observation, PolicyHarness
from ale.core.trace import DesktopAction

__all__ = ["CuaGuiHarness", "translate_action"]

#: cua-lite's desktop action vocabulary mapped onto ours. Anything absent is refused
#: rather than guessed: dispatching an approximation of an action an agent asked for is
#: how a trajectory stops describing what happened.
_ACTION_TYPES = {
    "click": "click",
    "left_click": "click",
    "double_click": "double_click",
    "right_click": "right_click",
    "mouse_move": "move",
    "move": "move",
    "left_click_drag": "drag",
    "drag": "drag",
    "scroll": "scroll",
    "type": "type",
    "key": "key",
    "keypress": "key",
    "wait": "wait",
}


def translate_action(raw: dict[str, Any]) -> DesktopAction:
    """Convert one cua-lite action into ours.

    Coordinates already share the [0, 1000] convention, which is why that convention was
    adopted rather than pixels: no rescaling means no place for a rounding difference to
    turn into a click that lands somewhere else.
    """
    name = str(raw.get("action") or raw.get("type") or "").lower()
    kind = _ACTION_TYPES.get(name)
    if kind is None:
        raise AgentError(f"unsupported action {name!r} from the GUI agent")

    coordinate = raw.get("coordinate") or raw.get("start_coordinate")
    destination = raw.get("to") or raw.get("end_coordinate")
    keys = raw.get("keys") or raw.get("key")
    if isinstance(keys, str):
        # cua-lite writes chords as "ctrl+s"; ours are already split.
        keys = tuple(part.strip() for part in keys.replace("-", "+").split("+") if part.strip())

    return DesktopAction(
        type=kind,  # type: ignore[arg-type]
        coordinate=tuple(coordinate) if coordinate else None,  # type: ignore[arg-type]
        to=tuple(destination) if destination else None,  # type: ignore[arg-type]
        text=raw.get("text"),
        keys=tuple(keys) if keys else None,
        direction=raw.get("direction"),
        amount=raw.get("amount") or raw.get("scroll_amount"),
        duration_ms=raw.get("duration_ms"),
    )


@dataclass
class _Observation:
    screenshot_b64: str | None = None
    text: str | None = None


@dataclass
class _StepResult:
    observation: _Observation
    reward: float | None = None
    terminated: bool = False
    truncated: bool = False


@dataclass
class _ShimEnv:
    """A cua-lite environment whose steps are supplied by our loop.

    The agent calls ``reset`` and ``step``; both wait for the framework to hand over the
    next observation. When the framework stops (the episode ended, a guard fired), a
    sentinel terminates the agent's loop rather than leaving it awaiting forever.
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    observations: asyncio.Queue[_StepResult | None] = field(default_factory=asyncio.Queue)
    actions: asyncio.Queue[list[dict[str, Any]]] = field(default_factory=asyncio.Queue)

    async def reset(self) -> _StepResult:
        return await self._next()

    async def step(self, actions: list[dict[str, Any]]) -> _StepResult:
        await self.actions.put(list(actions or []))
        return await self._next()

    async def close(self) -> None:
        return None

    async def _next(self) -> _StepResult:
        result = await self.observations.get()
        if result is None:
            # Ends the agent's loop cleanly; it is not an error, the episode is simply over.
            return _StepResult(observation=_Observation(), terminated=True)
        return result


class CuaGuiHarness(PolicyHarness):
    """cua-lite's Claude desktop agent, decided one step at a time."""

    name = "cua-gui"

    def __init__(self, model: str = "claude-opus-4-6", agent_factory: Any = None) -> None:
        self.model = model
        self._agent_factory = agent_factory
        self._env = _ShimEnv()
        self._task: asyncio.Task[Any] | None = None
        self._version = "unknown"

    def version(self) -> str:
        return self._version

    def integrity(self) -> str:
        return f"cua-lite:{self._version}"

    def _build_agent(self) -> Any:
        if self._agent_factory is not None:
            return self._agent_factory(self.model)
        try:
            module = importlib.import_module("lite.agents.models.claude.agent")
        except ImportError as exc:  # cua-lite is optional, so say so precisely
            raise AgentError(
                "the cua-gui harness needs cua-lite importable; install it, or select another agent"
            ) from exc
        return module.ClaudeDesktopUseAgent(model_id=self.model)

    async def start(self, instruction: str, session: HarnessSession) -> None:
        """Launch the agent against the shim, and let it wait for the first observation."""
        agent = self._build_agent()
        self._version = getattr(agent, "model_id", self.model)
        self._instruction = instruction

        # The agent's model calls must traverse our gateway like everyone else's; these
        # are the variables its client reads, so no agent modification is needed.
        self._env.metadata = {
            "instruction": instruction,
            "base_url": session.gateway_url,
            "api_key": session.token,
        }
        self._task = asyncio.create_task(agent.sample(self._env))

    async def decide(self, observation: Observation) -> list[DesktopAction]:
        if self._task is None:
            raise AgentError("decide() was called before start()")
        if self._task.done():
            return []  # the agent finished on its own; an empty list ends the episode

        screenshot = observation.screenshot_png or b""
        await self._env.observations.put(
            _StepResult(
                observation=_Observation(
                    screenshot_b64=base64.b64encode(screenshot).decode(),
                    text=observation.instruction or self._instruction,
                )
            )
        )

        pending = asyncio.ensure_future(self._env.actions.get())
        done, _ = await asyncio.wait({pending, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if pending not in done:
            # The agent returned rather than acting, which is how it says it is finished.
            pending.cancel()
            return []

        return [translate_action(action) for action in pending.result()]

    async def finish(self) -> str | None:
        """Stop the agent, whether it finished, stalled, or was cut short by a guard."""
        if self._task is None:
            return None
        await self._env.observations.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        except Exception as exc:  # the agent's own failure, reported not swallowed
            return f"cua-lite agent ended with {type(exc).__name__}: {exc}"
        return "cua-lite agent finished"
