# Phase 0 Research: Sandbox-Native Subscription Authentication

**Research snapshot**: 2026-08-11

## 2026-08-11 Validated Revision

The first implementation below was checkpointed as commit `fe00cd0`, then superseded
after deeper source inspection and live tests proved a simpler topology:

- Codex ChatGPT requests use the Responses body at
  `chatgpt.com/backend-api/codex/responses` with a Bearer access token and
  `ChatGPT-Account-ID`.
- Grok subscription requests use the selected cli-chat-proxy dialect with a Bearer
  access token and fixed official CLI identity headers.
- Both request shapes can pass through the existing ALE Gateway while the official
  agent CLI and tool loop remain inside the Task Sandbox.
- xAI's current source refreshes standard OIDC state from the issuer discovery document
  and atomically updates the saved access/refresh token. Codex's current source refreshes
  at `auth.openai.com/oauth/token` and persists rotated fields. ALE implements only these
  two bounded checkout-local refresh exchanges; it does not install another Host CLI.
- Normal requests need no profile lock. A lock is required only during refresh-token
  rotation, and another process's fresh file is reused after the lock is acquired.

Live evidence on the pinned versions:

| Harness | Sequential Task | Two episodes, concurrency two |
|---|---:|---:|
| Codex CLI `0.146.0`, `gpt-5.6-luna` | pass, reward 1.0 | 2/2 pass |
| Grok Build `0.2.112`, `grok-4.5` | pass, reward 1.0 | 2/2 pass after one bounded transient-429 retry |

This changes the final decision from native credential staging to the unified Gateway
specified by ADR 0008. The remainder of this document is retained as the Phase-0
rationale for the checkpoint and is historical wherever it conflicts with this section,
[spec.md](spec.md), or [plan.md](plan.md).

## Executive Decision

The requested topology is feasible: all three official agents can run non-interactively
inside an ALE Task Sandbox using a subscription login created earlier by the operator.
Live implementation validation refined the transport: Codex/Grok use credential staging
and CONNECT; Claude uses the existing Gateway as a Bearer relay because the pinned native
binary did not honor the authenticated proxy under ALE's routeless Docker network.

```text
provider subscription service
       ^                         ^
       | Codex/Grok opaque TLS   | Claude OAuth Bearer
       |                         |
official pinned CLI        existing Host Gateway
inside Task Sandbox <--- episode token ---+
       ^
       | Codex/Grok auth.json copied by ALE
operator's one-time native login in checkout-local state
```

The implementation is a **GO for local, operator-owned use behind provider-specific
live gates**. API-key mode remains the stable existing path. Shared-account SaaS,
credential pooling, and a generic subscription proxy are not supported.

| Harness | Official saved login | Official headless mode | Minimal in-Sandbox credential | Feasibility |
|---------|----------------------|------------------------|--------------------------------|-------------|
| `claude-code` | `claude setup-token` | `claude -p` / `--print` | episode token to Host Bearer relay | High after live relay validation |
| `codex-cli` | `codex login` / device auth | `codex exec` | file-backed `auth.json` with serialized copyback | High for one profile |
| `grok-build` | `grok login` / device auth | `grok -p` | `auth.json` with serialized copyback | Medium-high pending pinned-version live gate |

This deliberately accepts that Task code can read and exfiltrate staged Codex/Grok
credentials. Claude's long-lived token remains on the Host. Either behavior is part of
the declared Harness contract rather than an accidental escape.

## Cross-Provider Decisions

### Decision: keep the official agent loop inside the Task Sandbox

**Rationale**: This is the required product behavior and matches the current ALE
Autonomous Harness architecture. The three Harnesses already install the exact CLI,
translate Skills/MCP/settings, launch as `Identity.AGENT`, collect native evidence, and
support native continuation. Moving the loop to the host would recreate filesystem and
tool mediation that ALE already has.

**Alternatives considered**:

- Host-side controller plus Sandbox tool bridge: rejected; it adds a second execution
  topology and violates the requested placement.
- A generic provider OAuth/inference relay: rejected. Claude's implemented exception is
  a narrow reuse of the existing Anthropic `/v1/messages` Gateway path and the official
  OAuth Bearer header; Codex/Grok remain native.
- Shared writable native home mount: rejected; it is Docker-specific, exposes unrelated
  state, and does not solve QEMU consistently.

