# Tests

| Directory | Purpose | Infrastructure |
|---|---|---|
| `unit/` | fast in-process behavior | none |
| `conformance/` | reusable public-contract suites | usually none |
| `integration/` | real Sandbox and cross-component behavior | marked by backend |
| `acceptance/` | live provider, model, GPU or GUI qualification | explicit live markers |

```bash
just test       # unit and lightweight conformance
just test-int   # conformance and integration
just lint       # formatting, lint and import boundaries
```

Tests that require infrastructure must carry the matching marker (`needs_docker`,
`needs_kvm`, `needs_gui`, `needs_llm`, or a Hugging Face marker) so local and CI runs can
select them deliberately. Prefer testing the public contract; use private helpers only
when the helper itself is the behavior under test.

Live acceptance tests make billed model calls, so CI runs none of them: a change to what
one covers runs it locally before merging. Most run only with
`ALE_RUN_LIVE_ACCEPTANCE=1`; the Judge tests instead skip without their provider's key.
They load the checkout's `.env` before any test module is imported, and `ALE_ENV_FILE`
points every worktree at one file. Every agent and Judge call resolves its key and endpoint
exactly as `ale` does, from `ALE_<PROVIDER>_API_KEY` and `ALE_<PROVIDER>_BASE_URL`, so a
shared gateway is configured once in `.env` rather than per test. Only the models are test
knobs: `ALE_LIVE_ANTHROPIC_MODEL` (default `claude-sonnet-5`) and `ALE_LIVE_OPENAI_MODEL`
(default `gpt-5.6-luna`); xAI calls use `grok-4.5`.
