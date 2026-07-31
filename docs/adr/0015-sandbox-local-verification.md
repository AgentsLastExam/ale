# ADR 0015: Sandbox-Local Verification

**Status**: Accepted

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

LLM and Agent Judges execute directly inside the completed sandbox during the trusted
verify phase. Credentials exist only in the verify command environment. Agent Judges use
small package-owned `codex-cli` and `claude-code` adapters rather than solver Harnesses.

The sandbox atomically maintains `verification.json`. After `verify/run.sh` exits,
`ale-run` collects and validates that record, the reward envelope, and an optional raw
Agent transcript. There is no live Host callback, snapshot revision, Judge Gateway
session, or Agent-Judge trajectory.

Repository Domain Kits are flat importable packages at `kits/<package>/__init__.py`.
Their staged bytes are hashed per episode; `kit.toml` and `kits.lock.yaml` are not used.

## Consequences

- Task scoring order and semantics remain visible in one Python program.
- `ale_verify` imports neither `ale.core` nor `ale.run`.
- Verification failures remain explicit and cannot become zero rewards.
- Selected images must provide Python 3.12+ and any configured Agent CLI.
- Endpoint preflight, dynamic Python installation, and Harbor export remain separate
  future work.
