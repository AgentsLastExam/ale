# 0006 - Evidence has one owner and retention is run policy

**Status:** Accepted

## Context

Task authors know which paths represent output; operators know whether a particular run
should copy or retain them. Mixing these decisions in the Task made sweeps expensive and
created several competing sources of truth. Likewise, duplicated conversations and
scalar rewards made evidence ambiguous.

## Decision

A Task declares only unique, non-overlapping absolute artifact paths. Run configuration
selects `artifacts.collect = "host"|"none"`. `host` captures immutable regular-file or
directory snapshots after Harness cleanup and enables restoration to a separate verifier.
`none` performs no inspection, spool, copy, or restoration.

Run policy independently selects `destroy` or `keep` for solver and verifier sandboxes.
Destroy is the default. Retained sandboxes are sanitized first; failed sanitation forces
destruction. Result and RunLock record actual Provider outcomes and actionable handles.

Canonical episode evidence is split by ownership: ATIF trajectory for agent-visible
interaction, Gateway transport trace for solver model calls and accounting, execution
trace for framework and Task-stage activity, verification record for reward derivation,
result for terminal outcome, and RunLock for immutable provenance. Large payloads are
content-addressed blobs. Native logs are retained only by run policy or when conversion
needs diagnostic evidence.

A completed result contains a non-empty finite named reward map. ALE does not invent a
primary reward. Missing provenance, incomplete stages, malformed verification, and
infrastructure failure cannot be represented as a completed low score. Resume and
deduplication use stable execution identities, not display names or timestamps.

## Consequences

Large score-only sweeps avoid artifact transfer, while debug runs keep evidence without
changing Task identity. Each fact has one authoritative record, and adapters to scalar or
foreign formats must make any lossy aggregation explicit.
