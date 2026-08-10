# 0003 — Engine and task content live in separate repositories

**Status**: accepted (2026-07-24); partly superseded by
[0017](0017-self-contained-docker-tasks.md) (2026-08-03) — engine and Task content remain
separate, but standard Tasks no longer depend on a domain manifest, repository Kit, domain
image, registry mapping, or repository-level version declaration.

## Context

Task content grows without bound (hundreds of tasks across many subject domains) and
changes on a different rhythm than the engine. Domain experts must be able to add tasks
without engine review, while the abstractions they rely on must stay under central
review. A single repository would either drown the engine in content or force every
task change through architecture review.

## Decision

- **Engine repository** (`AgentsLastExam/ale`): contracts, orchestrator, base images
  and *all* host-side extension packages. Any new `Environment`, `TaskSpec` subclass or
  judge enters here through a reviewed pull request, so abstractions are centrally
  governed and evolve atomically with the contracts.
- **Task repositories**, one per collection: task folders, sandbox-side kits, asset
  locks and domain images. They contain **no host-side code**; manifests reference
  extensions by name. The first is the existing `agents-last-exam` collection, whose
  tasks are rebuilt in place in the new format.
- `registry.toml` in the engine maps a domain to a repository **and a subpath**, and
  several domains may share one repository. Existing task identifiers therefore survive
  conversion unchanged.
- Content is consumed either by name (fetched and cached at a pinned revision) or by
  local path during authoring. Both go through the same execution path.

Packages are organised by *extension capability*, never one package per domain: most
tasks are pure data and need no code at all.

## Consequences

- Adding a task is cheap; adding an abstraction is deliberately expensive. That
  gradient is the mechanism that keeps domains on the standard components.
- Engine and content pin each other explicitly (`requires_core` on one side, a pinned
  revision on the other), so version drift is a visible, reviewed change.
- Splitting a collection into its own repository later is a registry edit, not a
  refactor.
