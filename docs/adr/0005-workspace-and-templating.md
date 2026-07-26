# 0005 — A fixed workspace replaces path templating; variants stay parameters

**Status**: accepted (2026-07-24); partly superseded by [0008](0008-task-owned-paths.md) (2026-07-25) — the
fixed workspace is gone; a task declares its own absolute paths. The placeholder triage
and strict rendering below still stand.

## Context

In the legacy framework, task prompts were Python f-strings interpolating path
properties (`input_dir`, `remote_output_dir`, `task_dir`, `software_dir`,
`python_wrapper`, …). Measured across the 157 legacy tasks, these path derivations are
roughly 95% of all placeholders (`remote_output_dir` 125 uses, `input_dir` 117,
`task_dir` 44). They existed for one reason: a single Python class had to serve both a
Windows root (`E:\agenthle`) and a Linux root (`/media/user/data/agenthle`).

A task now declares exactly one image, so that reason is gone. But two other kinds of
placeholder are real and must survive: variant parameters, and values that only exist
at run time.

## Decision

Every sandbox exposes one fixed workspace — `/ale/input`, `/ale/software`,
`/ale/output`, `/ale/work` during the agent phase, and `/ale/reference` at verification
only. Instructions therefore state paths **literally**, and the linter rejects legacy
path patterns.

Placeholders are triaged into three kinds:

- **Path derivations** — frozen to literal workspace paths at conversion time.
- **Variant parameters** — kept as `${name}`, filled from a task's `params` merged with
  the variant's `params`. `string.Template` syntax is used because instructions
  routinely contain JSON and code braces.
- **Run-time discovered values** — never templated. Setup writes a file (for example
  `/ale/input/brief.md`) and the instruction tells the agent to read it.

Rendering is strict: an unresolved placeholder or an unused declared parameter fails to
load. The `TaskSpec` stores the **rendered** instruction, so `taskspec_hash` covers
exactly what the agent saw, and each variant has its own identity. Variants are expanded
by the taskset into one task each; `TaskSpec.family` keeps the variant-stripped
identifier for aggregation.

## Consequences

- Conversion of the legacy corpus is mostly mechanical deletion, with a linter that
  catches the residue.
- Prompts are static and hashable, which is what makes provenance and resume identity
  meaningful.
- Dynamic content costs one file read for the agent, and buys leak-free, reproducible
  instructions.
