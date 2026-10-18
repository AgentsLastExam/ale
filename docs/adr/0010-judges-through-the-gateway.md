# 0010 - Judges reach their endpoint through the Gateway

**Status:** Accepted

## Context

LLM and Agent Judges ran inside the verify sandbox and called their endpoint directly, so
the Judge's real API key was placed in the verify environment. Verification can execute
code the solver wrote (`checks.command`), and the shared verify phase reopens egress, so
that code could read the key and spend it elsewhere. Judge calls were also invisible to
the transport trace that records every agent call.

## Decision

- The Gateway serves sessions with their own upstream. Each episode opens one session per
  configured Judge holding that Judge's protocol, exact endpoint and key; the verifier
  receives the Gateway address and the session token in place of the key.
- Judge sessions are unlimited and never replay a cached response, so a retried verdict
  samples again. Their calls are recorded with `purpose: judge` and excluded from the
  agent's trajectory and usage.
- Every command that can run a verifier (`run`, `validate`, `reverify`) starts a Gateway
  when Judges are configured, even when no agent needs one. A retained sandbox reattached
  by a later run is given that run's Gateway port.
- The pre-run Judge probe still calls the endpoint directly from the Host.

## Consequences

No credential of any kind enters a sandbox: agents and Judges alike hold only episode
tokens that die with the episode. Judge traffic is observable in the same trace as agent
traffic. Verification depends on the Host Gateway, which already runs for the episode.
