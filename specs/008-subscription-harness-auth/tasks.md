# Tasks: Subscription-Authenticated Harness Runs

> Phases 1-6 record the implementation checkpoint in commit `fe00cd0`. Phase 7 records
> the validated simplification that supersedes guest credential staging, native CONNECT
> routing, copyback, and complete-episode profile locks.

**Input**: Design documents from `/specs/008-subscription-harness-auth/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Tests are required because authentication, profile persistence, network policy,
and provenance are trust-boundary behavior.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup and Design Alignment

**Purpose**: Incorporate the operator's requirement that ALE never mutates the user's
ordinary Host agent profiles.

- [X] T001 Update `/home/weichen/ale/ale/specs/008-subscription-harness-auth/{spec.md,plan.md,research.md,data-model.md,quickstart.md,contracts/native-profile.md}` so Codex/Grok use a gitignored checkout-local profile and never discover or write the user's default native profile
- [X] T002 Verify existing ignore rules cover Python, Node, `.env`, native auth state, and generated Run data in `/home/weichen/ale/ale/.gitignore` and `/home/weichen/ale/ale/.dockerignore`; add only missing critical patterns

---

## Phase 2: Foundational Authentication Infrastructure

**Purpose**: Add the shared configuration, lifecycle, and proxy capability used by all
three Harnesses.

**⚠️ CRITICAL**: No provider integration starts until this phase passes its unit tests.

- [X] T003 Write failing authentication-selection and provenance model tests in `/home/weichen/ale/ale/tests/unit/test_subscription_auth.py`
- [X] T004 Implement `auto | api-key | subscription` configuration, effective authentication metadata, CLI `--auth`, and typed subscription failures in `/home/weichen/ale/ale/packages/ale-core/src/ale/core/{config.py,harness.py,lock.py,errors.py}` and `/home/weichen/ale/ale/packages/ale-run/src/ale/run/cli/main.py`
- [X] T005 Write failing isolated profile resolution, cross-process lock, guest staging, validation, and atomic copyback tests in `/home/weichen/ale/ale/tests/unit/test_subscription_auth.py`
- [X] T006 Implement the minimum stdlib profile lifecycle in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/subscription.py`
- [X] T007 Write failing tests for an authenticated EgressProxy session without a model Gateway in `/home/weichen/ale/ale/tests/unit/test_egress_proxy.py`
- [X] T008 Reuse `SessionRegistry` for subscription-only proxy sessions and conditional Gateway startup in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{episode.py,cli/main.py}`

**Checkpoint**: Authentication selection, dedicated Host state, locking/copyback, and
native proxy egress work without any live provider account.

---

## Phase 3: User Story 1 - Reuse a Native Subscription Login (Priority: P1) 🎯 MVP

**Goal**: Run each official CLI inside the Task Sandbox with an ALE-owned reusable
subscription credential and no API key.

**Independent Test**: Fake native credentials produce correct isolated guest homes,
provider-native configuration, child environments, and copyback without reading the
user's default Host profiles.

### Tests for User Story 1

- [X] T009 [US1] Add failing API-key/subscription configuration and environment precedence tests for all three Harnesses in `/home/weichen/ale/ale/tests/unit/{test_claude_code.py,test_codex_cli.py,test_grok_build.py}`

### Implementation for User Story 1

- [X] T010 [US1] Implement Claude setup-token subscription launch and exact compatible pin in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/harnesses/claude_code.py` and `/home/weichen/ale/ale/packages/ale-run/src/ale/run/presets/claude-code.toml`
- [X] T011 [US1] Implement Codex built-in ChatGPT subscription configuration, staging, and cleanup copyback in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/harnesses/codex_cli.py`
- [X] T012 [US1] Implement Grok native subscription configuration, staging, cleanup copyback, and workspace-upload opt-out in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/harnesses/grok_build.py`
- [X] T013 [US1] Add a provider-independent in-Sandbox subscription lifecycle integration test in `/home/weichen/ale/ale/tests/integration/test_subscription_auth.py`

**Checkpoint**: The three official Agent programs remain Sandbox-side and API-key mode
still produces its previous Gateway configuration.

---

## Phase 4: User Story 2 - Choose Authentication Predictably (Priority: P1)

**Goal**: Make selection, precedence, diagnostics, and no-fallback behavior observable
before agent launch.

**Independent Test**: Auto, explicit API-key, explicit subscription, missing profile,
stale API settings, and unsupported Harness cases resolve deterministically without
provisioning or unintended API credentials.

### Tests for User Story 2

- [X] T014 [US2] Add CLI and orchestration tests for precedence, missing/revoked profile diagnostics, unsupported Harnesses, provider limits, and no API fallback in `/home/weichen/ale/ale/tests/unit/test_subscription_auth.py`

### Implementation for User Story 2

- [X] T015 [US2] Complete preflight, operator diagnostics, provider-host resolution, and non-result failure mapping in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{subscription.py,cli/main.py}`
- [X] T016 [US2] Update CLI help and shipped Harness presets for the authentication contract in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/cli/main.py` and `/home/weichen/ale/ale/packages/ale-run/src/ale/run/presets/{claude-code.toml,codex-cli.toml,grok-build.toml}`

**Checkpoint**: The selected mode is visible and never changes after preflight.

