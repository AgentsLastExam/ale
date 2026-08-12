# Contract: Run Authentication and Provenance

## Configuration

```toml
[agent]
name = "codex-cli"
authentication = "auto" # auto | api-key | subscription
```

CLI override:

```text
ale run TASK --agent codex-cli --auth subscription
```

Precedence is CLI, Run file, preset, then the `auto` default. No saved ALE preference is
introduced.

## Selection

| Requested | Native source present | Effective | Result |
|-----------|-----------------------|-----------|--------|
| `api-key` | either | `api-key` | Existing API-key/Gateway preflight. |
| `subscription` | yes | `subscription` | Native profile preflight. |
| `subscription` | no | — | Fail before provisioning with the official setup/login command. |
| `auto` | yes | `subscription` | Subscription wins; API credentials are ignored. |
| `auto` | no | `api-key` | Existing behavior. |

An effective subscription never retries with an API key or another model after an auth,
entitlement, quota, rate, endpoint, or compatibility failure.

## API-key Mode

- Existing `gateway.base_url`, `gateway.api_key_env`, dialect, limits, Gateway transport,
  model enforcement, cost estimation, and Transport Trace remain unchanged.
- Native profiles are neither read nor staged.
- Missing API credentials use the existing error path.

## Subscription Mode

- Do not require or resolve provider API-key environment variables.
- Ignore custom API base URLs and Gateway dialect for the native agent process.
- The exact official CLI runs as the evaluated agent inside the Task Sandbox.
- Stage only the selected native token/file after Task setup and before agent launch.
- The agent and its tools may read, modify, delete, or exfiltrate the staged credential.
- Use the existing episode-authenticated CONNECT proxy for the Harness provider hosts
  plus Task-declared hosts; do not open general egress.
- Gateway limit and accounting fields are unavailable; episode deadlines and native
  Harness limits continue to apply.
- No failure silently selects API-key mode.

## Preflight Order

Before Task Sandbox provisioning:

1. resolve and display Harness, provider, requested/effective auth mode, and model;
2. validate exact CLI version/integrity policy;
3. resolve the provider-native token/file source;
4. validate file type, ownership, permissions, and basic accepted representation;
5. acquire the mutable profile lock when applicable;
6. derive the non-secret profile-slot identity and provider host allowlist;
7. only then provision the episode.

Provider entitlement may require the first native call; such a rejection remains a
typed non-result failure.

## Provenance

RunLock adds:

```json
{
  "authentication": {
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
}
```

Old RunLocks without `authentication` load as the existing API-key/Gateway path. Raw
credential values, paths, account IDs, and provider status payloads never enter this
object.

Claude subscription Runs use `transport = subscription-relay` and
`credential_exposed_to_agent = false`; Codex/Grok use the values shown above.

Subscription Runs have no `trace.transport.jsonl`. Its absence means no retained
Gateway trace or provider billing observation, not zero calls or zero cost. Claude's
Gateway relay still controls the model and coalesces identical retries in memory. Native
evidence and `trajectory.json` record the agent interaction ALE can observe.

## Continuation

A native continuation must retain the original:

- effective authentication mode and profile-slot identity;
- Harness and exact CLI version;
- requested/effective model;
- Task, resources digest, episode, live Sandbox, and native session ID.

A mismatch fails explicitly. Continuation never falls back to a new session, another
profile, another model, or API-key mode.

## Terminal Failures

Authentication, entitlement, provider limit, compatibility, proxy allowlist, and profile
persistence errors are non-result failures. They produce no rewards and never enter score
aggregation as valid zeroes.
