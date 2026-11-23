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
from typing import Any, Literal

from ale.core.taskspec import NetworkPolicy
from ale.core.trace import UpstreamError

__all__ = [
    "CallUsage",
    "GatewayReservation",
    "GatewaySession",
    "LimitReached",
    "Limits",
    "SessionRegistry",
    "Upstream",
    "UpstreamFailure",
]


@dataclass(frozen=True)
class Upstream:
    """Where one session's calls go instead of the Gateway's own upstream.

    A Judge session carries one: its calls reach the Judge's own endpoint with the Judge's
    own key, which never leaves the Host.
    """

    dialect: str
    endpoint: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class Limits:
    """Ceilings enforced by refusing the next call. ``None`` means unlimited."""

    max_model_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None


class LimitReached(Exception):
    """A completed-usage ceiling was reached, so the next call was refused."""

    def __init__(self, limit: str, value: float, observed: float | None = None) -> None:
        super().__init__(f"{limit} limit reached ({value:g})")
        self.limit = limit
        self.value = value
        self.observed = observed if observed is not None else value


@dataclass(frozen=True)
class CallUsage:
    """Provider-reported tokens of one call, with one meaning in every dialect."""

    input_tokens: int = 0
    """Every input token the provider processed, cached or not."""
    cache_read_tokens: int = 0
    """Of ``input_tokens``, those read from the provider's prompt cache."""
    cache_write_tokens: int = 0
    """Of ``input_tokens``, those written to the provider's prompt cache."""
    output_tokens: int = 0
    """Every generated token, reasoning included."""


@dataclass
class Usage:
    """One session's calls the provider answered, and the tokens of all its calls, with
    :class:`CallUsage` meanings."""

    model_calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UpstreamFailure:
    """How the model provider is failing a session's calls: from the first call it failed,
    at ``since`` (the event loop's clock), until it answers one."""

    kind: UpstreamError
    status: int | None
    """The HTTP status of the last failure; ``None`` where it was ``unreachable``."""
    since: float

    def describe(self, now: float) -> str:
        status = "" if self.status is None else f" (HTTP {self.status})"
        return f"{self.kind.replace('_', ' ')}{status} for {max(now - self.since, 0):.0f} s"


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
    redacted: tuple[str, ...] = field(default=(), repr=False)
    """The Task's secret values, kept out of retained payloads and token evidence."""

    refusal: LimitReached | None = field(default=None, repr=False)
    """The first limit that refused one of this session's calls, if any."""
    failure: UpstreamFailure | None = field(default=None, repr=False)
    """How the model provider is failing this session's calls, until it answers one."""
    suspended: bool = False
    """Whether its calls are refused for now: an agent's from the end of its first turn, or
    of its policy rollout, except while a later turn runs; a Judge's except while a verifier
    runs; an egress session's except while a phase with a proxied policy runs. Nothing run
    between the agent's turns or after its last then spends either model session."""

    purpose: Literal["agent", "judge", "egress"] = "agent"
    """A Judge session is unlimited and never replays: a retried verdict must sample again.
    An egress session authenticates a Sandbox's relay to the egress proxy and nothing else:
    the Gateway refuses it, as the proxy refuses every other session."""
    upstream: Upstream | None = field(default=None, repr=False)
    """The session's own upstream, or ``None`` for the Gateway's."""

    policy: NetworkPolicy = field(default_factory=NetworkPolicy)
    """The policy of the phase that runs, for an egress session: what it reaches through the
    proxy. The default, ``block``, reaches nothing: a task that declared no additional access
    gets none, and cannot acquire any by asking.
    """
    provider_web_tools: bool = False
    """Whether calls may use the provider's own web tools, such as search or grounding.

    They reach the web from the provider's side, outside the Sandbox's network policy, so
    only a Task whose network is open may use them; the default refuses them.
    """

    def may_reach(self, host: str) -> bool:
        """Whether the phase's policy admits ``host``, as the client asked for it.

        Matching is on the name the client asked for, not on a resolved address: a task
        declares ``pypi.org`` because that is what it means, and pinning to whatever IP
        that resolved to once would break the task rather than tighten it. Under a
        blocklist the proxy also judges the address it connects to and the name the TLS
        handshake asks for (``proxy.py``).
        """
        name = host.partition(":")[0] if host.count(":") == 1 else host
        return self.policy.admits(name)

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
            )
            for name, limit, observed in completed:
                if limit is not None and observed >= limit:
                    raise LimitReached(name, limit, observed)

            self._reserved_calls += 1
            return GatewayReservation()

    async def commit(
        self,
        reservation: GatewayReservation,
        usage: CallUsage | None = None,
        *,
        answered: bool,
    ) -> int:
        """Release a reservation and account for the call's tokens, and for the call itself
        where the provider ``answered`` it, a request it refused included: one it failed
        gave the agent nothing."""
        usage = usage or CallUsage()
        async with self._lock:
            self._reserved_calls -= 1
            self.usage.model_calls += int(answered)
            self.usage.input_tokens += usage.input_tokens
            self.usage.cache_read_tokens += usage.cache_read_tokens
            self.usage.cache_write_tokens += usage.cache_write_tokens
            self.usage.output_tokens += usage.output_tokens
            self.seq += 1
            return self.seq

    def upstream_failed(
        self, kind: UpstreamError, status: int | None, now: float
    ) -> UpstreamFailure:
        """Record that the model provider failed a call, in the failure that began at the
        first."""
        since = self.failure.since if self.failure is not None else now
        self.failure = UpstreamFailure(kind, status, since)
        return self.failure

    def upstream_answered(self) -> None:
        """Record that the model provider answered a call, which ends its failure."""
        self.failure = None

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

    def finish(
        self,
        key: str,
        response: Any,
        error: BaseException | None = None,
        *,
        replayable: bool = True,
    ) -> None:
        """Settle an in-flight call; keep it for replay only when a retry must not resample it.

        Waiters coalesced on the call always get its response. A response that is not
        ``replayable`` (a provider error) is not kept, so the client's next retry of the
        same request is forwarded instead of being answered with the old failure.
        """
        future = self._inflight.pop(key, None)
        if future is not None and not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(response)
        if error is None and replayable:
            self.cache(key, response)


class SessionRegistry:
    """Bearer token to session. One gateway process serves many episodes."""

    def __init__(self) -> None:
        self._by_token: dict[str, GatewaySession] = {}

    def open(self, session: GatewaySession) -> GatewaySession:
        self._by_token[session.token] = session
        return session

    def resolve(self, token: str | None) -> GatewaySession | None:
        """The open model session of ``token``, an agent's or a Judge's; none for an unknown,
        a suspended or an egress one."""
        session = self.known(token)
        return None if session is None or session.suspended else session

    def known(self, token: str | None) -> GatewaySession | None:
        """The model session of ``token``, suspended or not, until its episode ends."""
        session = self._by_token.get(token) if token else None
        return None if session is None or session.purpose == "egress" else session

    def egress(self, token: str | None) -> GatewaySession | None:
        """The open egress session of ``token``; none for any other session."""
        session = self._by_token.get(token) if token else None
        if session is None or session.purpose != "egress" or session.suspended:
            return None
        return session

    def close(self, session: GatewaySession) -> None:
        """Revoke a token the moment its episode ends."""
        self._by_token.pop(session.token, None)
