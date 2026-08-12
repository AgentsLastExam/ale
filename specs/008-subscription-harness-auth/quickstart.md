# Quickstart: Subscription-Authenticated Harnesses

All state stays inside the ALE checkout and is ignored by Git. These commands do not
reuse or modify the ordinary Host profiles under `~`.

## 1. Log in once

Claude uses a setup token in the checkout `.env`:

```bash
CLAUDE_CONFIG_DIR="$PWD/.ale/auth/claude-code" claude setup-token
# Store the returned value as CLAUDE_CODE_OAUTH_TOKEN in .env.
```

Codex and Grok use isolated file profiles:

```bash
mkdir -p .ale/auth/codex-cli .ale/auth/grok-build
CODEX_HOME="$PWD/.ale/auth/codex-cli" codex login
GROK_HOME="$PWD/.ale/auth/grok-build" grok login
chmod 0600 .ale/auth/codex-cli/auth.json .ale/auth/grok-build/auth.json
```

ALE may refresh these two checkout-local files. Do not copy an ordinary Host profile
into this directory while a Run is active.

## 2. Run

```bash
uv run ale run TASK --agent claude-code --auth subscription
uv run ale run TASK --agent codex-cli --auth subscription
uv run ale run TASK --agent grok-build --auth subscription
```

`auto` is the preset default: it selects subscription when that Harness's checkout-local
source exists, otherwise API key. Force API billing with `--auth api-key`.

The model is always the real model name and can be overridden normally:

```bash
uv run ale run TASK --agent codex-cli --auth subscription --model gpt-5.6-luna
uv run ale run TASK --agent grok-build --auth subscription --model grok-4.5
```

ALE internally generates the CLI provider configuration. There is no user-facing
`model.ale` setting and no subscription-specific Sandbox configuration.

## 3. Run concurrently

```bash
uv run ale run TASK --agent codex-cli --auth subscription -n 2 --concurrency 2
uv run ale run TASK --agent grok-build --auth subscription -n 2 --concurrency 2
```

Normal requests share the current access token without a profile lock. Only a token
refresh is serialized. Provider quota, RPS, and concurrency policies may still cap
throughput; ALE retries one transient subscription 429 and then reports a sustained
limit explicitly.

## 4. Inspect evidence

The startup line shows requested/effective auth, Harness, provider, model, and a
non-secret profile-slot digest. `lock.json` records the same selection. Subscription
Runs now retain `trace.transport.jsonl` just like API-key Runs because all model calls
use the Gateway.

The trace contains request/response digests, provider response ID, usage, status,
latency, and estimated cost where pricing is known. It contains no raw provider token or
refresh token. Provider billing and subscription quota records remain provider-owned.

## 5. Recover

If explicit subscription mode reports missing/revoked auth, repeat only the relevant
checkout-local login command from section 1. `auto` falls back to API key only when the
source is absent during initial selection; a failure after subscription selection never
changes billing mode or model.

## Current limitations

- Linux file-backed Codex/Grok profiles only; macOS Keychain and Windows Credential
  Manager are TODO T025.
- Provider auth JSON and private subscription endpoint compatibility is pinned and may
  require an ALE/CLI update after provider changes.
- ChatGPT Codex rejects `max_output_tokens`; ALE records completed usage and refuses
  later calls after a ceiling, but cannot hard-cap the current subscription response
  with that field.
