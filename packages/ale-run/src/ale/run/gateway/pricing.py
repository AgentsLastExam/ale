"""List prices for estimating what one model call cost.

A price is looked up by exact model ID, optionally followed by a dated snapshot suffix.
A model that is not listed has an unknown cost (``None``), never a guessed or zero one:
an estimate is only useful for stopping a runaway run if it is the provider's own price.

Standard-tier prices in USD per million tokens, copied on 2026-10-13 from
https://platform.claude.com/docs/en/about-claude/pricing and
https://developers.openai.com/api/docs/pricing. Anthropic cache writes use the 5-minute
rate. Update this table, not a family heuristic, when a provider changes a price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ale.run.gateway.session import CallUsage

__all__ = ["Price", "Rates", "estimate_cost", "price_for"]


@dataclass(frozen=True)
class Rates:
    input: float
    cache_read: float
    cache_write: float
    output: float


@dataclass(frozen=True)
class Price:
    standard: Rates
    long_context: Rates | None = None
    long_context_over: int = 0
    """Input tokens above which a call is billed at ``long_context`` rates."""


def _flat(input: float, cache_read: float, cache_write: float, output: float) -> Price:
    return Price(Rates(input, cache_read, cache_write, output))


_OPUS_4 = _flat(5.0, 0.50, 6.25, 25.0)
_SONNET_4 = _flat(3.0, 0.30, 3.75, 15.0)

PRICES: dict[str, Price] = {
    "claude-fable-5-1": _flat(10.0, 0.25, 12.50, 50.0),
    "claude-mythos-5-1": _flat(10.0, 0.25, 12.50, 50.0),
    "claude-fable-5": _flat(10.0, 1.0, 12.50, 50.0),
    "claude-mythos-5": _flat(10.0, 1.0, 12.50, 50.0),
    "claude-opus-5-5": _flat(4.0, 0.20, 5.0, 20.0),
    "claude-opus-5": _OPUS_4,
    "claude-opus-4-8": _OPUS_4,
    "claude-opus-4-7": _OPUS_4,
    "claude-opus-4-6": _OPUS_4,
    "claude-opus-4-5": _OPUS_4,
    "claude-sonnet-5-5": _flat(2.0, 0.10, 2.50, 10.0),
    "claude-sonnet-5": _flat(2.0, 0.20, 2.50, 10.0),
    "claude-sonnet-4-6": _SONNET_4,
    "claude-sonnet-4-5": _SONNET_4,
    "claude-haiku-5-5": Price(
        Rates(0.10, 0.01, 0.125, 0.50),
        long_context=Rates(0.50, 0.05, 0.625, 2.50),
        long_context_over=100_000,
    ),
    "claude-haiku-4-5": _flat(1.0, 0.10, 1.25, 5.0),
    "gpt-5-mini": _flat(0.25, 0.025, 0.25, 2.0),
    "gpt-5.4-mini": _flat(0.75, 0.075, 0.75, 4.50),
    "gpt-5.4": Price(
        Rates(2.50, 0.25, 2.50, 15.0),
        long_context=Rates(5.0, 0.50, 5.0, 22.50),
        long_context_over=272_000,
    ),
    "gpt-5.5": Price(
        Rates(5.0, 0.50, 5.0, 30.0),
        long_context=Rates(10.0, 1.0, 10.0, 45.0),
        long_context_over=272_000,
    ),
    "gpt-5.6-luna": Price(
        Rates(0.20, 0.02, 0.25, 1.20),
        long_context=Rates(0.40, 0.04, 0.50, 1.80),
        long_context_over=272_000,
    ),
    "gpt-5": _flat(1.25, 0.125, 1.25, 10.0),
    "gpt-5.1": _flat(1.25, 0.125, 1.25, 10.0),
    "gpt-5.2": _flat(1.75, 0.175, 1.75, 14.0),
}

_SNAPSHOT = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def price_for(model: str) -> Price | None:
    """The list price of an exact model ID or one of its dated snapshots."""
    if price := PRICES.get(model):
        return price
    undated = _SNAPSHOT.sub("", model)
    return PRICES.get(undated) if undated != model else None


def estimate_cost(model: str, usage: CallUsage) -> float | None:
    """Estimated USD cost of one call, or ``None`` when the model has no listed price."""
    price = price_for(model)
    if price is None:
        return None
    rates = price.standard
    if price.long_context is not None and usage.input_tokens > price.long_context_over:
        rates = price.long_context
    uncached = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    return (
        uncached * rates.input
        + usage.cache_read_tokens * rates.cache_read
        + usage.cache_write_tokens * rates.cache_write
        + usage.output_tokens * rates.output
    ) / 1_000_000
