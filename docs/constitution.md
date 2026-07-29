<!--
Sync Impact Report
==================
Version change: 1.0.1 → 2.0.0 (canonical trajectory and task-admission revision)
Modified principles:
  - I. Contracts First, Compatibility Last → Contracts First, Standards Deliberately
  - II. Unified Data Shapes, Delegated Behavior: canonical run-artifact vocabulary
  - IV. Structural Decoupling Over Discipline: explicit recording handles
  - V. Sandbox Security & Result Integrity by Default: transport trace ownership
  - VII. Naming & Language Discipline: canonical run-artifact vocabulary
  - Development Workflow & Quality Gates: oracle plus all-ones named rewards
Added sections: none
Removed sections: none
Templates requiring updates:
  - .specify/templates/plan-template.md ✅ aligned (generic "Constitution Check" gate
    derives from this file at plan time; no edit needed)
  - .specify/templates/spec-template.md ✅ no constitution references; no edit needed
  - .specify/templates/tasks-template.md ✅ no constitution references; no edit needed
Propagation:
  - .specify/memory/constitution.md ✅ synchronized
  - ale/docs/constitution.md ✅ synchronized
  - ale/docs/specs/lexicon.md ✅ synchronized
Follow-up TODOs:
  - Design docs in /home/weichen/ale/.scratch/ (00–05) are informative background;
    they are working notes, not normative.
-->

# ALE Framework Constitution

Project: `ale` — the Agents' Last Exam core orchestration framework
(engine repo `AgentsLastExam/ale`; per-domain task repos `ale-<domain>-tasks`;
assets on Hugging Face org `agents-last-exam`; images on GHCR org `AgentsLastExam`).

## Core Principles

### I. Contracts First, Standards Deliberately

All cross-boundary data shapes — `TaskSpec`, ATIF Trajectory, Transport Trace,
Execution Trace, Episode Result, `RunLock`, task IDs, resource references — MUST be
typed models owned by `ale-core` and serialized as canonical JSON. Contracts SHOULD be
designed from first principles, but MAY deliberately adopt a stable published
interchange standard when direct interoperability is an approved feature requirement.
Such adoption requires explicit specification and constitution review, strict local
models, namespaced ALE extensions, and conformance tests; it MUST NOT require runtime
imports from the external framework. Formats not selected as canonical remain edge
adapters and MUST NOT distort unrelated core contracts. `ale-core` is the single package
that task repos and extensions may depend on.

Rationale: contracts outlive implementations. Owning strict local types preserves ALE's
control, while deliberate use of a mature interchange standard avoids maintaining a
private duplicate when direct compatibility is itself the requirement.

### II. Unified Data Shapes, Delegated Behavior

What is uniform across all domains and MUST NOT be forked: task ID namespace, ATIF
Trajectory, Transport Trace, Execution Trace, Episode Result and status taxonomy,
`RunLock` schema, Run Status Projection, sandbox leasing, gateway routing of
model/judge traffic, external resource pinning, and task admission gates. What is
delegated to domains: `Environment` orchestration internals, `TaskSpec` extension
fields, taskset/variant generation, scoring logic and judges, sandbox-side kits, and
image contents. Host-side extensions (`Environment`/`TaskSpec` subclasses, judges)
MUST live in the engine repo and enter only via reviewed PRs; task repos MUST contain
zero host-side code. Domain-specific experiments start in namespaced `extras` and are
promoted into core schemas only after demonstrated cross-domain need.

Rationale: results are comparable only if the nouns are shared; innovation is safe
only if the verbs are free. Central review of extensions is the design-quality gate.

### III. Full Provenance, Reproducible by Construction

Every run MUST produce a complete `RunLock`: task repo URL + commit, `TaskSpec`
content hash, image reference resolved to sha256 digest (never a bare tag), assets
revisions, kit versions, agent harness name + version + integrity, model ID, judge
model + prompt hash when used, config hash, seed, and framework version + commit.
A result without full provenance is invalid and MUST NOT be reported. Resume MUST be
idempotent, keyed by content hash — never by directory names or timestamps.

Rationale: a benchmark number that cannot answer "exactly what produced you?" is not
a number; for a long-lived research project this is the difference between a
leaderboard and folklore.

### IV. Structural Decoupling Over Discipline

Module boundaries are enforced by structure, not convention:

- Gateway ⊥ Provider: the gateway is a standalone host-side HTTP service whose only
  interface is a sandbox-reachable URL + bearer token; it MUST NOT import or know any
  provider. Wiring the route is the provider's plumbing job.
- GuestServer ⊥ Provider: all in-sandbox capabilities (exec, file transfer,
  observation) go through `ale-guestd`, preinstalled in base images, stdlib-only; OS
  differences are absorbed inside it. Providers only attach a transport.
- Capability injection: `Environment` implementations receive framework handles
  (`ctx.sandboxes`, `ctx.gateway`, `ctx.artifacts`, `ctx.budget`, `ctx.trajectory`,
  `ctx.transport`, `ctx.execution`, `ctx.blobs`, `ctx.result`) and MUST NOT construct
  infrastructure clients directly.
- Dependency direction: extensions → `ale-core` only; no core → extension, no
  extension → extension, no engine → task-repo imports. Enforced by import linting
  in CI, not by review vigilance.

Rationale: decoupling that relies on discipline decays; decoupling that relies on
structure compounds.

### V. Sandbox Security & Result Integrity by Default

