"""Typed error taxonomy.

Every failure in Orbit maps to exactly one class here, and every class maps to exactly
one terminal :class:`~orbit.core.verdict.Status`, but a Sandbox lost to memory or storage, which
is the agent's or the Task's by whose code ran there. That mapping is what lets the run layer
decide retries, and what makes "why did this run stop?" answerable from a record rather
than from a log.

Add a new class only when it implies a *different* decision by the caller.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "AgentRefusalError",
    "AgentResourceConflictError",
    "AgentResourceError",
    "AgentUnsupportedError",
    "AllocationEndedError",
    "AssetError",
    "BudgetExceededError",
    "ConfigError",
    "EnvironmentError_",
    "GuestUnreachableError",
    "HarnessLimitError",
    "IncompleteTrainingDataError",
    "MemoryExhaustedError",
    "ModelProviderError",
    "NativeContinuationError",
    "OrbitError",
    "OutputStreamError",
    "PhaseTimeoutError",
    "ProvenanceIncompleteError",
    "ProviderCapabilityError",
    "ProviderCapacityError",
    "ProviderStartError",
    "RegistryError",
    "ResourceExhaustedError",
    "RetentionFinalizationError",
    "SandboxHandOff",
    "StorageExhaustedError",
    "SubscriptionAuthenticationError",
    "SubscriptionCompatibilityError",
    "SubscriptionEntitlementError",
    "SubscriptionProfileError",
    "SubscriptionUnavailableError",
    "TaskDefinitionError",
    "TaskError",
    "TornJsonlError",
    "TrajectoryConversionError",
    "TrajectoryReferenceError",
    "VerificationAdapterError",
    "VerificationConfigurationError",
    "VerificationInfrastructureError",
    "VerificationProviderError",
    "VerificationRequestError",
    "VerificationVerdictError",
    "VerifierOutputError",
]


class OrbitError(Exception):
    """Base class for every error the framework raises deliberately."""


# --- Configuration and task definition -------------------------------------------
# These are authoring or invocation mistakes: they surface before any sandbox exists.


class ConfigError(OrbitError):
    """Invalid configuration: unknown key, bad value, contradictory layers."""


class AgentResourceError(ConfigError):
    """A declared Skill or MCP resource is invalid or cannot be resolved."""


class AgentResourceConflictError(AgentResourceError):
    """One logical resource name resolved to different content."""


class AgentUnsupportedError(ConfigError):
    """The selected harness cannot honor a configured feature."""


class SubscriptionUnavailableError(ConfigError):
    """The requested Harness has no isolated Orbit subscription credential."""


class SubscriptionCompatibilityError(ConfigError):
    """The pinned native CLI cannot honor the subscription contract."""


class RegistryError(OrbitError):
    """A task reference could not be resolved to content."""


class TaskDefinitionError(OrbitError):
    """A Task folder or its ``task.toml`` is invalid."""


class AssetError(OrbitError):
    """A declared asset could not be fetched, authenticated, or staged."""


# --- Environment ------------------------------------------------------------------


class EnvironmentError_(OrbitError):
    """The sandbox side failed for reasons unrelated to the agent or the task."""


class RetentionFinalizationError(EnvironmentError_):
    """A requested debug sandbox could not be sanitized and retained."""


class ProviderCapabilityError(EnvironmentError_):
    """The task requires a capability the selected provider does not offer."""


class ProviderCapacityError(ProviderCapabilityError):
    """The provider serves the task, but not now: what it needs is in use. A Fleet Node answers
    that it has no room for its grant now, and the Sandbox is placed again, there or
    elsewhere."""


class ProviderStartError(EnvironmentError_):
    """A sandbox could not be provisioned or did not become ready."""


class SandboxHandOff(EnvironmentError_):
    """The Provider holds no Sandbox past the deadline of the process that drives it, as a
    cluster's episode job holds none past its wall time: it cannot boot from ``handle``
    here, and has made the snapshot durable for the same Provider in a later process."""

    def __init__(self, handle: str) -> None:
        super().__init__(f"snapshot {handle} goes on in the episode's next process")
        self.handle = handle


class GuestUnreachableError(EnvironmentError_):
    """The guest service stopped answering mid-episode."""


class AllocationEndedError(GuestUnreachableError):
    """The scheduler that held the Sandbox's allocation ended it before the episode did: its
    wall time ran out, a job of higher priority preempted it, or its node failed."""


class ResourceExhaustedError(GuestUnreachableError):
    """The Sandbox, or the guest service in it, was lost to its memory or its storage: the
    agent's result in the Sandbox it ran in from its phase on, and a Task defect in setup or
    in a separate or snapshot verifier."""


class MemoryExhaustedError(ResourceExhaustedError):
    """It was lost after an OOM kill in it, for the memory its Task declares or for the
    memory around it."""


class StorageExhaustedError(ResourceExhaustedError):
    """It wrote past the ``storage_mb`` its Task declares, which a Provider that measures
    what a Sandbox writes, rather than giving it a disk of that size, ends it for."""


class SubscriptionProfileError(EnvironmentError_):
    """A mutable native subscription profile could not be staged or persisted."""


class SubscriptionAuthenticationError(EnvironmentError_):
    """The native provider rejected or could not refresh the subscription login."""


class SubscriptionEntitlementError(EnvironmentError_):
    """The subscription account cannot use the requested model or capability."""


class ModelProviderError(EnvironmentError_):
    """The model provider was failing the agent's calls, as the Gateway saw, when its program
    gave up: overloaded, limiting their rate, failing, not reached, or refusing them for the
    account's quota. Not the agent's result: nothing scores it."""


# --- Agent ------------------------------------------------------------------------


class AgentError(OrbitError):
    """The agent failed in a way that is attributable to the agent."""


class AgentRefusalError(AgentError):
    """The agent declined the task (safety refusal, policy stop)."""


class NativeContinuationError(AgentError):
    """A native session cannot continue under the requested conditions."""


class TrajectoryConversionError(AgentError):
    """Harness-native evidence could not be converted to canonical ATIF."""


class TrajectoryReferenceError(TrajectoryConversionError):
    """A trajectory call, result, subagent, or continuation reference is invalid."""


class IncompleteTrainingDataError(OrbitError):
    """Exact token-level evidence required for a training export is absent."""


class TornJsonlError(OrbitError):
    """A JSONL file ends with an incomplete or malformed record."""


class OutputStreamError(EnvironmentError_):
    """Progressive sandbox output could not be delivered or finalized."""


# --- Task-side failures -----------------------------------------------------------


class TaskError(OrbitError):
    """The task's own machinery failed — a task defect, not an agent result."""


