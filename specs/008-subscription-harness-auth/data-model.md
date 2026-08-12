# Data Model: Subscription-Authenticated Harness Runs

## AgentConfig

| Field | Type | Rule |
|---|---|---|
| `authentication` | `auto | api-key | subscription` | Defaults to `auto` for supported Harness presets. |
| `model` | string | Always the real requested model; CLI override applies in both modes. |

## ResolvedAuthentication

Immutable result of selection before provisioning.

| Field | Type | Meaning |
|---|---|---|
| `harness` | string | Selected Harness. |
| `requested` | enum | Operator request. |
| `mode` | `api-key | subscription` | Effective mode; never `auto`. |
| `provider` | `anthropic | openai | xai | null` | Provider identity. |
| `profile` | Host `Path | null` | Checkout-local file; never persisted to Run metadata. |
| `profile_slot_id` | `sha256:<digest> | null` | Non-secret continuation/provenance identity. |
| `token` | string | Claude checkout token only; excluded from metadata. |

## SubscriptionCredential

Host-only mutable runtime state shared by the Gateway.

| Field | Meaning |
|---|---|
| `upstream` | Provider subscription service base URL. |
| `upstream_path` | Provider request path; Codex uses `/responses`. |
| `dialect` | Existing Gateway wire dialect. |
| `supports_output_limit` | False only for ChatGPT Codex. |
| access token/headers/expiry | In-memory current snapshot; never serialized by ALE. |

State transition:

```text
profile absent/invalid ──> preflight failure
valid + fresh ──────────> ready ──> lock-free model requests
valid + expiring ───────> refresh lock ──> reload
                                      ├── changed/fresh ──> ready
                                      └── still stale ────> refresh + atomic write ──> ready
provider 401 ───────────> one refresh attempt ──> one request retry
provider 429 ───────────> one bounded delay ────> one request retry
```

## GatewaySession

Every API-key and subscription episode uses the existing session:

- opaque episode token;
- authoritative real model;
- configured limits and completed usage;
- allowed Task egress hosts;
- duplicate/replay cache;
- optional payload/exact-token debug directories;
- attached Transport Trace sink.

## HarnessSession

The Harness receives only:

- episode ID and token;
- Sandbox-reachable Gateway URL;
- real model;
- effective auth mode and non-secret profile slot;
- Sandbox/home/resource identity.

It contains no subscription access/refresh credential field.

## AuthenticationProvenance

All subscription Harnesses record:

```json
{
  "requested": "subscription",
  "effective": "subscription",
  "selection_source": "cli",
  "provider": "xai",
  "profile_slot_id": "sha256:<digest>",
  "transport": "gateway",
  "credential_exposed_to_agent": false,
  "gateway_observability": "available",
  "validated_cli_version": "0.2.112"
}
```

No raw credential or path field exists.

## Relationships

```text
AgentConfig 1 ──> 1 ResolvedAuthentication
ResolvedAuthentication 1 ──> 0..1 SubscriptionCredential
SubscriptionCredential 1 ──> 1 Gateway
Gateway 1 ──> many GatewaySession
GatewaySession 1 ──> 1 HarnessSession episode token
HarnessSession 1 ──> 1 official CLI in Task Sandbox
```

## Error mapping

| Condition | Type |
|---|---|
| missing isolated source | `SubscriptionUnavailableError` |
| invalid permissions/JSON/fields | `SubscriptionProfileError` |
| refresh or upstream authentication rejection | `SubscriptionAuthenticationError` |
| unsupported CLI/config/endpoint contract | `SubscriptionCompatibilityError` |
| requested model/account entitlement rejection | `SubscriptionEntitlementError` |
| sustained rate/quota/concurrency rejection | `SubscriptionProviderLimitError` |
