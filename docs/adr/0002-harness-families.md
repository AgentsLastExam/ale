# 0002 — Harness families are defined by loop ownership

**Status**: accepted (2026-07-24)

## Context

We must support two very different kinds of agent: coding agents such as Claude Code,
which are given a prompt and run their own loop until they finish, and GUI or policy
agents, which are asked for one action at a time against an observation.

The obvious naming axis — *where the agent process runs* — is wrong. Claude Code can be
installed inside the task sandbox, but it can equally run outside it against exposed
sandbox interfaces. Location is an implementation detail, not a category.

## Decision

Harness families are named after **who owns the interaction loop**:

- `AutonomousHarness` — the agent owns the loop. The framework supplies a prompt and
  accepts the result; intermediate calls are the agent's business. Location-independent.
- `PolicyHarness` — the framework owns the observe/act loop and calls the harness for
  each step's decision. Action spaces are pluggable.

Both families route model traffic through the gateway and reach sandboxes through the
guest service, so both produce the same unified two-layer trace. A migrated agent's
native logs are preserved as episode artifacts rather than replacing our trace.

Rejected names: `InstalledHarness` / `StepwiseHarness` (encode location or granularity
rather than the real distinction) and `ProgramHarness` (too broad — everything is a
program).

## Consequences

- Adding an agent means choosing a family, not inventing an integration style.
- Existing agent implementations are adapted, not rewritten: the adapter maps their
  outputs onto our action model and points their client at the gateway.
- Tracing, limits and provenance are uniform across both families, so results from a
  CLI agent and a GUI agent are comparable.