class VerifierOutputError(TaskError):
    """Verification crashed, or produced missing/malformed rewards."""


class VerificationRequestError(TaskError):
    """Task-authored verification input is invalid."""


class VerificationInfrastructureError(EnvironmentError_):
    """Framework judge configuration, transport, or execution failed."""


class VerificationConfigurationError(VerificationInfrastructureError):
    """A requested Judge has no usable run configuration or credential."""


class VerificationProviderError(VerificationInfrastructureError):
    """A direct Judge provider request failed."""


class VerificationAdapterError(VerificationInfrastructureError):
    """A local Agent Judge adapter is unavailable or failed."""


class VerificationVerdictError(VerificationInfrastructureError):
    """A Judge exhausted recovery without a valid scored verdict."""


# --- Budgets and deadlines --------------------------------------------------------


class PhaseTimeoutError(OrbitError):
    """A phase exceeded its declared timeout."""

    def __init__(
        self, phase: str, limit_sec: float, *, native_session_id: str | None = None
    ) -> None:
        super().__init__(f"phase {phase!r} exceeded its {limit_sec:g}s timeout")
        self.phase = phase
        self.limit_sec = limit_sec
        #: The native session an agent's run that its limit cut left, which it can resume.
        self.native_session_id = native_session_id


class BudgetExceededError(OrbitError):
    """A turn, token or model-call ceiling was reached; the gateway refused to continue."""

    layer = "gateway"

    def __init__(
        self,
        limit: str,
        value: float,
        observed_value: float | None = None,
    ) -> None:
        super().__init__(f"budget limit {limit!r} reached ({value:g})")
        self.limit = limit
        self.value = value
        self.observed_value = observed_value


class HarnessLimitError(BudgetExceededError):
    """A harness-native control, rather than the Gateway, stopped the agent."""

    layer = "harness"


# --- Provenance -------------------------------------------------------------------


class ProvenanceIncompleteError(OrbitError):
    """A result would be reported without a complete provenance record.

    Raised instead of emitting the result: an unattributable number is worse than no
    number at all.
    """
