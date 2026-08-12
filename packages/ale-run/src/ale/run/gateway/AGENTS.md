# Gateway agent guide

Read [`README.md`](README.md), `docs/specs/security.md` and `docs/specs/trace.md` first.

- Authorize every request with its live episode session; never trust Sandbox headers as
  provider credentials or upstream selection.
- Redact credentials before logs, errors, events or persisted payloads are written.
- Keep dialect translation local and preserve streaming semantics and usage accounting.
- Limits fail closed and are shared by API-key and subscription upstreams.
- The Gateway never imports Providers, Environments or Harness implementations.
- A protocol or subscription compatibility change needs a focused request/stream/error
  test and the matching live acceptance when credentials are available.

Run the Gateway unit tests, isolation integration tests and `just lint`.
