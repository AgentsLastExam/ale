# 0012 — The agent drives a stepwise environment

**Status**: accepted (2026-07-26)

## Context

The policy harness family asked the agent for one decision at a time:
`decide(observation) → actions`, with the framework owning the loop. The reasoning was
that owning the loop is what lets the framework witness every step.

Most agents are not shaped that way. Gymnasium, cua-lite and verifiers all hand an agent
an environment and let it run. Adapting cua-lite's desktop agent to our callback shape
meant running it in a background task against an environment shim fed by queues — and its
single-step entry point turned out to raise `NotImplementedError` by design, with a
comment saying rollouts go through `sample()`. The inversion worked, and existed for no
reason other than the direction of the call.

## Decision

**A `PolicyHarness` is handed a `TaskEnv` and drives it**: `reset()`, then `step(actions)`
until the result says done.

Nothing is given up. The framework still witnesses every step, because the guarantee never
came from who calls whom — it comes from the environment being ours and being the only
thing that touches the sandbox. It is in fact stronger: the step ceiling and the
no-progress limit now live inside `step()`, so they bind an agent that has never heard of
them, where before a guard only bound agents that went through our loop.

`step` takes a **batch** of actions, because "click here, then type this" is one decision.

`StepResult` is **not** gymnasium's five-tuple. Our reward comes from the verify stage
after the agent phase ends, so a per-step reward would be a field that is always zero, and
a shape that lies for the sake of familiarity is worse than one that needs an adapter.
The field exists and stays absent.

`StepwisePolicy` supplies the loop for agents that would rather be asked than drive, so
neither style has to adapt to the other.

## Consequences

- The queue-based shim is deleted. It was the symptom.
- Adapting an existing step-driven agent becomes a translation of its action vocabulary
  rather than an inversion of its control flow.
- The training seam the constitution reserves lines up naturally: what a trainer wants to
  hold is exactly this.
- `Environment` keeps its own meaning — provisioning, scoring, provenance — and is not
  conflated with the stepwise view. A trainer holding a `TaskEnv` should not also be
  holding sandbox lifecycle and verification.
