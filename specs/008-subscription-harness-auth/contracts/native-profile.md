# Contract: Checkout-Local Subscription Credential

## Sources

| Harness | Source |
|---|---|
| Claude Code | `CLAUDE_CODE_OAUTH_TOKEN` loaded from the checkout `.env` |
| Codex CLI | `<checkout>/.ale/auth/codex-cli/auth.json` |
| Grok Build | `<checkout>/.ale/auth/grok-build/auth.json` |

ALE MUST NOT discover, copy, refresh, or log out ordinary default Host profiles.
`.ale/` and `.env` MUST remain ignored by Git and Docker build contexts.

## File preflight

Codex/Grok auth MUST be a non-symlink regular file owned by the current user with mode
`0600`, valid nonempty JSON, and the provider fields required for access and refresh.
Failure occurs before Task Sandbox provisioning and includes the isolated login recovery
command.

## Host use

The Gateway reads the current short-lived access token and adds provider-required
headers. It MUST NOT place provider auth, refresh tokens, or `auth.json` inside the Task
Sandbox. The Sandbox receives only its episode Gateway token.

## Refresh

Normal requests are lock-free. When expiry or a 401 requires refresh:

1. acquire `<auth.json>.ale.lock` with an exclusive cross-process `flock`;
2. reload and validate the authoritative file;
3. if another process installed a fresh access token, use it without another refresh;
4. otherwise exchange the provider refresh token;
5. update only provider auth fields in a sibling temporary file;
6. set mode `0600`, flush, `fsync`, and atomically replace `auth.json`;
7. release the lock.

The lock MUST NOT cover Sandbox provisioning, agent execution, verification, teardown,
or ordinary model requests.

## Metadata

Framework records MAY contain a deterministic SHA-256 profile-slot identifier. They
MUST NOT contain the path, access token, refresh token, account ID, email, or raw auth
JSON. Agent-authored output is not a framework secret store.

## Failure behavior

No refresh/auth/entitlement/quota/compatibility failure may fall back to an API key,
another profile, or another model. One 401 refresh retry and one bounded transient 429
retry are permitted before the provider error is surfaced.
