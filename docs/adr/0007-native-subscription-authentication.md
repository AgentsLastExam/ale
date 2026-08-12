# 0007 - Native subscriptions use isolated ALE profiles inside the Sandbox

**Status:** Accepted

## Context

Claude Code, Codex CLI, and Grok Build can run non-interactively against a user's
provider subscription after one native login. Their supported subscription path is not
the API-key Gateway path: the official CLI must own its native authentication state and
talk to its native provider service.

The evaluated agent program must continue to run inside the Task Sandbox. The operator
also requires ALE not to discover, modify, refresh, or log out the profile used by their
ordinary Host-side agent commands. Credential confidentiality from evaluated code is not
a requirement of this feature.

## Decision

`agent.authentication` is `auto | api-key | subscription`. `auto` selects a subscription
only when ALE's dedicated source exists; otherwise it preserves API-key behavior. An
effective mode never changes after preflight and subscription failure never falls back to
an API key or another model.

Claude uses `CLAUDE_CODE_OAUTH_TOKEN` from the ALE checkout's gitignored `.env`; ALE's
Host Gateway exchanges an episode token for that OAuth Bearer token because the pinned
native Claude binary does not reliably honor an authenticated CONNECT proxy. Codex and
Grok use only:

- `<checkout>/.ale/auth/codex-cli/auth.json`
- `<checkout>/.ale/auth/grok-build/auth.json`

ALE never reads or writes ordinary `~/.claude`, `~/.codex`, or `~/.grok` profiles.
`.ale/` is excluded from Git and Docker build contexts. The operator creates each ALE
profile once with the official CLI and an inline isolated `CODEX_HOME` or `GROK_HOME`
pointed at the current checkout.

For each Codex/Grok episode ALE holds an exclusive Host lock, copies the selected
credential into the episode-local native home, and runs the official CLI as the
unprivileged agent. A valid changed file is atomically copied back while the lock is
held; the guest copy is then removed best-effort. Same-profile episodes are serialized.
The evaluated agent can read, change, delete, or exfiltrate the staged credential, and
published framework metadata records that exposure without recording the raw value or
path. Claude receives only the episode token; its long-lived token remains in the
checkout-local Host environment.

Codex/Grok subscription traffic uses the existing episode-authenticated CONNECT proxy
with only Task-declared hosts plus pinned native provider hosts. Claude sampling traffic
uses the existing Gateway as an unobserved OAuth relay. Subscription Runs do not apply
Gateway limits/accounting and do not retain `trace.transport.jsonl`; native evidence,
phase deadlines, Harness-native limits, verification, and Task isolation remain in
force.

This decision supersedes ADR 0004's statement that both Harness families always use the
same Gateway and narrows ADR 0006's transport-trace requirement to Gateway-observed
API-key traffic.

## Consequences

ALE reuses provider-owned login and refresh behavior without implementing OAuth. The
operator's normal Host agent configuration remains independent, while account-owned
quota, entitlement, rate, and endpoint behavior remain provider-controlled and require
live acceptance against each pinned CLI. Subscription Runs are less observable than API
Runs and are suitable only when the operator accepts credential exposure to evaluated
code and the provider's applicable terms.
