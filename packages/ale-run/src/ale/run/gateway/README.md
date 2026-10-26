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
| `gemini.py` | Gemini `generateContent` normalization and request policy |
| `openai_responses.py` | OpenAI Responses normalization |
| `openai_chat_completions.py` | Chat Completions normalization |
| `proxy.py` | allowlisted non-model egress for Sandbox processes |

API-key and subscription modes share the same Sandbox-side protocol. Subscription
credential loading and refresh live in `ale.run.subscription`; configuration is covered
by [`../../../../../../docs/guides/subscription-auth.md`](../../../../../../docs/guides/subscription-auth.md).

The trust and logging contract is normative in
[`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md)
and [`../../../../../../docs/specs/trace.md`](../../../../../../docs/specs/trace.md).

## Dialects

The specs state the rules every dialect follows: the Gateway imposes the model, refuses
what it cannot account for, and gates provider-side network tools on the Task's network
policy. This section records how each dialect meets them.

| Dialect | Sandbox-side route | Episode token in | Model imposed in | Stream complete at | Limit refusal |
|---|---|---|---|---|---|
| `anthropic` | `POST /v1/messages` | `Authorization: Bearer` or `x-api-key` | body `model` | `message_stop` | 429 |
| `openai-chat-completions` | `POST /v1/chat/completions` | `Authorization: Bearer` or `x-api-key` | body `model` | `[DONE]` | 429 |
| `openai-responses` | `POST /v1/responses` | `Authorization: Bearer` or `x-api-key` | body `model` | `response.completed` or `response.incomplete` | 429 |
| `gemini` | `POST {prefix}/v1beta/models/{model}:generateContent`, `:streamGenerateContent?alt=sse` | `x-goog-api-key` or `?key=` | the `{model}` path segment | a candidate's `finishReason` | 400 `FAILED_PRECONDITION` |

A stream that ends without its terminal event, or with an in-band error, is not complete
and is not replayed to a retry. Every limit refusal carries `x-should-retry: false`.

Request changes beyond the model:

- Chat Completions streaming requests get `stream_options.include_usage=true`, the only
  way Chat reports usage on a stream, and legacy `max_tokens` becomes
  `max_completion_tokens`.
- The ChatGPT subscription backend rejects `max_output_tokens`, so it is dropped there and
  the ceiling is enforced after the response.
- Gemini's `generationConfig.maxOutputTokens` is forwarded as sent.

### Gemini

- **Routes.** The route matches at the Gateway root or below any path prefix the client
  keeps in its base URL. The prefix is not forwarded. Every `v1beta` path is Gemini's,
  `GET /v1beta/models` included, so the generic `/models` catalog does not answer there.
- **Methods.** `generateContent`, `streamGenerateContent` and `countTokens` are forwarded,
  each on the run's model. A stream must ask for `alt=sse`, the only stream form whose
  usage the Gateway reads. `countTokens` is recorded as a call with zero tokens and counts
  toward `max_model_calls` like any forwarded call. Every other method is refused with a
  Google error and recorded as `unsupported_method`:
  - `cachedContents` and files would store state with Google;
  - `embedContent` would run a model other than the run's;
  - the `models` listing would name models the run cannot use.
- **Grounding.** `googleSearch`, `googleSearchRetrieval`, `urlContext` and `googleMaps`, in
  either JSON spelling, reach the web from Google's side. A request whose `tools` include
  one is refused as `web_tools` unless the Task's network is `open`.
- **Refusal status.** Refusals are 400 `FAILED_PRECONDITION`, with an `ErrorInfo` detail,
  not 429. Antigravity CLI 1.4.7 retried a 429 eight times over about 2.5 minutes but
  fails a 400 at once, and a Gateway refusal does not lift by waiting.
- **No keep-alive.** Gemini streams get no SSE comment line. Antigravity CLI rejects one
  as an invalid chunk and fails the turn without retrying, so the provider's bytes pass
  through unchanged.
- **Accounting.** Usage comes from the last `usageMetadata`. `cachedContentTokenCount`
  counts as cache reads, and `thoughtsTokenCount` counts as output. `responseId` is
  recorded as the provider response ID. The replay digest covers the upstream path, so one
  body sent both streamed and unstreamed is never replayed as the other.
