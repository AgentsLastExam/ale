# Feature Specification: Subscription-Authenticated Harness Runs

**Feature Branch**: `008-subscription-harness-auth`

**Created**: 2026-08-10

**Last Revised**: 2026-08-11

**Status**: Draft

**Input**: Add subscription-backed operation for the Claude Code, Codex CLI, and Grok
Build Autonomous Harnesses. The official agent program continues to execute inside the
Task Sandbox. An operator signs in once with the provider's native CLI, and later Tasks
reuse that profile without a provider API key.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reuse a native subscription login (Priority: P1)

An ALE operator signs in once with the official provider CLI on an execution host. Later
ALE runs detect that native profile, copy the selected credential into the episode-local
agent home, and run the same official CLI inside the Task Sandbox without an API key or
another interactive login.

**Why this priority**: This is the requested value and reuses the existing Harness
execution topology instead of adding another agent controller.

**Independent Test**: Sign in with one official CLI, remove the provider API key, and
run several Tasks in separate ALE invocations. Each run uses the subscription profile
inside its Task Sandbox and reaches a normal terminal state without another login.

**Acceptance Scenarios**:

1. **Given** a valid native login for a supported Harness, **When** the operator starts a
   later ALE run with default authentication selection, **Then** ALE selects subscription
   mode and identifies that choice before agent launch.
2. **Given** subscription mode, **When** ALE prepares the Harness, **Then** only that
   Harness's credential files are staged into its episode-local native home and the
   official CLI executes inside the Task Sandbox.
3. **Given** a valid staged credential, **When** the provider CLI starts, **Then** it uses
   the native subscription service without an API key, custom API endpoint, or login
   prompt.
4. **Given** ten eligible Tasks started in separate ALE invocations, **When** the saved
   authorization remains usable, **Then** all ten reuse it without repeated login.
5. **Given** a login for one Harness, **When** another provider's Harness runs, **Then**
   ALE does not treat the first provider's profile as authorization for the second.

---

### User Story 2 - Choose authentication predictably (Priority: P1)

An operator can use automatic selection or explicitly choose API-key or subscription
mode. ALE reports the effective mode, requested model, and provider before agent launch,
and never silently changes modes after a provider rejects a credential.

**Why this priority**: Predictable precedence prevents unintended API charges and makes
subscription failures distinguishable from Task performance.

**Independent Test**: Exercise auto, explicit API-key, explicit subscription, expired,
revoked, missing, and wrong-model cases. Confirm the selected mode is visible and the
existing API-key path is unchanged.

**Acceptance Scenarios**:

1. **Given** both a native subscription profile and API-key configuration, **When** mode
   is `auto`, **Then** subscription wins and the API key is not used.
2. **Given** no native subscription profile, **When** mode is `auto`, **Then** ALE uses
   the existing API-key path.
3. **Given** the operator explicitly selects API-key mode, **When** a subscription
   profile exists, **Then** ALE ignores the profile and preserves Gateway behavior.
4. **Given** an expired, revoked, malformed, or inaccessible profile, **When** ALE has
   selected subscription mode, **Then** it reports the provider's native login recovery
   command and does not fall back to API-key billing.
5. **Given** the requested model is not available to the subscription account, **When**
   the provider rejects it, **Then** ALE records an entitlement failure without model or
   authentication substitution.
6. **Given** the operator uses the provider's native logout or account replacement,
   **When** a later run starts, **Then** ALE observes the new native profile state.

---

### User Story 3 - Preserve truthful evaluation records (Priority: P2)

An operator accepts that the evaluated Codex/Grok agent can read the subscription
credential staged inside its Sandbox; Claude receives only an episode token. ALE still
records the authentication path, native CLI/model, network
path, available limits, and profile lifecycle honestly while preserving Task isolation,
verification, evidence parsing, and native continuation.

**Why this priority**: Credential isolation is deliberately not a goal, but results must
distinguish opaque Codex/Grok egress from Claude's controlled Gateway relay and must not
invent provider billing observations for either path.

**Independent Test**: Run normal Tasks, an agent credential-read probe, provider quota
failure, serialized same-profile runs, retained-Sandbox handling, and native continuation.
Inspect native evidence, framework-authored records, terminal status, and provenance.

**Acceptance Scenarios**:

1. **Given** Codex/Grok subscription mode, **When** the evaluated agent reads its native
   auth home, **Then** access is allowed and does not itself invalidate the result.
2. **Given** a subscription-backed run, **When** framework-authored trace, result, and
   provenance are inspected, **Then** they identify subscription mode and a non-secret
   profile identity without intentionally serializing a raw credential.
3. **Given** subscription model traffic, **When** usage and limits are reported, **Then**
   Gateway token and cost controls are marked unavailable rather than reported as
   enforced or zero-cost; Claude still retains Gateway model authority and retry
   coalescing without a retained Transport Trace.
4. **Given** the Task declares no unrelated network access, **When** subscription mode
   runs, **Then** the Sandbox can reach the Harness's native provider hosts but gains no
   other general internet access.
5. **Given** two runs select the same mutable native profile, **When** they overlap,
   **Then** ALE serializes their complete credential-use lifecycle so the authoritative
   host profile is not corrupted.
