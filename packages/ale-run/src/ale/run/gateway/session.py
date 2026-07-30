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
from pathlib import Path
from typing import Any

__all__ = [
    "GatewayReservation",
    "GatewaySession",
    "LimitReached",
    "Limits",
    "SessionRegistry",
]


@dataclass(frozen=True)
class Limits:
    """Ceilings enforced by refusing the next call. ``None`` means unlimited."""

    max_model_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None


class LimitReached(Exception):
    """A completed-usage ceiling was reached, so the next call was refused."""

    def __init__(self, limit: str, value: float, observed: float | None = None) -> None:
        super().__init__(f"{limit} limit reached ({value:g})")
        self.limit = limit
        self.value = value
        self.observed = observed if observed is not None else value


@dataclass
class Usage:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def turns(self) -> int:
        """Compatibility name for callers that predate ``max_model_calls``."""
        return self.model_calls

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class GatewayReservation:
    """One unique upstream request held against the model-call limit."""


@dataclass
class GatewaySession:
    """One episode's slice of the gateway."""

    episode_id: str
    model: str
    limits: Limits = field(default_factory=Limits)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    usage: Usage = field(default_factory=Usage)
    seq: int = 0
    payload_dir: Path | None = None
    """Explicit debug destination for raw provider payloads; absent by default."""
    exact_token_dir: Path | None = None
    """Temporary exact provider token evidence; absent unless explicitly requested."""

    allowed_hosts: frozenset[str] = frozenset()
    """Hosts this episode may reach through the proxy, beyond the model endpoint.

    Empty means none, which is the default: a task that declared no additional access
    gets none, and cannot acquire any by asking.
    """

    def may_reach(self, host: str) -> bool:
        """Whether ``host`` is allowlisted, matching subdomains of a declared name.

        Matching is on the name the client asked for, not on a resolved address: a task
        declares ``pypi.org`` because that is what it means, and pinning to whatever IP
        that resolved to once would break the task rather than tighten it.
        """
        name = host.partition(":")[0].lower().rstrip(".")
        return any(
            name == allowed or name.endswith(f".{allowed}")
            for allowed in (a.lower() for a in self.allowed_hosts)
        )

    _replay: dict[str, Any] = field(default_factory=dict, repr=False)
    _inflight: dict[str, asyncio.Future[Any]] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _reserved_calls: int = field(default=0, repr=False)

    async def reserve(self) -> GatewayReservation:
        """Refuse reached limits, then reserve one model call."""
        async with self._lock:
            limits, usage = self.limits, self.usage
            calls = usage.model_calls + self._reserved_calls + 1
            if limits.max_model_calls is not None and calls > limits.max_model_calls:
                raise LimitReached("max_model_calls", limits.max_model_calls, calls)

            completed = (
                ("max_input_tokens", limits.max_input_tokens, usage.input_tokens),
                ("max_output_tokens", limits.max_output_tokens, usage.output_tokens),
                ("max_total_tokens", limits.max_total_tokens, usage.total_tokens),
                ("max_cost_usd", limits.max_cost_usd, usage.cost_usd),
            )
            for name, limit, observed in completed:
                if limit is not None and observed >= limit:
                    raise LimitReached(name, limit, observed)

            self._reserved_calls += 1
            return GatewayReservation()

    async def commit(
        self,
        reservation: GatewayReservation,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> int:
        """Release a reservation and account for the forwarded call."""
        async with self._lock:
            self._reserved_calls -= 1
            self.usage.model_calls += 1
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