### Decision: reuse native auth state; do not implement OAuth

**Rationale**: Each provider already supplies a supported login and refresh owner. ALE
only transfers the documented token/file into the existing episode home. Codex and Grok
own their opaque JSON schema and refresh it themselves; Claude's setup token requires no
refresh daemon.

**Alternatives considered**:

- Parsing access/refresh tokens and refreshing them in ALE: rejected; unnecessary and
  more fragile than the official CLI.
- Copying the complete provider home: rejected; it imports ambient history, settings,
  hooks, plugins, Skills, MCP servers, and sessions.
- Adding `ale auth login/status/logout`: deferred; native commands already provide the
  required one-time login and the feature needs no separate credential store.

### Decision: use existing host transports

**Rationale**: Subscription model traffic must work when the Task network policy is
`block`. Codex/Grok reuse the provider-independent raw TLS `EgressProxy`, episode token,
Docker/QEMU routing, hostname allowlists, and revocation. Claude uses the existing
Gateway with upstream Bearer auth after live tests showed the pinned Bun/native binary
ignored authenticated HTTP proxy settings. No new transport service is added.

Subscription Runs do not attach finite Gateway limits or Transport Trace. Codex/Grok
tunnels are opaque; Claude's relay still controls the model and coalesces identical
retries in memory. None claim provider billing usage or cost enforcement.

**Alternatives considered**:

- Require `network.mode = "open"`: rejected; it changes Task semantics and defeats the
  default isolation policy.
- Route all subscription providers through the model Gateway: rejected. The narrow
  Claude relay is retained because Anthropic sampling uses the existing `/v1/messages`
  contract; Codex/Grok private subscription endpoints remain native.
- Add a second proxy: rejected; the current proxy already implements the needed contract.

### Decision: serialize mutable file profiles

**Rationale**: Codex and Grok rotate refresh state and write it back. Independent Sandbox
copies can both spend the same refresh token, and copying no state back eventually makes
the host seed stale. A host cross-process lock around copy-in, native execution, and
copyback is the minimum correct lifecycle.

Version 1 accepts one episode at a time for a given Codex/Grok profile. Claude's
immutable setup token can retain ordinary concurrency.

**Alternatives considered**:

- Copy without copyback: rejected by the official refresh lifecycle.
- Lock only copyback: rejected; two Sandboxes may refresh before either writes.
- Merge JSON: rejected; the format is provider-owned and refresh-token families are not
  mergeable.
- Credential broker: deferred until measured throughput justifies it.

## Provider Findings

### Claude Code

#### Decision

Use `claude setup-token` once and make its output available to ALE as
`CLAUDE_CODE_OAUTH_TOKEN`. Keep that token on the Host Gateway; inject only the episode
token and `ANTHROPIC_BASE_URL` into the Sandbox-side
`claude --print --output-format=stream-json` process with a clean episode-local
`CLAUDE_CONFIG_DIR`.

The subscription branch must omit or unset all higher-precedence routes:

```text
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL
CLAUDE_CODE_USE_BEDROCK
CLAUDE_CODE_USE_VERTEX
CLAUDE_CODE_USE_FOUNDRY
```

It must not pass `--bare`, because bare mode ignores OAuth credentials. Keep
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, disable native Claude.ai MCP discovery,
and use the declared ALE model.

#### Rationale

Anthropic documents `setup-token` for automation: the operator authorizes once and
receives an inference-only token with a one-year lifetime. It is not a rotating profile,
so separate Sandbox processes can share it without copyback or a refresh lock. Native
programmatic mode remains supported; the previously announced separate subscription
accounting change was paused rather than removing `claude -p`.

Credential precedence is the main integration trap. The current ALE Harness sets both
`ANTHROPIC_BASE_URL` and higher-priority API/bearer variables for the Gateway. Merely
adding `CLAUDE_CODE_OAUTH_TOKEN` would therefore keep using the API path.

The Host relay needs `api.anthropic.com`; `platform.claude.com` is included for
the accepted native auth lifecycle. Browser hosts are needed during the host-side
one-time setup, not during Task execution.

#### Version gate

ALE currently pins Claude Code `2.1.220`. Release `2.1.225` fixed a transient-401 bug
that could replace a long-lived setup token with short-lived stored state. Pin exactly
`2.1.227` after a live conformance run and keep the existing exact-version verification.

