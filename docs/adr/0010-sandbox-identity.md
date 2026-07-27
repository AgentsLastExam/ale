# 0010 — The agent is unprivileged, and so is the oracle

**Status**: accepted (2026-07-26)

## Context

Nothing in the engine passed a user to `docker exec`, so every operation ran as whatever
the image defaulted to — root, for a sandbox image. That included the agent.

An agent with root can change the network policy it is being isolated by, the clock its
timeouts are measured against, and the guest service that drives its own sandbox. None of
that requires malice; a task that installs something system-wide, or an agent that tidies
up after itself too enthusiastically, is enough. What it costs is reproducibility: a
number obtained under conditions the subject could alter is not a measurement.

Two requirements pull against each other here. An agent must be able to reach everything
it needs, or it fails for reasons that have nothing to do with the task and report as
though they do. And an agent must not be able to reach past that, or the isolation the
result depends on is nominal.

## Decision

**The agent runs as an unprivileged user declared by the image.** The framework's own
work — installing the guest service, staging content, running the task's setup and verify
stages, collecting artifacts — runs as root, because it must succeed regardless of what
the agent did to its own workspace.

**The oracle runs as the agent.** It stands in for the agent during `ale validate`, which
is the only check a task gets before publication. An oracle with more privilege passes
exactly the tasks a real agent then fails on access alone.

**Staged content is handed over on arrival**, not corrected afterwards. The framework
creates what a task declared and gives it to the agent user immediately.

**The framework does not adjust what a task's setup produced.** A task decides what it
opens up, because only the task knows what the agent is meant to change. A correcting pass
would be the engine guessing on the task's behalf — and it would mask the very defect the
oracle's privileges are there to catch.

The contract names two roles, `framework` and `agent`, rather than a unix account. Which
account each maps to is the image's business, and the image says so.

## Consequences

- A task whose setup leaves the agent unable to write something fails its own
  `ale validate`. That is the intended trade for not correcting ownership automatically,
  and it only works because the oracle shares the agent's limits.
- A GUI task's setup, being root, must drop to the desktop user to open a window. The
  guest service supplies the session's environment so this is one `runuser`.
- Tasks that genuinely need to install software declare `resources.sudo`; see
  [0009](0009-run-level-artifact-retention.md) for why a declaration that changes
  isolation belongs in provenance.
- Building this surfaced two failures worth recording. A sudoers rule written into an
  image with no `sudo` binary succeeds, so the grant is now confirmed by using it. And the
  test fixtures had been running on an upstream image with no unprivileged account at all
  — which is what prompted [0011](0011-image-contract.md).
