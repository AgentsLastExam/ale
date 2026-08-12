# 0008 - Subscription and API model traffic share the ALE Gateway

**Status:** Accepted

## Context

ADR 0007 copied Codex and Grok `auth.json` into each Task Sandbox and let the native
CLI contact the provider through a CONNECT proxy. Because both providers may rotate a
refresh token, ALE held a profile lock for the complete episode and copied changed auth
state back. That design worked, but serialized same-profile Tasks, exposed provider
configuration as a Harness branch, and made subscription Runs less observable.

Live research established a smaller common contract. All three official CLIs can send
their model protocol to an ALE-generated local provider configuration. Their
subscription inference services accept the same model request body after the Host adds
the provider-owned bearer and required account/client headers.

## Decision

Claude Code, Codex CLI, and Grok Build always send model traffic from the Task Sandbox
to the Host Gateway. The Sandbox receives only its episode token and reachable Gateway
URL. API-key versus subscription changes only how the Host Gateway authenticates and
where it forwards the request; generated Harness configuration, requested model,
session handling, limits, and Transport Trace use the same path.

Subscription credentials remain checkout-local:

- Claude: `CLAUDE_CODE_OAUTH_TOKEN` in the checkout `.env`;
- Codex: `.ale/auth/codex-cli/auth.json`;
- Grok: `.ale/auth/grok-build/auth.json`.

ALE never discovers or modifies ordinary `~/.claude`, `~/.codex`, or `~/.grok`
profiles. Codex and Grok access tokens are read and refreshed on the Host. Normal model
requests do not acquire a profile lock. A cross-process `flock` covers only the rare
refresh request and atomic `0600` replacement, so concurrent Tasks share the current
access token without racing refresh-token rotation.

The Gateway forwards Codex subscription traffic to
`https://chatgpt.com/backend-api/codex/responses`, Grok traffic to the selected
cli-chat-proxy dialect, and Claude traffic to Anthropic Messages. It retries one
subscription 401 after refresh and one transient 429. It never falls back to an API key
or another model.

Codex's ChatGPT backend rejects the public Responses `max_output_tokens` request field.
ALE therefore omits that field for this one upstream. Completed usage is still recorded
and the next request is refused after a configured ceiling is reached, but ALE cannot
hard-cap the current Codex subscription response with that field.

## Consequences

Same-profile episodes can run concurrently; provider-owned account limits still bound
actual throughput. All three subscription Harnesses regain Gateway model authority,
duplicate coalescing, token accounting, configured ceilings, and Transport Trace.
Provider billing remains external; `cost_usd` is an ALE estimate only when pricing is
known.

The isolated login files remain provider-owned, but their refresh endpoints and JSON
fields are compatibility inputs. ALE pins CLI versions, validates profile shape and
permissions, fails closed on drift, and keeps OS-keychain support deferred. Shared
consumer-account pools and credential resale remain out of scope.
