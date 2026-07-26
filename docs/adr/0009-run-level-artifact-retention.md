# 0009 — A task names its outputs; a run decides whether to keep them

**Status**: accepted (2026-07-25)

## Context

`artifacts` began as a list of paths, then briefly grew a per-entry disposition
(`collect: host | none`) so a task could say a path was scratch rather than product.

That conflated two questions asked by different people at different times. A task author
knows which paths hold the result — that is domain knowledge, and nobody else has it. An
operator knows whether *this run* wants a copy — a sweep of ten thousand episodes that
only needs scores should not pay to copy gigabytes it will never open, while a single
debugging run wants everything, and both may run the identical task file.

With the disposition on the task, changing retention meant editing a task repository, or
adding a per-task override to the run configuration and having two places to look.

## Decision

`artifacts` is a list of absolute paths. A task states where its output lives, and
nothing more.

Retention is `artifacts.collect` in the run configuration (`host` | `none`), covered by
`config_hash` so provenance records what a run actually kept.

The mechanism is the sink, not a branch. `run_episode` builds either the collecting sink
or one that discards, and environments collect unconditionally — so no phase, and no
future environment, needs to know the policy exists.

Remote destinations (object storage, a results service) extend this field. They were the
reason a disposition looked attractive on the task in the first place, and they belong
here for the same reason retention does: where the data goes is the operator's question.

## Consequences

- Task files get shorter and stop encoding operator concerns.
- A large sweep and a debugging run differ by one flag, with no task edits.
- `config_hash` changes when retention changes, so two runs that kept different things
  are correctly not the same run.
- A task cannot mark a path scratch-only. If that turns out to matter, it belongs in
  `metadata` as advice, not as a directive — the run still decides.
