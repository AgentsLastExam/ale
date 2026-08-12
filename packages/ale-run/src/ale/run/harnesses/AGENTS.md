# Harness agent guide

Read [`README.md`](README.md) and `docs/specs/autonomous-harness.md` before changing a
Harness.

- The official agent program runs as `Identity.AGENT` inside the Task Sandbox.
- API-key and subscription model calls use `HarnessSession.gateway_url` and the episode
  token; provider credentials and upstream selection remain Host-side.
- Keep native configuration and state below `HarnessSession.home`; never discover the
  operator's ordinary agent home.
- Validate settings and optional resources before launch. Unknown settings fail closed.
- Pin the native program version and record a meaningful integrity identity.
- Native logs are evidence, not canonical trajectory. Parsing must be deterministic and
  preserve malformed/incomplete evidence as explicit issues.
- Native continuation is valid only for the original episode, Sandbox and effective
  model/settings/resources/authentication fingerprint.
- Provider-specific config and log formats stay in the provider Harness. Share only
  mechanics that are already identical across several adapters.

Run the Harness unit tests, conformance tests and the matching real Sandbox integration.
Any CLI-version or model-discovery behavior also requires its marked live acceptance.