6. **Given** the provider CLI refreshes or rotates auth state, **When** its process ends
   for any reason, **Then** ALE persists valid updated native credential state for the
   next run under the profile lock.
7. **Given** a subscription-backed Sandbox is retained, **When** ordinary credential
   cleanup succeeds or fails, **Then** ALE reports the outcome but does not destroy the
   Sandbox solely to guarantee credential confidentiality.
8. **Given** a native continuation, **When** it resumes in the same live Sandbox, **Then**
   the same authentication mode, profile identity, Harness, model, and native session are
   used or the continuation fails explicitly.

### Edge Cases

- The native provider profile does not exist, is unreadable, is a symlink, or has unsafe
  ownership or permissions.
- The provider profile exists but its access token is expired and its refresh token is
  revoked, rotated, or requires renewed consent.
- The provider stores credentials in an OS keyring rather than a portable file.
- The operator is logged in with the provider's ordinary Host profile but has not logged
  in to ALE's isolated profile; `auto` must ignore the ordinary profile.
- A Task is cancelled or the host crashes after the provider rotates the refresh token
  but before ALE copies updated state back.
- The evaluated agent intentionally deletes or modifies its staged auth file.
- A Task copies the credential into Task-declared artifacts or prints it in agent-owned
  output; those surfaces are not guaranteed secret-free.
- A provider changes required login/model hosts or the native credential file format.
- API credentials, native subscription auth, and stale custom endpoints are all present.
- The requested API model name is not an available subscription product model.
- Provider quota, rate, fair-use, or concurrency limits are reached.
- Two ALE processes select the same profile while one is refreshing it.
- A run resumes after native logout, account replacement, or auth-mode change.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: ALE MUST support native subscription-backed execution for the
  `claude-code`, `codex-cli`, and `grok-build` Autonomous Harnesses while their official
  agent programs continue to run inside the Task Sandbox.
- **FR-002**: ALE MUST reuse the official provider CLI's existing login and credential
  format; ALE MUST NOT implement a separate OAuth protocol when the native login already
  produces reusable state.
- **FR-003**: ALE MUST resolve at most one subscription profile per supported Harness
  from the current ALE checkout's gitignored `.ale/auth` directory. It MUST NOT
  discover, copy, mutate, or log out the provider's ordinary default Host profile.
- **FR-004**: Run authentication MUST support `auto`, `api-key`, and `subscription`.
  `auto` MUST select subscription when that Harness has a native profile and otherwise
  retain API-key behavior.
- **FR-005**: An explicit per-run selection MUST override `auto`; after subscription is
  selected, ALE MUST NOT fall back to API-key mode on authentication, entitlement, quota,
  or provider failure.
- **FR-006**: ALE MUST show the selected authentication mode, Harness, provider, and
  requested model before evaluated agent launch.
- **FR-007**: API-key mode MUST preserve the existing Gateway, credential, endpoint,
  accounting, and transport-trace behavior.
- **FR-008**: Subscription mode MUST ignore API-key variables and custom API endpoints
  and MUST configure the official CLI for its native subscription service.
- **FR-009**: Subscription mode MUST stage only the selected Harness's required native
  credential files or token into its episode-local agent home; it MUST NOT copy ambient
  Skills, MCP servers, histories, settings, or another Harness's auth profile.
- **FR-010**: The staged subscription credential MAY be readable and mutable by the
  evaluated agent, its tools, and its declared resources; this exposure MUST be
  documented and MUST NOT by itself invalidate the result. Task setup runs before
  Harness staging and does not receive the credential.
- **FR-011**: Subscription mode MUST validate native profile presence, ownership,
  permissions, accepted CLI version, and login usability before sending the Task
  instruction whenever the provider CLI exposes such validation.
- **FR-012**: Missing, expired, revoked, malformed, inaccessible, or incompatible native
  auth MUST produce a distinct non-result failure with the provider's login recovery
  command.
- **FR-013**: Account/model entitlement mismatch MUST fail without model substitution or
  authentication-mode fallback.
- **FR-014**: Authentication loss, provider refusal, and subscription quota, rate, or
  concurrency limits MUST remain typed non-result failures.
- **FR-015**: Subscription mode MUST grant the Sandbox only the pinned provider
  authentication/model hosts required by that Harness plus Task-declared hosts; it MUST
  NOT grant general internet access solely because subscription mode is selected.
- **FR-016**: Runs sharing one mutable native profile MUST use an exclusive host profile
  lock for staging and persistence. Version 1 MAY serialize those runs rather than merge
  concurrently refreshed credential files.
- **FR-017**: Each episode MUST keep its native home, histories, session state, evidence,
  artifacts, and continuation state separate from every other episode even when they
  originate from the same host profile.
- **FR-018**: After a mutable-profile native CLI process terminates for any reason, ALE
  MUST persist valid provider-updated credential state back to the authoritative host
  profile with an atomic replacement while holding the profile lock. Invalid or missing
  staged state MUST NOT overwrite the last host copy. Immutable provider tokens require
  no copyback.
