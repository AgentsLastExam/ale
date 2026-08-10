# 0001 - ALE owns its contracts and current documentation

**Status:** Accepted

## Context

Harbor, Prime Verifiers, and other evaluation systems provide useful formats, but each
also carries assumptions about Tasks, execution, and storage. ALE additionally used
feature specifications as implementation workspaces; after several revisions, historical
decisions obscured the current contract.

## Decision

ALE defines boundary-crossing and persisted contracts in `ale-core` as strict typed
models with canonical serialization. Third-party compatibility is implemented by edge
adapters and conformance tests; an upstream format does not implicitly define unrelated
core behavior.

Maintained project knowledge lives under `docs/`:

- `constitution.md` contains durable project-wide principles;
- `specs/` contains the current normative behavior;
- `adr/` contains the rationale for current cross-module architectural choices;
- `guides/` contains workflows that link back to, but do not redefine, contracts.

Top-level feature specifications are temporary planning artifacts. When a feature lands,
its durable behavior moves into the owning living specification. Reversed decisions are
removed or consolidated from the current ADR set; Git history retains the former text.

## Consequences

Readers have one current path instead of a supersession graph. Contract changes update
the model, its living specification, and proportionate tests together. ALE carries the
cost of maintaining its own schemas, while interoperability remains replaceable at the
edges.
