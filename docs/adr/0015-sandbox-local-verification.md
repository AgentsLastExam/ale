# ADR 0015: Sandbox-Local Verification

**Status**: accepted (2026-07-31); partly superseded by
[0017](0017-self-contained-docker-tasks.md) and
[0019](0019-stage-local-task-assets-and-verification-topology.md). Sandbox-local
`ale_verify` and direct Judges remain; Domain Kits are removed and verification may use a
separate sandbox.

**Date**: 2026-07-31

## Context

The first verification-kit implementation placed Task-facing code inside `ale-run` and
used a Host Verification Service for state snapshots and Judge execution. That split a
Task's scoring program across Python, Host services, Gateway sessions, and Harness
machinery.

## Decision

`ale-verify` is an independent Python 3.12+ package. ALE stages its installed bytes into
the verify-stage `python3`; Task code owns checks, Judges, aggregation, and final writing
through one stateful object.

LLM and Agent Judges execute directly inside the active verification sandbox during the
trusted verify phase. Credentials exist only in the verify command environment. Agent
Judges use package-owned `codex-cli` and `claude-code` adapters rather than solver
Harnesses.

The sandbox atomically maintains `verification.json`. After `verify/run.sh` exits,
`ale-run` collects and validates that record, the reward envelope, and an optional raw
Agent transcript. There is no live Host callback, snapshot revision, Judge Gateway
session, or Agent-Judge trajectory.

Task-specific verifier helpers remain under `verify/`. Reusable stable primitives belong
in `ale_verify`; repository Domain Kits do not exist in the current contract.

## Consequences

- Task scoring order and semantics remain visible in one Python program.
- `ale_verify` imports neither `ale.core` nor `ale.run`.
- Verification failures remain explicit and cannot become zero rewards.
- Selected images must provide Python 3.12+. A configured Agent Judge CLI is observed and
  dynamically installed at its exact run-configured version when needed.
- Endpoint preflight and Harbor export remain separate future work.
