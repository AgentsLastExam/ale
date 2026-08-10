# Task design principles

This document defines what makes a Task valid independent of its domain. It is intentionally
small. The concrete file format lives in [task-folder.md](task-folder.md), the evaluation
flow in [standard-environment.md](standard-environment.md), and the implementation
workflow in [the authoring guide](../guides/task-authoring.md).

A Task is a contract among three things:

- **instruction** — what the agent is asked to achieve;
- **context** — the locally built image, its baked solver inputs, staged verification
  assets, tools, services, and initial sandbox state;
- **verification** — how the final result is judged.

All three must describe the same task.

## Preconditions

The principles below matter only after these two conditions hold:

1. **The environment runs faithfully.** Every required dependency, input, service,
   permission, and other resource exists in the sandbox and behaves as the instruction
   assumes.
2. **The verifier is correct.** It completes without reading the wrong state, distinguishes
   infrastructure failure from a zero score, gives the untouched initial state zero, and
   reports a known oracle's actual result without rewriting it.

A failure of either condition is a broken Task, not agent performance.

## P1: Verify outcomes, not paths

Reward must depend on the requested final output or final state. It must accept every valid
way to produce that result and must not require the agent to follow the author's setup,
tool choice, command sequence, or trajectory.

Bad: ask the agent to obtain a product price, then score whether a particular browser
instance opened a particular page.

Good: ask the agent to write the price to a specified file, then verify the required
contents of that file.

Trajectory may be part of the scored outcome only when behavior itself is explicitly part
of the task, such as a requirement to call a named service or avoid a prohibited operation.

## P2: Align instruction, context, and verification

Every scored requirement must be stated in the instruction, and every stated requirement
must be feasible in the supplied context and covered by verification.

In particular:

- do not score persistence, formatting, fonts, metadata, or side effects the instruction
  did not require;
- do not require a resource or real-world state that the sandbox does not provide;
- compare only the properties that define correctness, not an entire gold artifact when
  irrelevant differences would cause failure;
- state exact output paths, formats, tolerances, and constraints when verification depends
  on them.

## P3: Bound side effects only when they are requirements

Advanced tasks may also protect state that the agent must not change. Such penalties are
valid only when the instruction explicitly forbids the change or the protected invariant
is an unavoidable part of the stated task contract.

Prefer verifying protected final state. Do not penalize generic "extra work" discovered in
a trajectory: that silently invents a requirement and binds reward to a completion path.

## Required acceptance conditions

Before use, every Task must satisfy:

- **untouched zero:** after setup, running verify without an agent produces the complete,
  non-empty reward map with every reward exactly `0.0`;
- **observable oracle:** the Task's oracle completes with the same reward names and its
  actual values are recorded. Full credit is the design target; partial credit remains
  visible for the user to accept, reject, or improve;
- **explicit failure:** setup, verifier, Judge, dependency, and infrastructure failures are
  reported as errors, never converted into synthetic zero rewards.

`ale validate` checks these conditions in independent episodes. It does not silently turn a
partial oracle into full credit or confuse it with infrastructure failure.

## How to use trajectory

Run at least one real agent after static review and validation. Use its trajectory to find
missing context, ambiguous instructions, accidental shortcuts, verifier blind spots, and
unstated scoring requirements.

The trajectory is evidence about the Task, not a target for the verifier. Never change
reward merely to make the observed agent pass. Change it only when the evidence shows that
the current Task violates the principles above.

## Review order

1. **Design:** write the intended outcome, supplied context, and scoring criteria together.
2. **Pre-run:** run `ale lint` and `ale validate`, then manually confirm the built image,
   pinned inputs, and generated state when the Task depends on external context.
3. **Post-run:** run a real agent and audit its trajectory against the contract without
   fitting the verifier to that particular run.
