# 0004 - Harness families follow loop ownership

**Status:** Accepted

## Context

Agent programs split into those that own a complete interaction loop and those that act
against a stepwise environment. Classifying them by process location was unstable, and
forcing an existing rollout agent into a framework callback loop required unnecessary
queue adapters.

## Decision

`AutonomousHarness` launches an agent-owned loop. `PolicyHarness` drives ALE's
framework-owned `TaskEnv` through `reset()` and batched `step()` calls; ALE still witnesses
every observation and action because only that environment touches the sandbox.
`StepwisePolicy` adapts callback-style policies without changing the core direction.

Both families use the same Gateway, sandbox contract, limits, ATIF trajectory, execution
trace, and provenance. Native logs are input evidence for deterministic conversion, not
canonical truth.

Each shipped autonomous Harness has a complete version-controlled preset and a strict
settings model. Unknown settings fail before provisioning. Task, preset, run, and CLI
Skill/MCP declarations form one deduplicated union; only that union is staged. MCP supports
stdio and Streamable HTTP, while vendor configuration files remain adapter output.

Native continuation is optional and resumes the exact live native session in its original
sandbox with only new input. Run resume, transcript replay, and cross-sandbox continuation
are different mechanisms. No central capability registry is added: an adapter implements
a configured behavior or rejects it explicitly.

## Consequences

Adding an agent means choosing a loop contract, strictly translating configuration and
resources, and proving deterministic evidence conversion. Existing agents do not need
their control flow inverted, and location remains an implementation detail.
