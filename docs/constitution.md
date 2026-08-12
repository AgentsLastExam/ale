# ALE Framework Constitution

Project: `ale` — the Agents' Last Exam orchestration and evaluation framework.

## Scope

This constitution governs only durable, project-wide constraints. It MUST NOT
prescribe a concrete transport, provider, package or directory layout, model-routing
mechanism, credential-delivery mechanism, runtime version, storage path, external
registry, or exact validation algorithm.

Concrete designs MUST live in versioned specifications or architecture decision
records. They MAY change without a constitutional amendment when the principles below
remain satisfied.

## Core Principles

### I. Explicit, Versioned Contracts

Data and behavior that cross a component, process, repository, or trust boundary MUST
have an explicit owner and a documented, validated contract. Persisted or public
contracts MUST be versioned, and breaking changes MUST include a migration or an
explicit compatibility boundary.

External standards MAY be adopted when interoperability is a real requirement. The
adoption MUST be specified, represented by locally owned public types or adapters, and
covered by conformance tests. An external implementation MUST NOT become an implicit
dependency of unrelated core behavior.

Rationale: implementations change more often than the agreements between them.
Explicit ownership and versioning let ALE evolve without making stored results,
task repositories, or integrations ambiguous.

### II. Clear Ownership and Dependency Direction

Every responsibility MUST have one documented owner. Orchestration, public contracts,
task-authored behavior, reusable task libraries, and infrastructure integrations MUST
interact through their published interfaces rather than private imports or ambient
state.

Dependencies MUST flow toward stable public contracts. Contract layers MUST NOT depend
on orchestration or feature implementations, and task-authored code MUST NOT acquire
undeclared host capabilities. Boundaries with meaningful integrity impact MUST be
enforced mechanically where practical.

Rationale: a boundary that exists only in a diagram decays. Clear ownership and
one-way dependencies keep components replaceable without freezing their current
implementation.

### III. Reproducible Outcomes and Honest Provenance

Every reportable evaluation outcome MUST identify the immutable inputs, code, data,
artifacts, configuration, and framework state needed to reproduce or meaningfully
compare it. Mutable references used during execution MUST be resolved to immutable
identities in provenance.

An outcome with missing required provenance, an incomplete execution stage, or a
failed measurement component MUST NOT be represented as a successful result. Resume,
deduplication, and cache identity MUST use stable content or execution identities
rather than display names, directories, or timestamps alone.

Rationale: a score without traceable inputs is not durable evidence. Provenance must
describe what actually ran, including failure, rather than what was intended to run.

### IV. Explicit Trust Boundaries and Honest Failure

Each execution phase MUST document which code is trusted, which resources it may
access, and which capabilities, credentials, or privileges it receives. Evaluated code
MUST NOT access withheld evaluation material or undeclared host capabilities. A
versioned feature contract MAY deliberately provide evaluated code with credentials or
privileges when it states their scope, exposure, lifecycle, cleanup, and provenance
behavior. Credential confidentiality from evaluated code is not a constitutional
guarantee. Framework-authored metadata MUST exclude raw sensitive values; Task- and
agent-authored output is not guaranteed to be secret-free.

Infrastructure, setup, verifier, judge, timeout, and contract failures MUST be
reported explicitly and MUST NOT be converted into a valid low score or silently
ignored. Security-sensitive paths MUST fail closed when their required guarantees
cannot be established.

Rationale: evaluation integrity depends on describing the actual boundary, not imposing
one credential-delivery design on every Harness. Separating measurement failure from
measured performance prevents corrupted results from looking legitimate.

### V. Minimal Surface and Evidence-Based Generalization

ALE MUST keep the mandatory task-author and operator surface as small as the current
requirements allow. Existing language, platform, and repository capabilities MUST be
preferred over new abstractions, dependencies, configuration, or extension points.

Domain-specific behavior MUST remain local until multiple concrete uses demonstrate a
stable shared contract. Hypothetical scale, future providers, or unrequested
compatibility MUST NOT justify production complexity. Public compatibility guarantees
MUST be explicit and proportional to actual consumers.

Rationale: each mandatory concept becomes permanent coordination cost. Generalization
is valuable only after repeated evidence reveals what is actually shared.

### VI. Verifiable Changes and Normative Documentation

Changes to public contracts, trust boundaries, persisted artifacts, or cross-component
behavior MUST update the relevant normative specification in the same change. New
architectural decisions or reversals MUST be recorded in an ADR. Implementation details
MUST NOT be promoted into this constitution merely to make a current design harder to
change.

Behavior-changing work MUST include tests proportional to its risk, and user-facing
workflows MUST have an executable validation path. Failed required checks MUST block
publication or release. Exact test matrices, task-admission criteria, and release gates
belong to their owning specifications and workflows.

Repository artifacts MUST use standard English and the canonical terms defined in the
project lexicon.

Rationale: principles stay durable only when current behavior is testable and recorded
at the correct level of authority.

## Governance

- Authority: this constitution supersedes ad-hoc practice across ALE engine and task
  repositories. Normative specifications and ADRs MAY impose stronger local
  requirements but MUST NOT weaken these principles.
- Amendments: a constitutional amendment MUST change a durable project-wide principle,
  the scope rule, or governance itself. Concrete implementation decisions MUST instead
  update their owning specification or ADR. Every amendment MUST include a Sync Impact
  Report and approval from the central maintainer.
- Versioning: MAJOR removes or redefines a principle or governance guarantee; MINOR
  adds a principle or materially expands governance; PATCH clarifies wording without
  changing obligations.
- Compliance: every feature plan MUST check the current principles before
  implementation and again after design. A conflict requires either changing the
  design or explicitly amending this constitution; it MUST NOT be waived silently.
- Review: repository reviews MUST verify applicable constitutional principles and the
  current normative specs independently. Compliance with one does not imply compliance
  with the other.

**Version**: 4.0.0 | **Ratified**: 2026-07-24 | **Last Amended**: 2026-08-11
