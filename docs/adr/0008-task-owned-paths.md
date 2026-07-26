# 0008 — A task owns its paths; the framework owns the timing

**Status**: accepted (2026-07-25)

**Supersedes**: the fixed-workspace decision in [0005](0005-workspace-and-templating.md)
and the workspace half of [0007](0007-store-and-workspace.md). The rest of both stands:
0005's placeholder triage and strict rendering, 0007's content-derived store key.

## Context

ADR 0005 fixed one sandbox layout — `/ale/input`, `/ale/software`, `/ale/output`,
`/ale/work`, and `/ale/reference` at verification only — so that instructions could
state literal paths instead of interpolating them. That part worked, and it is why
prompts are static and hashable today.

Two problems showed up once other domains were considered against it.

The layout is a guess about what every future domain needs. A simulation domain wants
scenes where its simulator expects them; a robotics stack has its own conventions; a
desktop task cares about a home directory. Each would either adapt to names that mean
nothing to it, or the enum would grow a member per domain — and a fixed enum that grows
per caller was never fixed.

More seriously, the layout was carrying a security guarantee it could not hold.
`/ale/reference` was "the verify-only directory", which reads as though the path is what
protects the answers. It is not. What protects them is that verify-stage material is
uploaded during scoring and is therefore absent while the agent works. A named location
invites code that checks the name, and a check is something a future path can forget.

ADR 0007 then had to work around the fixed layout: pre-baking wants many tasks' data in
one image, and one fixed location holds exactly one task's data. That produced a
store-plus-workspace split whose only job was to undo the constraint.

## Decision

**A task declares its own absolute paths.** Each asset mount names a `dest`; each
artifact names a `path`. There is no framework-wide layout, and nothing in the engine
knows the string `/ale/input`.

The framework creates exactly three things: every declared mount destination, every
declared artifact path, and the run's scratch directory (`work_dir`, default
`/ale/work`, a run-level setting). Anything else a task needs, its own setup script
creates. It keeps `/ale/kits`, the stage directories and the rewards file for its own
machinery, because it has to put those somewhere.

**Withholding is timing, not location.** A stage's material — its assets, its folder —
reaches the sandbox when that stage runs. `verify/` and `oracle/` are absent during the
agent phase for the same reason, and no flag marks anything secret.

The store from 0007 stays, and gets simpler: it is keyed by content
(`sha256(repo + revision + path)`) and materialises into whatever destination a task
asked for, rather than into a fixed workspace. Pre-baking remains possible for the
original reason — many tasks' data can coexist under distinct keys.

`setup.prebakeable` is deleted. Assets are already immutable and always bakeable; setup
scripts are per-episode and never are. The flag distinguished nothing.

## Consequences

- A domain with an unusual shape adapts nothing. This is the change that makes the
  engine plausible beyond the first corpus.
- A task that writes to a directory it never declared fails in its own setup, rather
  than succeeding by accident on an image that happened to have it. That is a real
  behaviour change, and it surfaced two demo tasks relying on it.
- The security property is now enforced by the schedule the framework controls, and is
  stated that way in `docs/security-model.md`.
- Instructions still state literal paths — they are just the task's own literals now.
  0005's rendering rules are untouched.
- Migration from the legacy corpus is unaffected: most rebuilt tasks keep the old
  `/ale/...` paths out of habit, which is fine. They simply declare them.
