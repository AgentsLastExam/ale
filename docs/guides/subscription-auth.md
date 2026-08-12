# Subscription authentication

Claude Code, Codex CLI and Grok Build can use an existing consumer subscription for
non-interactive Task runs. Login state belongs to this ALE checkout, not to the user's
ordinary `~/.claude`, `~/.codex` or `~/.grok` profile.

## Login once

Run these commands from the ALE repository root. `.ale/` and `.env` are gitignored.

### Claude Code

Create a dedicated setup token without touching the ordinary Claude profile:

```bash
CLAUDE_CONFIG_DIR="$PWD/.ale/auth/claude-code" claude setup-token
```

Put the returned value in the checkout's `.env`:

```dotenv
CLAUDE_CODE_OAUTH_TOKEN=...
```

### Codex CLI

```bash
CODEX_HOME="$PWD/.ale/auth/codex-cli" codex login
chmod 0600 .ale/auth/codex-cli/auth.json
```

### Grok Build

```bash
GROK_HOME="$PWD/.ale/auth/grok-build" grok login
chmod 0600 .ale/auth/grok-build/auth.json
```

ALE validates that JSON profiles are regular files owned by the current user with mode
`0600`. It reads and refreshes only these checkout-local profiles. Ordinary Host agent
profiles are never discovered, changed or logged out.

## Run Tasks

Select subscription mode explicitly:

```bash
uv run ale run TASK --agent claude-code --auth subscription
uv run ale run TASK --agent codex-cli --auth subscription --model gpt-5.6-luna
uv run ale run TASK --agent grok-build --auth subscription --model grok-4.5
```

Harness presets default to `auto`: use the checkout-local subscription when present,
otherwise use the configured API-key path. `--auth api-key` always selects API-key
billing. Once subscription mode is selected, authentication or entitlement failure is
reported; ALE never silently falls back to an API key or another model.

Episodes may run concurrently:

```bash
uv run ale run TASK --agent codex-cli --auth subscription -n 4 --concurrency 4
```

Normal calls share the current access token without a profile lock. Codex/Grok token
refresh takes a short cross-process lock and atomically replaces the checkout-local
`auth.json`, preventing concurrent refresh-token rotation from corrupting the profile.

## What ALE does

The official CLI still runs inside each Task Sandbox. API-key and subscription runs use
the same Sandbox-side path:

```text
official CLI → episode Gateway URL + episode token → Host ALE Gateway → provider
```

The Sandbox never receives the provider access token, refresh token or `auth.json`.
Only the Host Gateway's upstream URL and authorization headers change. Model authority,
request coalescing, configured limits, usage accounting and Transport Trace stay common.

## Limits

- Provider subscription quota, billing and account concurrency remain provider-owned.
- ALE retries one refreshable subscription `401` and one transient `429`; sustained
  provider limits fail explicitly.
- Codex's subscription endpoint rejects `max_output_tokens`. ALE records completed usage
  and refuses later calls after a ceiling, but cannot hard-cap that current response with
  the public Responses field.
- Codex/Grok private subscription endpoints, headers, JSON fields and refresh behavior
  are compatibility surfaces pinned to the shipped CLI versions.
- File-backed Linux profiles are implemented. macOS Keychain and Windows Credential
  Manager profiles are not yet supported.

Repeat login only when the dedicated credential is revoked, malformed or no longer
refreshable.
