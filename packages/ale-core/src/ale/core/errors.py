"""Typed error taxonomy.

Every failure in ALE maps to exactly one class here, and every class maps to exactly
one terminal :class:`~ale.core.verdict.Status`. That mapping is what lets the run layer
decide retries, what keeps failed episodes out of score aggregates, and what makes
"why did this run stop?" answerable from a record rather than from a log.

Add a new class only when it implies a *different* decision by the caller.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "AgentRefusalError",
    "AleError",
    "AssetError",
    "BudgetExceededError",
    "ConfigError",
    "EnvironmentError_",
    "GuestUnreachableError",
    "PhaseTimeoutError",
    "ProvenanceIncompleteError",
    "ProviderCapabilityError",
    "ProviderStartError",
    "RegistryError",
    "TaskDefinitionError",
    "TaskError",
    "VerifierOutputError",
]


class AleError(Exception):
    """Base class for every error the framework raises deliberately."""


# --- Configuration and task definition -------------------------------------------
# These are authoring or invocation mistakes: they surface before any sandbox exists.


class ConfigError(AleError):
    """Invalid configuration: unknown key, bad value, contradictory layers."""


class RegistryError(AleError):
    """A task reference could not be resolved to content."""


class TaskDefinitionError(AleError):
    """A task manifest is invalid, or its instruction failed to render."""


class AssetError(AleError):
    """A declared asset could not be fetched, authenticated, or staged."""


# --- Environment ------------------------------------------------------------------


class EnvironmentError_(AleError):
    """The sandbox side failed for reasons unrelated to the agent or the task."""


class ProviderCapabilityError(EnvironmentError_):
    """The task requires a capability the selected provider does not offer."""


class ProviderStartError(EnvironmentError_):
    """A sandbox could not be provisioned or did not become ready."""


class GuestUnreachableError(EnvironmentError_):
    """The guest service stopped answering mid-episode."""


# --- Agent ------------------------------------------------------------------------


class AgentError(AleError):
    """The agent failed in a way that is attributable to the agent."""


class AgentRefusalError(AgentError):
    """The agent declined the task (safety refusal, policy stop)."""


# --- Task-side failures -----------------------------------------------------------


class TaskError(AleError):
    """The task's own machinery failed — a task defect, not an agent result."""


class VerifierOutputError(TaskError):
    """Verification crashed, or produced missing/malformed rewards."""


# --- Budgets and deadlines --------------------------------------------------------


class PhaseTimeoutError(AleError):
    """A phase exceeded its declared timeout."""

    def __init__(self, phase: str, limit_sec: float) -> None:
        super().__init__(f"phase {phase!r} exceeded its {limit_sec:g}s timeout")
        self.phase = phase
        self.limit_sec = limit_sec


class BudgetExceededError(AleError):
    """A turn, token or cost ceiling was reached; the gateway refused to continue."""

    def __init__(self, limit: str, value: float) -> None:
        super().__init__(f"budget limit {limit!r} reached ({value:g})")
        self.limit = limit
        self.value = value


# --- Provenance -------------------------------------------------------------------


class ProvenanceIncompleteError(AleError):
    """A result would be reported without a complete provenance record.

    Raised instead of emitting the result: an unattributable number is worse than no
    number at all.
    """
