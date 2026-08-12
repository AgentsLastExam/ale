# Implementation Plan: Subscription-Authenticated Harness Runs

**Branch**: `008-subscription-harness-auth` | **Date**: 2026-08-11 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/008-subscription-harness-auth/spec.md`

## Summary

Add `auto | api-key | subscription` authentication selection to the `claude-code`,
`codex-cli`, and `grok-build` Autonomous Harnesses. In subscription mode, the existing
official CLI still installs and runs inside the Task Sandbox:

- Claude receives an episode token and sends sampling requests through the existing Host
  Gateway, which relays them with the checkout-local `CLAUDE_CODE_OAUTH_TOKEN`.
- Codex and Grok receive only their provider-owned `auth.json` in the existing
  episode-local native home; updated valid files are copied back atomically.
- Codex/Grok provider TLS uses the existing authenticated `EgressProxy`; Claude uses the
  Gateway as a Bearer relay without retained Transport Trace because its pinned native
  binary does not reliably honor authenticated CONNECT proxy settings.

There is no host-side agent controller, OAuth implementation, persistent home mount,
multi-account manager, or new login command. Version 1
serializes ALE episodes that share one mutable Codex or Grok profile.

## Technical Context

**Language/Version**: Python 3.12+; the existing exact-pinned Node.js agent CLIs

**Primary Dependencies**: Existing Pydantic, Typer, asyncio, ALE `Sandbox`,
`SessionRegistry`, and `EgressProxy`; Python stdlib `fcntl`, `json`, and `os.replace`; no
new runtime dependency

**Storage**: ALE-owned provider-native profiles below the current checkout's
gitignored `.ale/auth`, an episode-local native home, and existing
RunLock/result artifacts; no credential database and no access to ordinary Host profiles

**Testing**: pytest unit/conformance/integration suites plus opt-in live tests with
operator-owned subscription accounts; `just lint && just test`

**Target Platform**: Linux execution hosts using Docker or QEMU Sandboxes; file-backed
Codex/Grok profiles and Claude setup tokens

**Project Type**: Python CLI and orchestration library

**Performance Goals**: Authentication selection adds no network round trip; immutable
Claude profiles retain normal episode concurrency; mutable Codex/Grok profiles queue
instead of racing

**Constraints**: Official agent runs in the Task Sandbox; staged Codex/Grok credentials
are readable by evaluated code; no silent API fallback; API-key behavior stays unchanged;
Codex/Grok native subscription traffic has no Gateway accounting, replay, or limits;
Claude subscription traffic keeps Gateway model authority and duplicate-request
coalescing but publishes no Transport Trace or provider billing claim; framework-authored
records never intentionally contain raw credential values

**Scale/Scope**: Three existing Autonomous Harnesses, one discovered profile per Harness
and local operator, one host, no account pool or distributed credential synchronization

## Constitution Check

*GATE: Passed before Phase 0 research and re-checked after Phase 1 design.*

| Principle | Pre-research gate | Post-design evidence |
|-----------|-------------------|----------------------|
| I. Explicit, Versioned Contracts | PASS: authentication selection, credential exposure, native egress, persistence, and provenance require written contracts. | PASS: `contracts/` defines selection, per-provider staging, copyback, failure, and RunLock behavior. |
| II. Clear Ownership and Dependency Direction | PASS: providers own credential formats; `ale-run` owns resolution and staging; Harnesses own native launch configuration. | PASS: credential files remain opaque bytes except bounded validity checks; existing Sandbox and Harness interfaces perform all guest work. |
| III. Reproducible Outcomes and Honest Provenance | PASS: selected mode, profile slot, CLI/model, and unavailable Gateway observations must be recorded. | PASS: the data model adds non-secret authentication provenance and makes native transport visibility explicit. |
| IV. Explicit Trust Boundaries and Honest Failure | PASS under Constitution 4.0.0: the feature contract may intentionally expose credentials to evaluated code. | PASS: the spec states that exposure; withheld verification material remains inaccessible; auth, entitlement, quota, and persistence failures remain non-results. |
| V. Minimal Surface and Evidence-Based Generalization | PASS: reuse native login, current Harnesses, Sandbox file APIs, and existing Gateway/proxy services. | PASS: no controller, OAuth client, new proxy, database, dependency, or multi-account abstraction is added. |
| VI. Verifiable Changes and Normative Documentation | PASS: the trust-boundary reversal and new topology require normative docs, an ADR, and live checks. | PASS: the plan updates the affected specs, adds ADR 0007, and defines deterministic plus opt-in live validation. |

Provider terms are a release gate, not a constitutional gate. Version 1 is limited to an
operator using their own authorized account; shared consumer-account pools remain out of
scope.

## Project Structure

### Documentation (this feature)

```text
specs/008-subscription-harness-auth/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── native-profile.md
│   └── run-authentication.md
└── tasks.md                 # generated later by /speckit-tasks
```

### Source Code (repository root)

```text
ale/
├── packages/
│   ├── ale-core/src/ale/core/
│   │   ├── config.py                    # AgentConfig.authentication
│   │   ├── harness.py                   # effective auth fields in HarnessSession
│   │   └── lock.py                      # non-secret authentication provenance
│   └── ale-run/src/ale/run/
│       ├── cli/main.py                  # --auth resolution; conditional Gateway/proxy
│       ├── episode.py                   # proxy session or untraced Claude relay
│       ├── subscription.py              # resolve, lock, stage, validate, atomic copyback
│       ├── gateway/session.py           # existing episode capability registry
│       ├── gateway/proxy.py             # existing CONNECT proxy
│       ├── gateway/server.py            # Claude subscription Bearer relay
│       └── harnesses/
│           ├── claude_code.py           # setup-token branch and 2.1.227 pin
│           ├── codex_cli.py             # built-in ChatGPT provider branch
│           └── grok_build.py            # built-in subscription model branch
├── tests/
│   ├── unit/                            # selection, precedence, files, provenance
│   ├── conformance/                     # both modes for three shipped Harnesses
│   ├── integration/                     # in-Sandbox auth, egress, lock, copyback
│   └── acceptance/                      # opt-in live subscription checks
└── docs/
    ├── adr/0007-sandbox-native-subscription-auth.md
    └── specs/
        ├── autonomous-harness.md
        ├── lexicon.md
        ├── security.md
        ├── standard-environment.md
        ├── task-folder.md
        └── trace.md
