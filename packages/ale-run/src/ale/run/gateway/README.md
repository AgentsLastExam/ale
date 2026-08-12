# Gateway

The Gateway is the only model-traffic entry point exposed to a Task Sandbox. Each
episode receives a short-lived bearer token and a local URL; the Host Gateway selects
the upstream, applies authentication, enforces configured limits, records usage and
writes the Transport Trace.

| Module | Responsibility |
|---|---|
| `server.py` | HTTP server, session authorization and upstream forwarding |
| `session.py` | episode sessions, usage and limit state |
| `anthropic.py` | Anthropic Messages normalization |
| `openai_responses.py` | OpenAI Responses normalization |
| `openai_chat_completions.py` | Chat Completions normalization |
| `proxy.py` | allowlisted non-model egress for Sandbox processes |

API-key and subscription modes share the same Sandbox-side protocol. Subscription
credential loading and refresh live in `ale.run.subscription`; configuration is covered
by [`../../../../../../docs/guides/subscription-auth.md`](../../../../../../docs/guides/subscription-auth.md).

The trust and logging contract is normative in
[`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md)
and [`../../../../../../docs/specs/trace.md`](../../../../../../docs/specs/trace.md).
