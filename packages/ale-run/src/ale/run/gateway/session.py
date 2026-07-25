"""Per-episode gateway sessions.

A session is what makes one episode's model traffic accountable: its own bearer token,
its own ceilings, its own counters, and its own replay cache.

The replay cache exists for a subtle reason. Agent SDKs retry on network errors, and a
retried request that reaches the upstream twice would sample twice — inflating usage,
duplicating trace records, and silently forking the conversation the trace claims to
describe. Serving a byte-identical repeat from cache, and coalescing one that arrives
while the first is still in flight, makes retries invisible instead of corrupting.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GatewaySession", "LimitReached", "Limits", "SessionRegistry"]


@dataclass(frozen=True)
class Limits:
    """Ceilings enforced by refusing the next call. ``None`` means unlimited."""

    max_turns: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None


class LimitReached(Exception):
    """A ceiling would be exceeded, so the call was refused before it was made."""

    def __init__(self, limit: str, value: float) -> None:
        super().__init__(f"{limit} limit reached ({value:g})")
        self.limit = limit
        self.value = value


@dataclass
class Usage:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class GatewaySession:
    """One episode's slice of the gateway."""

    episode_id: str
    model: str
    limits: Limits = field(default_factory=Limits)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    usage: Usage = field(default_factory=Usage)
    seq: int = 0

    _replay: dict[str, Any] = field(default_factory=dict, repr=False)
    _inflight: dict[str, asyncio.Future[Any]] = field(default_factory=dict, repr=False)

    def check(self) -> None:
        """Raise :class:`LimitReached` if another call would breach a ceiling.

        Checked before the upstream request rather than after, so a run stops at its
        budget instead of one call past it.
        """
        limits, usage = self.limits, self.usage
        if limits.max_turns is not None and usage.turns >= limits.max_turns:
            raise LimitReached("max_turns", limits.max_turns)
        if limits.max_input_tokens is not None and usage.input_tokens >= limits.max_input_tokens:
            raise LimitReached("max_input_tokens", limits.max_input_tokens)
        if limits.max_output_tokens is not None and usage.output_tokens >= limits.max_output_tokens:
            raise LimitReached("max_output_tokens", limits.max_output_tokens)
        if limits.max_total_tokens is not None and usage.total_tokens >= limits.max_total_tokens:
            raise LimitReached("max_total_tokens", limits.max_total_tokens)
        if limits.max_cost_usd is not None and usage.cost_usd >= limits.max_cost_usd:
            raise LimitReached("max_cost_usd", limits.max_cost_usd)

    def record(self, *, input_tokens: int, output_tokens: int, cost_usd: float) -> int:
        """Account for a completed call and return its sequence number."""
        self.usage.turns += 1
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.cost_usd += cost_usd
        self.seq += 1
        return self.seq

    # --- retry idempotency ---

    @staticmethod
    def digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def cached(self, key: str) -> Any | None:
        return self._replay.get(key)

    def cache(self, key: str, response: Any) -> None:
        self._replay[key] = response

    def inflight(self, key: str) -> asyncio.Future[Any] | None:
        return self._inflight.get(key)

    def begin(self, key: str) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        return future

    def finish(self, key: str, response: Any, error: BaseException | None = None) -> None:
        future = self._inflight.pop(key, None)
        if future is not None and not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(response)
        if error is None:
            self.cache(key, response)


class SessionRegistry:
    """Bearer token to session. One gateway process serves many episodes."""

    def __init__(self) -> None:
        self._by_token: dict[str, GatewaySession] = {}

    def open(self, session: GatewaySession) -> GatewaySession:
        self._by_token[session.token] = session
        return session

    def resolve(self, token: str | None) -> GatewaySession | None:
        return self._by_token.get(token) if token else None

    def close(self, session: GatewaySession) -> None:
        """Revoke a token the moment its episode ends."""
        self._by_token.pop(session.token, None)
