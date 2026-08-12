# Quickstart: Sandbox-Native Subscription Runs

This is the post-implementation operator and acceptance path. Run ALE commands from
`/home/weichen/ale/ale`. The official agent always executes inside the Task Sandbox.

## 1. Log in once with the provider CLI

### Claude Code

Generate the official long-lived automation token:

```bash
mkdir -p "$PWD/.ale/auth/claude-code"
CLAUDE_CONFIG_DIR="$PWD/.ale/auth/claude-code" claude setup-token
```

Store the printed value as `CLAUDE_CODE_OAUTH_TOKEN` in the ALE checkout's gitignored
`.env`. ALE loads it only into its own process, so it does not change the authentication
used by an ordinary Host Claude process. Do not put the token on an `ale` command line.

### Codex CLI

Create ALE's dedicated profile without touching the ordinary Host Codex profile:

```bash
ALE_AUTH_ROOT="$PWD/.ale/auth"
mkdir -p "$ALE_AUTH_ROOT/codex-cli"
CODEX_HOME="$ALE_AUTH_ROOT/codex-cli" \
  codex login --device-auth -c 'cli_auth_credentials_store="file"'
```

Do not export `CODEX_HOME`; ALE derives the same checkout-local path itself. Your normal
Codex CLI continues to use its ordinary profile. Codex owns and refreshes the isolated
`auth.json`.

### Grok Build

Create the separate Grok profile:

```bash
ALE_AUTH_ROOT="$PWD/.ale/auth"
mkdir -p "$ALE_AUTH_ROOT/grok-build"
GROK_HOME="$ALE_AUTH_ROOT/grok-build" grok login --device-auth
```

Do not export `GROK_HOME`; ordinary Host Grok use stays on its own profile. Do not point
another native process at ALE's isolated mutable profile while an ALE Run is active.

## 2. Run explicitly in subscription mode

```bash
uv run ale run demo-hello --agent claude-code --auth subscription
uv run ale run demo-hello --agent codex-cli --auth subscription
uv run ale run demo-hello --agent grok-build --auth subscription
```

Before provisioning, ALE prints the Harness, provider, requested/effective mode, model,
and non-secret profile-slot ID. Codex/Grok report `native-proxy`; Claude reports
`subscription-relay` and receives an episode Gateway URL/token rather than the provider
OAuth token.

Run another Task later without logging in again:

```bash
uv run ale run demo-netprobe --agent codex-cli --auth subscription
```

Expected: the saved native login is reused and the official `codex exec` process runs in
the new Sandbox.

## 3. Verify `auto` and explicit precedence

With the Harness-native source present:

```bash
uv run ale run demo-hello --agent codex-cli
```

Expected: `requested=auto`, `effective=subscription`.

Explicit API-key mode ignores the native profile and keeps current Gateway behavior:

```bash
uv run ale run demo-hello --agent codex-cli --auth api-key
```

Expected: `transport=gateway` and a normal `trace.transport.jsonl`.

An explicit subscription Run with a stale API endpoint or API key still uses the native
subscription path. If native auth fails, it does not retry and incur API-key billing.

## 4. Confirm the declared Sandbox credential behavior

Run the subscription credential probe integration test:

```bash
uv run pytest tests/integration/test_subscription_auth.py -k agent_can_read_staged_credential
```

The probe is expected to find Codex/Grok `auth.json` in the episode home. Claude should
find only its episode token and Gateway base URL; the long-lived OAuth token remains on
the Host. The declared Codex/Grok access does not invalidate the result.

Framework-authored `result.json`, `lock.json`, execution diagnostics, and exceptions
must not intentionally contain the raw credential. Agent-authored trajectory output or
Task artifacts may contain anything the agent copied.

## 5. Inspect network and provenance

For a blocked-network Codex/Grok Run, the native CLI can reach only its pinned provider
hosts plus Task-declared hosts through ALE's authenticated proxy. Claude reaches the
episode-authenticated Host Gateway relay. Direct traffic that ignores these paths remains
blocked.

The RunLock authentication section resembles:

```json
{
  "requested": "subscription",
  "effective": "subscription",
  "selection_source": "cli",
  "provider": "openai",
  "profile_slot_id": "sha256:...",
  "transport": "native-proxy",
  "credential_exposed_to_agent": true,
  "gateway_observability": "unavailable",
  "validated_cli_version": "0.146.0"
}
```

There is no `trace.transport.jsonl` for the subscription model traffic. Its absence means
unavailable Gateway visibility, not zero calls or zero cost. `trajectory.json` and native
evidence remain available.

## 6. Validate refresh and serialization

For Codex and Grok, run two episodes against the same profile:

```bash
uv run ale run demo-hello --agent codex-cli --auth subscription -n 2 --concurrency 2
```

Expected: one waits for the other because version 1 holds one profile lock across native
execution and copyback. After both terminate, the host `auth.json` remains valid and mode
`0600`. Repeat with a credential near refresh time in the opt-in live suite.

Claude uses an immutable setup token, so it does not take this mutable-profile lock.

## 7. Exercise failures

For each Harness, test missing, malformed, expired/revoked, wrong-model, quota/rate, and
provider-host drift cases. Expected behavior:

- no Task reward and no aggregate-score entry;
- no API-key or model fallback;
- provider-native recovery command shown;
- last valid Codex/Grok host profile preserved after missing/corrupt guest state.

Native logout remains provider-owned:

```bash
CODEX_HOME="$PWD/.ale/auth/codex-cli" codex logout
GROK_HOME="$PWD/.ale/auth/grok-build" grok logout
```

After logout, explicit subscription mode fails before provisioning. For Claude, remove or
replace `CLAUDE_CODE_OAUTH_TOKEN` and run `claude setup-token` again when required.

## 8. Full verification

```bash
just lint
just test
uv run pytest -m needs_llm tests/acceptance/subscription
```

The live suite uses operator-owned accounts and is opt-in. A provider pin is enabled only
after login reuse, precedence, in-Sandbox execution, Skills/MCP, native continuation,
agent credential access, framework metadata checks, refresh/copyback, proxy allowlists,
failure typing, and API-key regression all pass.
