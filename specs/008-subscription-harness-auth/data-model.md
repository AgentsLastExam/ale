# Data Model: Subscription-Authenticated Harness Runs

The feature adds no database. Configuration uses existing Pydantic models, transient
credential state stays in `ale-run`, and only non-secret observations enter RunLock.

## AuthenticationSelection

The requested Run configuration.

| Field | Type | Rules |
|-------|------|-------|
| `requested` | `auto | api-key | subscription` | Defaults to `auto` for the three supported Autonomous Harnesses. |
| `source` | `cli | run | preset | default` | Existing configuration precedence; CLI wins. |

Resolution:

```text
explicit api-key       -> api-key
explicit subscription  -> subscription if source exists, otherwise fail
auto + native source   -> subscription
auto + no native source -> api-key
```

There is no saved preference entity and no retry from one effective mode to another.

## EffectiveAuthentication

Frozen episode input after selection.

| Field | Type | Rules |
|-------|------|-------|
| `mode` | `api-key | subscription` | Never `auto` after resolution. |
| `harness` | string | One of `claude-code`, `codex-cli`, `grok-build` for subscription. |
| `provider` | `anthropic | openai | xai` | Model-vendor identity, not ALE Sandbox Provider. |
| `profile_slot_id` | string or null | Stable SHA-256 digest of Harness plus canonical source slot; no credential bytes or account identifier. |
| `mutable` | boolean | False for Claude setup token; true for Codex/Grok file profiles. |
| `transport` | `gateway | native-proxy | subscription-relay` | Determines limits and trace applicability. |
| `provider_hosts` | frozen set of hostnames | Exact pinned CLI compatibility input. |

Validation:

- `api-key` requires `transport = gateway`, no profile, and no provider hosts here;
- `subscription` requires a profile slot and a supported Harness; Claude uses
  `subscription-relay`, while Codex/Grok use `native-proxy`;
- raw credential material is excluded from this model and all serialization.

## NativeCredentialSource

Transient host-only input owned by `ale-run`; never persisted in Run artifacts.

| Field | Type | Provider behavior |
|-------|------|-------------------|
| `kind` | `environment-token | json-file` | Claude uses token; Codex/Grok use file. |
| `source` | environment variable name or canonical `Path` | Resolved before the child process overrides native home variables. |
| `profile_slot_id` | string | Digest of Harness and canonical source slot. |
| `lock_path` | `Path` or null | Present only for mutable file profiles. |
| `login_recovery` | argument vector/display string | Official provider command; no shell interpolation. |

Rules:

- reject symlinks, non-regular files, unsafe ownership, and group/other-writable files;
- file content is treated as opaque except for nonempty valid-JSON copyback validation;
- errors identify the path/variable and recovery command, never raw content;
- an environment token is read once for the episode and registered with existing
  redaction, but Task/agent-owned output is not promised secret-free.

## ProfileLease

Transient host cross-process lease for Codex/Grok.

| Field | Type | Rules |
|-------|------|-------|
| `lock_fd` | host file descriptor | Owner-only lock file. |
| `profile_slot_id` | string | Must match the effective authentication. |
| `acquired_at` | monotonic timestamp | Diagnostic only. |

State:

```text
waiting -> locked -> staged -> cli-finished -> copied-back -> released
             |          |            |                |
             +----------+------------+--------------> released-with-error
```

The exclusive `flock` spans copy-in through copyback. Lock acquisition must not block the
async event loop. Release runs under cancellation shielding. Claude has no ProfileLease.

## StagedNativeCredential

Episode-local state inside the Task Sandbox.

| Field | Type | Rules |
|-------|------|-------|
| `guest_path` | POSIX path or null | Codex/Grok `auth.json`; null for Claude token. |
| `environment_name` | string or null | Null for Claude because only an episode token enters the Sandbox. |
| `staged` | boolean | True only after the Sandbox accepted the bytes/environment contract. |
| `copyback` | `not-applicable | pending | persisted | unchanged | failed` | Framework lifecycle observation. |

The evaluated agent may read, modify, delete, or copy this state. The Task setup phase
runs before Harness staging and does not receive it. Cleanup removes the episode copy on
a best-effort basis after copyback/evidence capture.

## AuthenticationProvenance

Additive frozen RunLock data.

```json
{
  "requested": "auto",
  "effective": "subscription",
  "selection_source": "default",
  "provider": "openai",
  "profile_slot_id": "sha256:...",
  "transport": "native-proxy",
  "credential_exposed_to_agent": true,
  "gateway_observability": "unavailable",
  "validated_cli_version": "0.146.0"
}
```

Rules:

- API-key Runs use `effective = api-key`, `transport = gateway`, and preserve existing
  Gateway provenance;
- old RunLocks without this additive object load as API-key/Gateway behavior;
- no path, token, file bytes, email, account ID, workspace ID, or native raw status is
  serialized;
- requested/effective model remain in existing agent provenance rather than duplicated.

## Provider Mapping

| Harness | Source | Guest representation | Mutable | Required hosts | Recovery |
|---------|--------|----------------------|---------|----------------|----------|
| `claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` | episode token + Host Gateway relay; clean `CLAUDE_CONFIG_DIR` | no | Host Gateway | `claude setup-token` |
| `codex-cli` | `<checkout>/.ale/auth/codex-cli/auth.json` | `<home>/.codex-ale/auth.json` | yes | `chatgpt.com`, `auth.openai.com` | official Codex login with isolated `CODEX_HOME` |
| `grok-build` | `<checkout>/.ale/auth/grok-build/auth.json` | `<home>/.grok-ale/auth.json` | yes | `cli-chat-proxy.grok.com`, `auth.x.ai` | official Grok login with isolated `GROK_HOME` |

## Failure Model

Authentication failures extend the existing typed non-result path; they do not add a
score field.

| Failure | Trigger | Host profile effect |
|---------|---------|---------------------|
| `subscription_unavailable` | source absent/inaccessible | none |
| `subscription_incompatible` | CLI/config/output/endpoint contract changed | none |
| `authentication_failed` | provider rejects or requires login | preserve prior source |
| `entitlement_mismatch` | requested model/account mismatch | preserve prior source |
| `provider_limit` | quota/rate/concurrency refusal | persist valid refresh if present |
| `profile_state_invalid` | guest file absent/malformed after CLI activity | preserve prior source |
| `profile_persistence_failed` | validated guest state cannot replace host file | prior file remains when possible; non-result failure |

## Relationships

```text
AuthenticationSelection 1 --- 1 EffectiveAuthentication
EffectiveAuthentication 1 --- 0..1 NativeCredentialSource
NativeCredentialSource  1 --- 0..1 ProfileLease
NativeCredentialSource  1 --- 1 StagedNativeCredential per episode
EffectiveAuthentication 1 --- 1 AuthenticationProvenance
```

There is intentionally no Account, token schema, refresh-token, credential pool, new
provider-relay service, or distributed lock entity in version 1; Claude reuses the
existing Gateway process in relay mode.
