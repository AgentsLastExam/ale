# 0001 — Contracts first, compatibility last

**Status**: accepted (2026-07-24)

## Context

Several mature agent-evaluation frameworks exist (Harbor, Prime's verifiers, NeMo Gym).
The cheapest short-term move would be to adopt one framework's task format as our own,
or to fork it. But formats encode their authors' assumptions: Harbor's task directory
is a thin TOML contract that cannot express task families, programmatic generation, or
domain extensions; verifiers' models are typed and expressive but pre-1.0 and coupled
to one vendor's infrastructure.

## Decision

Every shape that crosses a boundary — `TaskSpec`, `Trace`, `Verdict`, `RunLock`, task
identifiers, resource references — is defined in `ale-core` as a typed model with
canonical JSON serialization, designed from first principles for clarity and
scalability. Contracts are not derived from, and not constrained by, any third-party
format.

Interoperability with other frameworks is delivered by adapters at the edges, written
last. Their only requirement on the core is that our expressive power is a superset of
theirs, which is satisfied by construction.

Directory layouts are loader conveniences, not contracts: a task folder is one way to
materialise a `TaskSpec`, never the definition of one.

## Consequences

- Adopting a foreign format later is an adapter, not a migration.
- We own our schema evolution and versioning; no upstream release can break a run.
- We carry the cost of designing and maintaining the schemas ourselves, which is the
  point: they are the asset with the longest life in this project.
