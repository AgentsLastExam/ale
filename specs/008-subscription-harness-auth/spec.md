# Feature Specification: Subscription-Authenticated Harness Runs

**Feature Branch**: `008-subscription-harness-auth`

**Created**: 2026-08-10

**Last Revised**: 2026-08-11

**Status**: Implemented

**Input**: Claude Code, Codex CLI, and Grok Build continue to execute inside each Task
Sandbox. The operator logs in once using checkout-local provider state; later Tasks use
subscription entitlement without an API key or another interactive login.

## User Scenarios & Testing

### User Story 1 - Reuse one isolated login (Priority: P1)

An operator logs in once for a supported Harness. Later `auto` or explicit
`subscription` Runs reuse that login while leaving the operator's ordinary Host agent
profile untouched.

**Independent Test**: Remove the provider API key and run a normal Task with each
Harness. The official CLI runs in the Sandbox, reaches a terminal state, and does not
prompt for login.

**Acceptance Scenarios**:

1. A checkout-local login selects subscription mode before provisioning.
2. Missing, malformed, expired, or revoked state fails without API-key fallback.
3. Login state for one Harness never authorizes another Harness.
4. Ordinary `~/.claude`, `~/.codex`, and `~/.grok` state is neither discovered nor
   modified.

### User Story 2 - Keep one Harness path (Priority: P1)

The operator configures only the Harness, authentication mode, and real model name.
ALE generates any native provider section internally. API-key and subscription Runs use
the same Sandbox-side Gateway URL, episode token, model name, and CLI invocation.

**Independent Test**: Render Codex and Grok configuration in both modes and compare the
provider/model sections. They are identical; only Host Gateway authentication differs.

**Acceptance Scenarios**:

1. `--model` overrides the real model in either auth mode.
2. Codex always uses its internal ALE model provider with the requested model.
3. Grok's generated model table is keyed by the requested model, not a user-visible
   `ale` model alias.
4. No provider access token or `auth.json` enters the Task Sandbox.

### User Story 3 - Run concurrently with truthful evidence (Priority: P1)

Multiple Tasks using one subscription profile run concurrently. Normal model requests
do not lock the profile; only token refresh is serialized. Gateway limits, model
authority, replay coalescing, and Transport Trace apply to all three Harnesses.

**Independent Test**: Run two episodes with `--concurrency 2` for Codex and Grok. Both
complete using the same checkout-local profile; a transient provider 429 is retried
once and sustained provider limits remain explicit failures.

**Acceptance Scenarios**:

1. Concurrent requests reuse a current access token without acquiring the profile lock.
2. Expiration or 401 causes one locked refresh and atomic `0600` profile update.
3. Another process that already refreshed the file prevents a redundant refresh.
4. RunLock identifies subscription mode and a non-secret profile slot; raw credentials
   and profile paths are absent.
5. Transport Trace records model, usage, response identity, status, and estimated cost
   where pricing exists.

## Edge Cases

- The isolated profile is absent, symlinked, not owned by the user, or broader than
  mode `0600`.
- A provider rotates a refresh token while several ALE processes are waiting.
- Provider profile JSON, OAuth endpoint, required header, model name, or CLI config
  changes.
- Codex ChatGPT rejects public Responses fields such as `max_output_tokens`.
- Provider quota, fair-use, rate, or concurrency limits are reached.
- Credentials are stored only in an OS keychain.
- The Host crashes during refresh after the provider accepted a rotating refresh token.

## Functional Requirements

- **FR-001**: The three official agent programs MUST execute inside the Task Sandbox.
- **FR-002**: Authentication MUST support `auto | api-key | subscription`; explicit
  selection wins, and effective subscription MUST never fall back to API-key billing.
- **FR-003**: Subscription sources MUST be checkout-local `.env` or `.ale/auth` state;
  ordinary Host profiles MUST remain untouched.
- **FR-004**: The operator-facing model MUST always be the real requested model and MAY
  be overridden per Run.
- **FR-005**: ALE MUST generate required Codex/Grok provider configuration internally;
  the Sandbox-side config and launch path MUST NOT branch by auth mode.
- **FR-006**: Every model request MUST use the episode-authenticated ALE Gateway.
- **FR-007**: Provider credentials MUST remain on the Host; the Sandbox MUST receive
  only its revocable episode token.
- **FR-008**: The Gateway MUST impose the Run model, coalesce byte-identical retries,
  account provider-reported usage, apply configured ceilings, and retain Transport
  Trace for API-key and subscription traffic.
- **FR-009**: Codex/Grok access-token refresh MUST use their checkout-local provider
  file, a cross-process refresh-only lock, and atomic mode-`0600` replacement.
- **FR-010**: Normal model traffic MUST NOT hold the profile lock or serialize complete
  episodes.
- **FR-011**: Subscription 401 MUST refresh and retry at most once; subscription 429
  MUST honor `Retry-After` up to five seconds or retry once after one second.
- **FR-012**: Authentication, entitlement, compatibility, and provider-limit failures
  MUST remain distinguishable from Task performance.
- **FR-013**: Framework-authored records MUST exclude raw provider credentials and
  filesystem paths while recording auth mode, provider, profile-slot digest, and
  validated CLI version.
- **FR-014**: Codex subscription requests MUST omit `max_output_tokens`; completed usage
  still counts toward later refusals, and documentation MUST NOT claim a hard cap on the
  current response.
- **FR-015**: Subscription model routing MUST NOT grant general Sandbox egress.
- **FR-016**: Native continuation MUST bind authentication mode and profile slot along
  with Harness, model, settings, resources, episode, Sandbox, and native session.
- **FR-017**: `.ale/`, `.env`, generated smoke fixtures, and Run outputs MUST stay
  untracked and excluded from build contexts.

## Success Criteria

- **SC-001**: One login supports at least ten later sequential Runs without interaction
  while provider authorization remains valid.
- **SC-002**: Codex and Grok each complete two real episodes at concurrency two using
  one profile.
- **SC-003**: API-key and subscription config rendering differs in no Sandbox-side
  provider/model field for the same Harness/model.
- **SC-004**: No raw subscription token or refresh token appears in RunLock, result,
  Transport Trace, or framework logs.
- **SC-005**: The focused suite, repository lint, and full test suite pass.

## Assumptions and Scope

- The operator is authorized to use the selected account and provider terms permit the
  workflow.
- Provider subscription quotas and billing records remain provider-owned. ALE cost is
  only a pricing-table estimate.
- Version 1 supports Linux file-backed profiles. macOS Keychain and Windows Credential
  Manager remain a recorded TODO.
- Shared consumer-account pools, credential resale, and hosted multi-tenant brokering
  are out of scope.