- **FR-019**: Failure to persist refreshed auth state MUST be reported explicitly but
  MUST NOT change an otherwise completed and verified Task into a valid zero score.
- **FR-020**: Native continuation MUST bind to the original authentication mode,
  non-secret profile identity, Harness, model, native session, and existing continuation
  inputs; a mismatch MUST fail instead of starting a new session.
- **FR-021**: Framework-authored Run records MUST identify authentication mode, Harness,
  provider, native CLI version, requested/effective model, and a stable non-secret profile
  identity without intentionally recording a raw provider credential.
- **FR-022**: Run reporting MUST distinguish ALE-enforced limits from native Harness,
  provider-owned, unknown, or unavailable subscription limits and usage. It MUST NOT
  claim Gateway model-call, token, or API cost enforcement for native subscription
  traffic.
- **FR-023**: Native subscription transport MUST be represented honestly as not observed
  by the Gateway; absence of Gateway Transport Trace MUST NOT be presented as zero model
  calls.
- **FR-024**: Authentication, entitlement, provider-limit, and profile-persistence
  failures MUST be excluded from score aggregation and remain distinguishable from Task,
  Harness, infrastructure, and verifier failures.
- **FR-025**: Harness cleanup SHOULD remove the staged native credential from an ordinary
  or retained Sandbox and MUST report failure, but credential-removal failure MUST NOT
  force Sandbox destruction solely to protect the subscription secret.
- **FR-026**: A pinned upstream CLI that no longer supports the accepted native login,
  non-interactive execution, model selection, evidence, or resume contract MUST fail
  compatibility validation.

### Key Entities

- **Native Subscription Profile**: The provider CLI's durable host credential state for
  one Harness. Its raw contents are opaque to published ALE contracts and may be copied
  into an episode.
- **Authentication Selection**: The requested and effective `auto`, `api-key`, or
  `subscription` choice for one Run and its precedence source.
- **Staged Native Credential**: The episode-local credential copy used and potentially
  updated by the official CLI inside the Task Sandbox.
- **Authentication Record**: Non-secret Run provenance containing mode, Harness,
  provider, profile identity, CLI/model observations, transport visibility, and actual
  limit ownership.
- **Profile Lock**: The host-side exclusive lease that serializes staging and persistence
  for one mutable native profile.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For each supported Harness, one official native login supports 10 eligible
  Tasks in later ALE invocations with zero API keys and zero repeated interactive logins
  while the provider authorization remains usable.
- **SC-002**: At least 90% of operators already familiar with the provider CLI can start
  their first ALE subscription-backed Task within 5 minutes.
- **SC-003**: In 100% of valid-profile acceptance runs, the official agent executes
  inside the Task Sandbox and starts without an interactive login prompt.
- **SC-004**: In 100% of explicit API-key cases, existing Gateway behavior and outcomes
  remain unchanged even when a subscription profile exists.
- **SC-005**: In 100% of expired, revoked, malformed, wrong-model, and signed-out cases,
  ALE does not silently incur API-key billing or substitute another model.
- **SC-006**: Two requested Runs using one mutable profile either execute safely under
  the profile lock or are queued; the authoritative host profile is never partially
  written or concurrently overwritten.
- **SC-007**: 100% of framework-authored subscription Run records identify auth mode and
  correctly mark Gateway transport, token limits, and API cost as unavailable.
- **SC-008**: No subscription run automatically stages another Harness's profile,
  ambient native configuration, undeclared Skill, or undeclared MCP server.
- **SC-009**: 100% of authentication, entitlement, provider-limit, and profile-state
  failures remain non-result failures rather than valid zero rewards.

## Assumptions

- The three Harnesses in scope are `claude-code`, `codex-cli`, and `grok-build`.
  `openclaw-cli` remains outside this feature.
- "Without API" means no provider API key and no API-key billing. The native subscription
  product still uses provider network protocols.
- The operator has already used the official provider CLI to sign in and accepts that
  evaluated Task code can read, modify, or exfiltrate staged Codex/Grok credentials.
  Claude's provider token remains on the Host behind an episode-authenticated relay.
- The operator is authorized by the provider and organization policy to use the account
  for these non-interactive Tasks. ALE does not pool or resell consumer subscriptions.
- Version 1 uses one active native profile per Harness and serializes Runs sharing that
  mutable profile. Multi-account selection and distributed profile synchronization are
  outside scope.
- Codex and Grok login use their official home override pointed at the current checkout's
  `.ale/auth` directory. The user's ordinary `~/.codex` and `~/.grok` homes are outside
  scope and remain untouched.
- Linux file-backed native profiles are the initial target. OS-keyring-only credentials
  require a provider-supported export or token mechanism before that Harness is ready.
- Task- and agent-authored output may contain copied secrets. ALE only guarantees that
  framework-authored metadata does not intentionally serialize raw credentials.
- Verification Judges keep their existing, separately declared credential contract.
- Codex/Grok subscription traffic uses native provider egress; Claude uses a Gateway
  Bearer relay with model authority and retry coalescing. Neither receives finite Gateway
  cost/token limits, provider billing claims, or retained Transport Trace.
