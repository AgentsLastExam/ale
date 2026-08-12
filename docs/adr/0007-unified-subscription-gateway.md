# 0007 - Subscription and API model traffic share the ALE Gateway

**Status:** Accepted

## Context

Claude Code, Codex CLI and Grok Build can run non-interactively against a consumer
subscription after one native login. A separate provider-native route would duplicate
Sandbox configuration, model routing, egress policy and authentication lifecycle while
losing Gateway limits and Transport Trace.

All three official CLIs can instead send their normal model protocol to an ALE-generated
local provider configuration. Their subscription inference services accept the same
request body after the Host adds the provider-owned bearer and required account/client
headers.

## Decision

Claude Code, Codex CLI and Grok Build always send model traffic from the Task Sandbox to
the Host Gateway. The Sandbox receives only its episode token and reachable Gateway URL.
API-key versus subscription changes only how the Host Gateway authenticates and where it
forwards the request; generated Harness configuration, requested model, session handling,
limits and Transport Trace use the same path.

Subscription credentials remain checkout-local:

- Claude: `CLAUDE_CODE_OAUTH_TOKEN` in the checkout `.env`;
- Codex: `.ale/auth/codex-cli/auth.json`;
- Grok: `.ale/auth/grok-build/auth.json`.

ALE never discovers or modifies ordinary `~/.claude`, `~/.codex` or `~/.grok` profiles.
Codex and Grok access tokens are read and refreshed on the Host. Normal model requests
do not acquire a profile lock. A cross-process `flock` covers only the rare refresh and
atomic `0600` replacement, so concurrent Tasks share the current access token without
racing refresh-token rotation.

The Gateway forwards Codex subscription traffic to the ChatGPT Codex backend, Grok
traffic to the selected CLI chat-proxy dialect and Claude traffic to Anthropic Messages.
It retries one subscription 401 after refresh and one transient 429. It never falls back
to an API key or another model.

Codex's ChatGPT backend rejects the public Responses `max_output_tokens` field. ALE omits
that field for this upstream. Completed usage is recorded and the next request is refused
after a configured ceiling, but ALE cannot hard-cap the current response with that field.

## Consequences

Same-profile episodes can run concurrently; provider-owned account limits still bound
actual throughput. All three subscription Harnesses retain Gateway model authority,
duplicate coalescing, token accounting, configured ceilings and Transport Trace.
Provider billing remains external; `cost_usd` is an ALE estimate only when pricing is
known.

The isolated login files remain provider-owned, but their refresh endpoints and JSON
fields are compatibility inputs. ALE pins CLI versions, validates profile shape and
permissions, fails closed on drift and keeps OS-keychain support deferred. Shared
consumer-account pools and credential resale remain out of scope.
