# Contract: Native Subscription Credential Lifecycle

## One-Time Login

ALE reuses the providers' own commands; it does not add an auth command group:

```bash
# Claude: keep setup state and the printed token in this checkout
CLAUDE_CONFIG_DIR="$PWD/.ale/auth/claude-code" claude setup-token

# Codex/Grok: use the official login in this checkout's ignored state directory
CODEX_HOME="$PWD/.ale/auth/codex-cli" codex login --device-auth
GROK_HOME="$PWD/.ale/auth/grok-build" grok login --device-auth
```

Host source resolution:

| Harness | Source | Missing-source recovery |
|---------|--------|-------------------------|
| `claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` |
| `codex-cli` | `<checkout>/.ale/auth/codex-cli/auth.json` | official Codex login with isolated `CODEX_HOME` |
| `grok-build` | `<checkout>/.ale/auth/grok-build/auth.json` | official Grok login with isolated `GROK_HOME` |

`<checkout>` is the ALE repository containing the run's `.env`; `.ale/` is excluded from
Git and Docker build contexts. The CLI used for login must be compatible with the exact
Harness pin. ALE never falls back to `~/.codex` or `~/.grok`.
Operators must not run another process against the isolated mutable profile while ALE
owns it; ordinary Host agent profiles are independent and remain usable.

## Host Validation

Before reading a file profile, ALE must:

- resolve the canonical path without accepting a symlink;
- require an operator-owned regular file;
- reject group/other-writable permissions;
- require nonempty valid JSON without depending on provider field names; and
- acquire the Harness/profile `flock` before copying any bytes.

Error output may contain the environment-variable name or configured path, but never the
token/file content.

## Sandbox Staging

Staging occurs after Task setup and before native agent launch as `Identity.AGENT`:

| Harness | Target |
|---------|--------|
| Claude | episode `ANTHROPIC_AUTH_TOKEN` + Host Gateway `ANTHROPIC_BASE_URL`; clean episode `CLAUDE_CONFIG_DIR` |
| Codex | `<home>/.codex-ale/auth.json`, mode `0600` |
| Grok | `<home>/.grok-ale/auth.json`, mode `0600` |

No ambient provider config, history, session, Skill, MCP, plugin, hook, or other provider
credential is copied. Staged Codex/Grok credentials are intentionally readable by
evaluated code; Claude's provider token stays on the Host.

## Native Launch Precedence

Claude subscription launch must use only the episode bearer token and Host Gateway base
URL, unset API-key/cloud-provider selectors, and must not use `--bare`. The Gateway
forwards the checkout-local OAuth token as upstream Bearer auth without retaining a
Transport Trace.

Codex subscription launch must use its built-in ChatGPT provider with file credential
storage and must omit the ALE custom provider, `ALE_GATEWAY_TOKEN`, and base-URL overrides.

Grok subscription launch must use its native requested model, omit custom `[models]`
configuration, and unset `ALE_GATEWAY_TOKEN`, `XAI_API_KEY`, and
`GROK_CLI_CHAT_PROXY_BASE_URL`.

## Copyback

Claude setup tokens are immutable: copyback is not applicable.

For Codex and Grok, Harness cleanup runs copyback after every native process termination,
including Task failure and cancellation:

1. read the guest `auth.json` while the Sandbox is live;
2. require nonempty valid JSON;
3. if bytes are unchanged, record `unchanged`;
4. otherwise write a sibling host temporary file with mode `0600`;
5. flush and `fsync` the file;
6. atomically `os.replace` the authoritative profile;
7. `fsync` the parent directory where supported;
8. only then release the profile lock.

A missing or malformed guest file never deletes or overwrites the last host copy. A
copyback failure is explicit and cannot become a valid zero score.

## Cleanup and Retention

After copyback and evidence capture, remove the staged token/file and temporary native
home on a best-effort basis. Cleanup failure is warned and recorded. Because the feature
contract already permits agent access to the credential, a retained Sandbox is not
destroyed solely to claim credential confidentiality.

The per-episode proxy token remains a framework capability and is always revoked through
the existing session lifecycle.

## Required Tests

- auto/explicit source selection and competing API variables;
- symlink, ownership, permissions, empty, and malformed host sources;
- exact guest paths, file modes, and agent readability;
- provider-specific environment/config precedence;
- unchanged, updated, missing, corrupt, cancellation, and failed copyback;
- two processes contending for one mutable profile;
- cleanup on ordinary and retained Sandboxes;
- no raw credential in framework-authored provenance/errors;
- API-key configuration and behavior regression.