Sources: [authentication and precedence](https://code.claude.com/docs/en/team),
[programmatic mode](https://code.claude.com/docs/en/headless),
[network configuration](https://code.claude.com/docs/en/network-config),
[environment variables](https://code.claude.com/docs/en/env-vars), and
[Claude Code changelog](https://code.claude.com/docs/en/changelog).

#### Prior art and policy

[Harbor PR #1846](https://github.com/harbor-framework/harbor/pull/1846) demonstrates
container-side setup-token use. ALE live validation found that its pinned native binary
did not honor authenticated CONNECT in a routeless Docker network, which is why ALE uses
the relay while retaining the same in-Sandbox agent loop.

Anthropic's published third-party OAuth restrictions make account pooling or offering a
general “Sign in with Claude” product inappropriate. This plan is limited to a local
operator using their own token with the official CLI. Broader hosted distribution needs
separate policy approval.

### Codex CLI

#### Decision

Use a file-backed ChatGPT login created by the official Codex CLI inside ALE's dedicated
checkout-local state. Resolve `<checkout>/.ale/auth/codex-cli/auth.json`, copy only that file to
`<episode-home>/.codex-ale/auth.json`, and run the existing `codex exec --json` inside the
Sandbox.

The generated subscription config must:

- omit `model_provider = "ale"` and `[model_providers.ale]`;
- set `forced_login_method = "chatgpt"`;
- set `cli_auth_credentials_store = "file"`;
- keep the requested model, approval/sandbox, reasoning, Skills, MCP, and transcript
  behavior; and
- omit `ALE_GATEWAY_TOKEN` and all base-URL overrides from the native process.

After any native process termination, read the possibly refreshed guest `auth.json` and
atomically replace the host copy if it is nonempty valid JSON. Hold the profile lock for
the entire cycle.

#### Rationale

OpenAI documents ChatGPT login for Codex CLI, copying `auth.json` to a headless/container
environment, and `codex exec` for non-interactive scripts and scheduled jobs. It also
documents managed CI auth: Codex refreshes the file proactively and after 401, the
updated file must be persisted, and one mutable file belongs to one serialized workflow
stream.

The current ALE Harness already creates `.codex-ale`, runs `codex exec`, parses JSONL,
and uses portable Sandbox file APIs. Only provider selection, credential staging, and
copyback change. The user's ordinary `~/.codex` profile is never inspected, so ALE
refresh rotation cannot disrupt interactive Host use.

The pinned client currently uses `chatgpt.com` for subscription inference and
`auth.openai.com` for auth support. These hosts are client implementation details, not a
public subscription inference API; exact CLI conformance is therefore the compatibility
boundary.

Sources: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[managed CI/CD authentication](https://learn.chatgpt.com/docs/auth/ci-cd-auth), and
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

#### Prior art and account tiers

Harbor copies a Codex `auth.json` into its agent container and runs `codex exec`, proving
the basic Sandbox path. Harbor deletes the guest file without copying refreshed state
back, so it is not sufficient evidence for long-lived or concurrent reuse.

ChatGPT personal subscriptions support native `codex exec`; Business and Enterprise can
also use time-limited managed access tokens on trusted runners. This feature uses the
native CLI profile rather than pretending a ChatGPT credential is a public Responses API
key. The current ALE `0.146.0` pin exposes the required login/status/exec behavior and
stays pinned until its live matrix says otherwise.

### Grok Build

#### Decision

Resolve `<checkout>/.ale/auth/grok-build/auth.json`, copy only it to the existing
`<episode-home>/.grok-ale/auth.json`, and run the existing official `grok -p` path. The
subscription config must omit both `[models] default = "ale"` and `[model."ale"]`, pass
`--model <session.model>`, and unset:

```text
ALE_GATEWAY_TOKEN
XAI_API_KEY
GROK_CLI_CHAT_PROXY_BASE_URL
```

This is required because per-model `api_key`/`env_key` has precedence over the signed-in
session. Keep telemetry and trace-upload controls and explicitly set
`GROK_WORKSPACE_DATA_COLLECTION_DISABLED=true`.

After any termination, copy a valid changed `auth.json` back atomically under the full
profile lock. Normal execution needs `cli-chat-proxy.grok.com`; `auth.x.ai` supports the
native auth lifecycle. `api.x.ai` is the API-key service and is not the subscription
route.

#### Rationale

xAI documents browser/device login, owner-only `$GROK_HOME/auth.json`, automatic refresh,
and non-interactive prompt/streaming/session/resume operation. Official source shows
rotating refresh handling plus native file locking; separate Sandbox copies do not share
that lock, so ALE must serialize them externally.

The isolated login is created once by pointing official `GROK_HOME` at that directory;
the user's ordinary `~/.grok` profile is never read or written. ALE currently pins
`@xai-official/grok@0.2.112`; npm now publishes `1.0.0`, and later 0.2.x releases include
auth recovery fixes. Avoid an unrelated major upgrade in the first implementation: run
the full live subscription/refresh gate against `0.2.112`, then bump deliberately only
if that gate fails.

Sources: [xAI authentication guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md),
[headless guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md),
[configuration guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/05-configuration.md),
and [Grok Build](https://x.ai/cli).

[Open Harness](https://github.com/mifunedev/openharness/blob/main/.oh/docs/harnesses/grok-build.md)
runs Grok in its Sandbox with a persistent native home. That validates native Sandbox
execution, though ALE should transfer only `auth.json` to remain Docker/QEMU neutral.
xAI consumer terms prohibit sharing credentials or making an account available to other
people, so version 1 is strictly operator-owned use.

## Failure and Reporting Findings

Authentication-related failures must not look like poor Task performance:

| Condition | Required classification |
|-----------|-------------------------|
| source token/file absent or unreadable | subscription authentication unavailable; show native setup/login command |
| malformed staged or refreshed file | profile-state failure; preserve host copy |
| expired/revoked/consent required | authentication failure; no API fallback |
| requested model unavailable | entitlement failure; no model substitution |
| provider quota/rate/fair-use/concurrency refusal | provider-limit failure |
| pinned CLI/config/output no longer matches | subscription compatibility failure |
| guest refresh cannot be persisted | profile-persistence failure; never a valid zero score |
| native endpoint absent from pinned allowlist | network/compatibility failure; never open egress automatically |

Subscription traffic is outside metered Gateway policy. Claude still uses the Gateway
for model authority and retry coalescing, but `max_model_calls`, finite Gateway token
ceilings, and provider API cost are not enforced. Native turn/budget settings and episode
deadlines still apply. Unknown or unavailable observations are omitted or labeled
unavailable, not set to zero.

## Acceptance Matrix

Each provider remains disabled until its exact CLI pin passes:

1. one-time native login followed by ten later Tasks without an API key or login prompt;
2. explicit/auto precedence with stale API keys and custom endpoints present;
3. official CLI process observed inside Docker and QEMU Task Sandboxes;
4. agent reads the staged credential successfully and the Run is still valid;
5. framework result, trace, RunLock, exceptions, and logs do not intentionally serialize
   raw credential values;
6. native model, Skills, MCP, tool use, evidence conversion, and two same-Sandbox resumes;
7. blocked-network execution reaches only the pinned provider hosts plus Task hosts;
8. expired, revoked, wrong-model, quota, rate, and endpoint-drift failures;
9. cancellation during refresh and valid atomic Codex/Grok copyback;
10. two ALE processes sharing a mutable profile serialize; API-key regression remains
    unchanged.

## Final Feasibility Assessment

- **Technical feasibility**: high. The official CLIs and current Sandbox adapters already
  supply nearly all behavior; changes are auth selection, small per-Harness config
  branches, file lifecycle, and reuse of the existing proxy registry.
- **Operational feasibility**: high for Claude; medium-high for serialized Codex/Grok.
  Mutable profiles impose one-at-a-time throughput and must not be used concurrently by
  unrelated native CLI processes.
- **Compatibility confidence**: high for documented login/headless behavior; medium for
  pinned endpoint allowlists and provider-owned file schemas, which require live tests on
  every CLI pin change.
- **Policy feasibility**: conditional. Local self-use with the official CLI is the target;
  shared consumer accounts, credential pools, and hosted login brokerage are excluded.

The plan therefore proceeds with sandbox-native subscription authentication and treats
credential confidentiality as an operator-accepted limitation, not a design blocker.
