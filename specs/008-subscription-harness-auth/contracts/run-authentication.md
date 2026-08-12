# Contract: Run Authentication Selection

## Configuration

```toml
[agent]
name = "codex-cli"
authentication = "auto" # auto | api-key | subscription
model = "gpt-5.6-luna"
```

CLI overrides:

```bash
ale run TASK --agent codex-cli --auth subscription --model gpt-5.6-luna
```

## Selection

| Requested | Checkout source | Effective |
|---|---:|---|
| `api-key` | either | `api-key` |
| `subscription` | present | `subscription` |
| `subscription` | absent | fail before provisioning |
| `auto` | present | `subscription` |
| `auto` | absent | `api-key` |

Selection occurs once. Effective subscription never falls back to API key or changes
the model after provider failure.

## Unified Harness behavior

For a given Harness and model, API-key and subscription modes generate the same
Sandbox-side provider configuration and launch command. The official CLI sends the real
model name and episode token to the ALE Gateway. Only the Host Gateway's upstream URL
and authentication headers differ.

## Startup output

Before agent launch ALE prints Harness, provider, requested/effective mode, selection
source, real model, and non-secret profile-slot digest.

## Provenance

```json
{
  "requested": "subscription",
  "effective": "subscription",
  "selection_source": "cli",
  "provider": "openai",
  "profile_slot_id": "sha256:<digest>",
  "transport": "gateway",
  "credential_exposed_to_agent": false,
  "gateway_observability": "available",
  "validated_cli_version": "0.146.0"
}
```

RunLock contains no raw credential or profile path. Native continuation fingerprints
bind effective auth and profile slot in addition to Harness, CLI version, model,
settings, resources, episode, Sandbox, and native session ID.