---

## Phase 5: User Story 3 - Preserve Truthful Evaluation Records (Priority: P2)

**Goal**: Record credential exposure and unavailable Gateway observations honestly while
preserving native evidence, verification, continuation, and retention.

**Independent Test**: A subscription Run contains correct non-secret auth provenance,
no Gateway Transport Trace, an agent-readable staged credential, safe copyback, and typed
cleanup/persistence failures.

### Tests for User Story 3

- [X] T017 [US3] Add RunLock, transport absence, continuation binding, retained-Sandbox cleanup, and raw-credential metadata tests in `/home/weichen/ale/ale/tests/unit/test_subscription_auth.py` and `/home/weichen/ale/ale/tests/integration/test_subscription_auth.py`

### Implementation for User Story 3

- [X] T018 [US3] Implement authentication provenance, continuation fingerprint binding, truthful transport applicability, and best-effort staged-auth cleanup in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{provenance.py,episode.py,environments/standard.py}`
- [X] T019 [US3] Add ADR 0007 and synchronize implementation-facing documentation in `/home/weichen/ale/ale/docs/adr/0007-native-subscription-authentication.md` and `/home/weichen/ale/ale/docs/specs/{autonomous-harness.md,security.md,standard-environment.md,task-folder.md,trace.md,lexicon.md}`

**Checkpoint**: Subscription failures are non-results and published records never claim
Gateway visibility that did not exist.

---

## Phase 6: Polish and Acceptance

**Purpose**: Prove the offline implementation, then perform the only steps requiring the
operator's real accounts.

- [X] T020 Run focused unit/conformance/integration tests plus `just lint` and `just test` from `/home/weichen/ale/ale`, fixing only regressions caused by this feature
- [X] T021 Reuse the three checkout-local native logins and run live subscription Tasks for Claude, Codex, and Grok from `/home/weichen/ale/ale/specs/008-subscription-harness-auth/quickstart.md`
- [X] T022 Fix and test the effective subscription allowlist plus Docker host-proxy routing in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{environments/standard.py,providers/docker.py}`
- [X] T023 Add and live-test the Claude subscription Bearer relay with no Transport Trace in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{gateway/server.py,cli/main.py,harnesses/claude_code.py}`
- [X] T024 Replace the legacy Codex preset default with `gpt-5.6-luna` and remove the subscription-only model rewrite from `/home/weichen/ale/ale/packages/ale-run/src/ale/run/subscription.py`

## Deferred TODOs

- [ ] T025 Support provider-native macOS Keychain and Windows Credential Manager subscription profiles without reading or mutating the user's ordinary Host agent profile

---

## Phase 7: Unified Gateway Simplification

**Purpose**: Make authentication a Host-only routing choice, restore concurrency and
Gateway evidence, and remove the provider-specific Sandbox branches introduced by the
first implementation.

- [X] T026 Checkpoint the first complete implementation as commit `fe00cd0` for replay
- [X] T027 Replace Codex/Grok guest auth staging, copyback, native CONNECT routing, and episode-wide `ProfileLease` with checkout-local Host `SubscriptionCredential` refresh in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/subscription.py`
- [X] T028 Route Claude, Codex, and Grok subscription model traffic through the existing Gateway, including provider headers, Codex's `/responses` path, one 401 refresh retry, and one bounded 429 retry in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/{cli/main.py,gateway/server.py}`
- [X] T029 Make Codex and Grok generate the same Gateway configuration in API-key and subscription modes, use the real requested model name, and remove native-auth launch branches in `/home/weichen/ale/ale/packages/ale-run/src/ale/run/harnesses/{codex_cli.py,grok_build.py}`
- [X] T030 Remove `subscription_credential`, guest lifecycle hooks, subscription-only SessionRegistry flow, and unavailable-trace handling from `/home/weichen/ale/ale/packages/{ale-core/src/ale/core/harness.py,ale-run/src/ale/run/episode.py,ale-run/src/ale/run/environments/standard.py}`
- [X] T031 Replace staging/serialization tests with Host-header, refresh-only locking, unified config, Gateway 401/429, Codex field compatibility, and no-guest-auth tests in `/home/weichen/ale/ale/tests/{unit,integration}`
- [X] T032 Live-test Codex and Grok sequentially and at concurrency two with checkout-local profiles; record ADR 0008 and synchronize feature/normative documentation

---

## Dependencies and Execution Order

- Phase 1 precedes all code because dedicated ALE-owned profiles replace discovery of
  ordinary Host profiles.
- Phase 2 blocks every user story.
- US1 supplies the working provider paths; US2 completes their selection/diagnostics;
  US3 completes persistence and truthful records.
- T021 is the only task that requires operator credentials or interactive action.
- Phase 7 supersedes the Phase 2-6 transport/lifecycle implementation while preserving
  their authentication-selection and checkout-isolation outcomes.

## Parallel Opportunities

After Phase 7, episodes sharing one profile run concurrently. Only refresh-token
rotation is serialized; provider account limits remain independent throughput ceilings.

## Implementation Strategy

1. Keep login state checkout-local.
2. Keep one Sandbox-side Gateway configuration per Harness.
3. Vary only Host upstream authentication.
4. Validate sequential and concurrent real Tasks before release.
