# 0005 - One standard lifecycle owns staging and verification timing

**Status:** Accepted

## Context

Security cannot depend on a conventional reference path or a Task remembering a secret
flag. An earlier Verification Service also split scoring across sandbox code, Host
callbacks, Gateway sessions, and Harness machinery.

## Decision

`StandardEnvironment` owns the linear provision, setup, agent/oracle, evidence capture,
verify, and teardown order for both container and VM Tasks. Setup and verification run as
trusted root with open egress; solver and oracle run as the image user under Task network
policy. Phase deadlines and cancellation-safe teardown are framework enforced.

Withholding is staging time plus framework-directory permissions. `verify/` is uploaded
only after the solver and Harness cleanup finish. `oracle/` is uploaded only for an oracle
episode. Stage-local assets arrive with their stage directory and use ordinary relative
paths.

Shared verification runs in the completed solver sandbox. Separate verification starts a
fresh independently resourced sandbox, optionally with its own local or referenced image,
and restores only immutable declared artifact snapshots to their original absolute paths.
It does not rerun setup or copy the solver filesystem.

`ale_verify` is an independent sandbox-local Python package. One stateful `Verification`
program owns checks, stats, direct LLM/Agent Judges, aggregates, and atomic writing of
`verification.json` and the reward envelope. Judges call their configured endpoints
directly from the verification sandbox. Run configuration owns model, endpoint,
credential variable, reasoning effort, Agent adapter, and exact CLI version; Task code
owns rubric, prompt, evidence, and invocation-local MCP.

There is no Domain Kit, Host Verification Service, live record upload, Judge Gateway,
Agent-Judge ATIF trajectory, or solver-Harness reuse. Verification and infrastructure
failures are explicit and never become zero rewards.

## Consequences

Task scoring remains one readable program and behaves identically in shared or separate
placement. Answers are absent during solver execution, and records preserve completed
criteria and failed Judge attempts for diagnosis.