Sandboxes default to network-blocked with the gateway as the sole egress; any wider
access MUST be declared per task (`allowlist`/`open`). Real credentials MUST never
enter a sandbox — agents see only the gateway URL and a per-episode bearer token.
All model and judge traffic MUST flow through the gateway, where limits (turns,
tokens, cost ceilings) are enforced by refusal and every call is recorded into the
Transport Trace. Task materials invisible to agents (`task.yaml`, `verify/`, `oracle/`) MUST
never be mounted or uploaded into the agent phase. Budget and timeout overruns MUST
terminate episodes with typed statuses, never hang or silently truncate.

Rationale: evaluation integrity is a systems property. Under future RL optimization
pressure, every unenforced boundary becomes a reward hack.

### VI. Minimal Surface, Deliberate Extension

Prefer the core standard components — `StandardEnvironment`, `ManifestTaskset`, base
images — before writing anything new. A minimal valid task is three files
(`task.yaml`, `instruction.md`, `verify/`); authoring simple tasks MUST stay this
cheap. Adding tasks is cheap (domain repo PR, CI-gated); adding extensions is
deliberately expensive (engine repo PR, design review) — this friction gradient is
intentional and MUST be preserved. Features for hypothetical scale (cloud providers,
training integration, pooling) are seams to keep open, not code to write early:
never write code for training now, and never design in a way that walls it off.

Rationale: the framework's long-term scalability is bounded by how small its
mandatory surface stays, not by how many features it ships.

### VII. Naming & Language Discipline

Every core noun has exactly one meaning, recorded in the project lexicon: `Sandbox`
(execution instance — never called "environment"), `Environment` (how a task becomes
an episode — its only meaning), `TaskSpec`, `Taskset`, `Episode`, `Run`, `Harness`
(`AutonomousHarness` / `PolicyHarness`), `GuestServer`, `Gateway`, `Kit`, ATIF
Trajectory, Transport Trace, Execution Trace, Episode Result, Blob, Native Log, Run
Status Projection, `RunLock`. New names MUST be narrow rather than broad, MUST be
checked against the lexicon for collisions, and conversational shorthand MUST NOT
enter code without vetting. All repository artifacts — code, comments, docstrings,
docs, commit messages — MUST be standard English.

Rationale: in a multi-domain, multi-team codebase, ambiguous names are compounding
debt; the lexicon is the cheapest architecture document we will ever maintain.

## Architectural Constraints

- Repo topology: engine monorepo (`packages/ale-core`, `packages/ale-run`, host-side
  extension packages, `registry.toml`, base image builds) + one task repo per domain
  (task folders, kits, `assets.lock.yaml`, domain Dockerfiles). Task consumption
  resolves via `registry.toml` or explicit local paths; both go through the same
  execution path.
- Task repo and task folder layout MUST follow the task folder specification
  (manifest-driven, ID derived from path, visibility rules, verify/oracle contract).
- All domain images MUST build `FROM` an official base image
  (`sandbox-base-cli` / `sandbox-base-gui`); base images preinstall `ale-guestd`.
- Large assets live on HF (`agents-last-exam`) pinned by revision; images live on
  GHCR (`AgentsLastExam`) pinned by digest in `RunLock`. Neither belongs in git.
- Heavy dependencies (simulators, ML stacks) belong in images or kits, never in
  host-side package dependencies.

## Development Workflow & Quality Gates

- Task admission: `ale validate` MUST execute the task's oracle through the ordinary
  agent path, execute the real verifier, and pass only when the verifier emits a
  non-empty finite named-reward map whose every value is exactly `1.0`. Missing
  oracles, empty or malformed rewards, thresholds, and manual bypasses are invalid.
  `ale validate` and `ale lint` MUST pass in the task repo's CI before a task is
  runnable by name.
- Cross-repo pinning: task repos declare `requires_core` ranges and pin the engine
  version in CI; the engine pins task-repo commits for its smoke suite. Pin bumps
  are explicit, reviewed changes.
- Extension PRs to the engine repo MUST include: the concrete need no core component
  can express, the `extras`-stage evidence where applicable, and conformance-test
  coverage via the `ale-core` testkit.
- Phasing discipline: local-first (Docker + QEMU before any cloud), eval-first
  (training seams reserved, not implemented). Each phase declares explicit
  non-goals; scope creep across a phase boundary requires a stated decision.
- Failure handling: all errors map to the typed status taxonomy; retry policy is
  driven by error class, and failed episodes MUST NOT contaminate aggregates.

## Governance

- Authority: this constitution supersedes ad-hoc practice for all ALE repos (engine
  and task repos). The central maintainer (Weichen) approves amendments and all
  engine-repo extension PRs.
- Amendments: proposed as a PR modifying this file, including a Sync Impact Report
  (version bump, affected principles, propagation to templates/docs). Semantic
  versioning: MAJOR for principle removals/redefinitions, MINOR for new or
  materially expanded principles/sections, PATCH for clarifications.
- Compliance: every feature plan MUST pass the Constitution Check gate against the
  current version before implementation; violations require a written justification
  in the plan's complexity tracking table or a constitution amendment — never a
  silent exception. Reviews of engine-repo PRs verify Principles I–VII explicitly.
- The design notes under `.scratch/` are informative background, not normative;
  where they conflict with this constitution, the constitution wins.

**Version**: 2.0.0 | **Ratified**: 2026-07-24 | **Last Amended**: 2026-07-29