```

**Structure Decision**: Add one small `ale-run/subscription.py` module because profile
resolution, cross-process locking, guest file transfer, and atomic host replacement are
shared by Codex and Grok. Provider-specific filenames, environment cleanup, config, model
selection, and evidence remain in their existing Harness modules. Reuse `SessionRegistry`
for Codex/Grok proxy sessions and the existing Gateway session lifecycle for Claude.

## Implementation Design

### 1. Resolve authentication before provider credentials

Add `AgentConfig.authentication = "auto"` and CLI `--auth`. Resolution order is CLI,
Run config/preset, then the field default. Effective behavior:

- explicit `api-key`: existing credential/Gateway setup, ignoring native profiles;
- explicit `subscription`: require the Harness-native source or fail before provisioning;
- `auto`: choose subscription when its source exists, otherwise API-key.

Once subscription is effective, no auth, model, quota, rate, or compatibility failure
may retry through the API-key path.

### 2. Reuse official credentials inside the Sandbox

| Harness | Host source | Episode target | Native subscription launch |
|---------|-------------|----------------|----------------------------|
| Claude Code | `CLAUDE_CODE_OAUTH_TOKEN` created by `claude setup-token` | Host Gateway Bearer relay; clean guest `CLAUDE_CONFIG_DIR` | episode `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`; native `claude --print`; never `--bare` |
| Codex CLI | `<checkout>/.ale/auth/codex-cli/auth.json` | `<home>/.codex-ale/auth.json` | omit ALE custom provider; force ChatGPT login method and file credential store; native `codex exec` |
| Grok Build | `<checkout>/.ale/auth/grok-build/auth.json` | `<home>/.grok-ale/auth.json` | omit custom `model.ale`; unset API/custom proxy values; pass the requested native model |

Only the Codex/Grok credential file crosses the Sandbox boundary; Claude receives an
episode token. Ambient provider
configuration, ordinary Host profiles, history, sessions, Skills, MCP, plugins, and
hooks do not. Operators create the isolated file profiles by running the official login
command once with `CODEX_HOME` or `GROK_HOME` pointed at the current checkout's
gitignored `.ale/auth` path; ALE does not add a login wrapper.

### 3. Serialize mutable profiles and persist refresh

For Codex and Grok, acquire a host `flock` keyed by Harness and canonical profile path
before copy-in and hold it until Harness cleanup has read the guest file. Valid nonempty
JSON replaces the host file using a sibling temporary file, mode `0600`, `fsync`, and
`os.replace`; invalid or missing guest state preserves the last host copy. Copyback runs
after any CLI termination because refresh may precede a Task failure. Claude setup tokens
are immutable and need neither lock nor copyback.

The lock intentionally serializes full same-profile episodes in version 1. Because the
profile lives in an ALE-only home, ordinary interactive Codex/Grok processes never share
or race with it. Operators must not deliberately start another native process against
that ALE-owned home during a Run.

### 4. Reuse current host networking capabilities

For Codex/Grok, create a `SessionRegistry` and `GatewaySession` only as the existing
per-episode proxy capability. Start `EgressProxy` and give the session the union of Task
hosts and pinned Harness hosts:

- Codex: `chatgpt.com`, `auth.openai.com`;
- Grok: `cli-chat-proxy.grok.com`, `auth.x.ai`.

Claude starts the existing Gateway with Bearer upstream authentication and gives the
Sandbox only its episode token and reachable base URL. The subscription session is not
attached to Transport Trace or finite Gateway limits, but the shared Gateway still imposes
the Run model and coalesces identical retries. API-key mode continues to use the
Gateway-owned registry with full accounting and trace exactly as before.

### 5. Record only truthful observations

Extend RunLock agent provenance with effective auth mode, selection source, a stable
non-secret profile-slot digest, and transport visibility. API-key Runs retain Gateway
limits and Transport Trace. Codex/Grok subscription Runs mark Gateway model-call/token/cost
limits, usage, and trace as unavailable rather than zero. Claude subscription Runs also
mark provider billing and retained trace unavailable while accurately retaining the
Gateway's model-control role. Existing native trajectory, evidence, Task isolation,
verification, and continuation remain authoritative.

## Delivery Order

1. Add config/provenance contracts and pure authentication selection tests.
2. Add the shared file-profile helper and standalone proxy-session path with fake
   credential integration tests for both Docker and QEMU interfaces.
3. Add Claude subscription branching and pin `2.1.227`; verify token precedence and
   that `--bare` is absent.
4. Add Codex built-in ChatGPT branching, profile serialization, and copyback tests.
5. Add Grok built-in subscription branching, profile serialization, copyback, and
   workspace-upload opt-out.
6. Run API-key regression/conformance suites, then opt-in live tests for login reuse,
   refresh, entitlement, quota, continuation, and endpoint allowlists.
7. Land ADR 0007 and the synchronized normative/operator documentation.

## Release Gates

- The exact pinned CLI passes one native subscription Task and two native continuations
  inside each Sandbox provider.
- An agent can read its staged Codex/Grok credential while Claude sees only an episode
  token; framework-authored RunLock/result/trace contain no raw provider credential.
- Codex/Grok refresh and cancellation tests preserve a valid host profile and two ALE
  processes cannot update it concurrently.
- Provider endpoint drift fails visibly rather than opening general egress.
- API-key mode remains byte-for-byte equivalent in generated native configuration where
  authentication does not require a branch.
- Local self-use terms are documented; hosted shared-account use is not enabled.

## Complexity Tracking

No constitution violations. The deliberate credential exposure is permitted by
Constitution 4.0.0 and specified explicitly. The one-profile lock is the accepted
version-1 throughput ceiling; multiple independently logged-in profiles can be designed
later only if real demand requires concurrency.
