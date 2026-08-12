# Implementation Plan: Unified Subscription Gateway

**Branch**: `008-subscription-harness-auth` | **Revised**: 2026-08-11 | **Spec**:
[spec.md](spec.md)

## Summary

Keep all three official agent programs in the Task Sandbox, but make their model path
identical in API-key and subscription modes:

```text
official CLI in Task Sandbox
        │ real model + episode token
        ▼
ALE Gateway on Host
        │ API key OR checkout-local subscription bearer
        ▼
provider model service
```

The operator configures `agent.name`, `agent.authentication`, and `agent.model`. Codex
and Grok provider sections are generated implementation details. Subscription selection
changes only the Host Gateway's upstream credential and endpoint.

## Technical Context

- Python 3.12, asyncio, aiohttp, Pydantic 2, Typer
- Existing `Gateway`, `GatewaySession`, Harness, Sandbox, RunLock, and trace contracts
- No new dependency, daemon, database, controller, or Host CLI installation
- Checkout-local Linux file profiles under `.ale/auth`; Claude token in `.env`
- Unit, integration, conformance, and opt-in live Docker validation

## Constitution Check

| Principle | Result |
|---|---|
| Explicit contracts | PASS: auth selection, Host credential lifecycle, Gateway routing, and provenance are specified and tested. |
| Ownership | PASS: official CLIs own login creation; ALE owns checkout-local refresh/routing; Harnesses own generated native config. |
| Reproducibility | PASS: mode, provider, profile-slot digest, CLI/model, limits, and transport evidence are recorded. |
| Trust boundaries | PASS: the CLI stays Sandbox-side; provider credentials stay Host-side; failures remain non-results. |
| Minimal surface | PASS: reuse the existing Gateway and delete native subscription branches, CONNECT-only sessions, guest staging, copyback, and episode-wide locks. |
| Verification/docs | PASS: ADR 0008, normative specs, deterministic tests, and live sequential/parallel runs cover the reversal. |

## Source Layout

```text
packages/ale-run/src/ale/run/
├── subscription.py          # selection, Host headers, refresh-only lock
├── cli/main.py              # one Gateway orchestration path
├── gateway/server.py        # dynamic Host auth, 401/429 retry, Codex path quirk
└── harnesses/
    ├── claude_code.py       # episode Gateway token in both modes
    ├── codex_cli.py         # one generated ALE provider
    └── grok_build.py        # requested-model table pointing to Gateway
```

## Design

### 1. Resolve auth once

`auto` chooses the checkout-local subscription source when present, otherwise API key.
Explicit mode wins. Selection is printed before launch and never changes after a
provider failure.

### 2. Generate one Sandbox configuration

Claude always gets its Gateway base URL and episode token. Codex always gets the
internal `model_providers.ale` section with the requested model. Grok always gets a
generated `[model."<requested model>"]` entry; its key and native `--model` value are
the real model name. None of these fields branch on auth mode.

### 3. Authenticate only on the Host

`SubscriptionCredential` supplies the Gateway's upstream URL, path, and headers:

| Harness | Upstream | Host auth |
|---|---|---|
| Claude | `api.anthropic.com/v1/messages` | OAuth Bearer from checkout `.env` |
| Codex | `chatgpt.com/backend-api/codex/responses` | access token + ChatGPT account ID |
| Grok | `cli-chat-proxy.grok.com/<dialect path>` | access token + official CLI identity headers |

Codex and Grok profiles are validated for regular-file ownership and mode `0600`.
Access-token expiry is checked before provisioning and on requests. Refresh uses the
provider's current file schema and refresh service, then atomically replaces the same
checkout-local file.

### 4. Lock only refresh

Each process reads the current access token without locking. When refresh is required,
it acquires `<auth.json>.ale.lock`, reloads the profile, and skips network refresh if
another process already installed a fresh token. Otherwise it refreshes while holding
the lock, writes mode `0600`, and releases. The Task episode never holds this lock.

### 5. Reuse Gateway evidence and limits

Every episode opens a normal `GatewaySession` and Transport Trace. The Gateway imposes
the Run model, coalesces retries, accounts response usage, and refuses later calls after
limits are reached. Subscription 401 and transient 429 each receive one bounded internal
retry. Provider entitlement, sustained limit, and auth failures remain visible.

Codex ChatGPT does not accept the public `max_output_tokens` field, so that field is
removed only for that upstream. This is the sole current limit-semantic difference.

## Rejected Alternatives

- Copy `auth.json` into every Sandbox: requires copyback and episode-wide locking and
  makes provider state part of the evaluated filesystem.
- Keep provider-native egress through CONNECT: duplicates routing and removes Gateway
  evidence/limits without adding value once Host header injection is proven.
- Install provider CLIs on the Host only to refresh: Grok is not otherwise installed and
  this creates another managed runtime. The minimum refresh exchange is smaller.
- Build a generic OAuth broker/plugin framework: only two current file formats need
  refresh; add an abstraction after another concrete provider proves it stable.

## Validation and Release Gates

1. Unit tests cover profile isolation, headers, refresh-only atomic update, 401 refresh,
   429 retry, Codex path/field behavior, and identical Harness config.
2. Integration proves no `auth.json` is staged into a real Task Sandbox.
3. Live Docker Tasks pass for Claude, Codex `gpt-5.6-luna`, and Grok `grok-4.5`.
4. Codex and Grok each pass two episodes at `--concurrency 2` using one profile.
5. `just lint` and `just test` pass before the final commit.

## Known Compatibility Boundaries

- Codex and Grok subscription endpoints, headers, refresh endpoints, and JSON fields are
  provider implementation contracts, not general public inference APIs.
- A rotating refresh token accepted immediately before a Host crash may still require a
  new login; atomic local replacement cannot make a remote/local two-phase transaction.
- OS-keychain-backed profiles are deferred.
- Provider account quotas may cap useful concurrency even though ALE itself no longer
  serializes episodes.
