# Trace specification

An episode produces two trace layers, written as JSON Lines, plus externalised blobs.
The layers exist because we have exactly two trustworthy capture points: the gateway
(all model traffic) and the guest service (everything that happens in the sandbox).

## Transport layer — `trace.transport.jsonl`

One record per model call, written by the gateway. Nothing else may write it.

```json
{"seq": 3, "ts": "2026-07-24T12:00:00Z", "episode_id": "…", "model": "…",
 "request_digest": "sha256:…", "response_digest": "sha256:…",
 "input_tokens": 812, "output_tokens": 96, "cost_usd": 0.0123,
 "stop_reason": "end_turn", "latency_ms": 2140, "refused": false}
```

A retried request that the gateway serves from its replay cache produces exactly one
record: retries never double-count.

**Calls that did not succeed are still calls.** A provider failure carries
`upstream_status` and no usage; a call the gateway itself refused carries `refused` with
the `refusal_limit` that stopped it. Recording nothing for either would make a run that
spent eleven minutes being rate-limited indistinguishable from an idle agent — the same
evidence leading to opposite conclusions.

```json
{"seq": 4, "ts": "…", "episode_id": "…", "model": "…", "request_digest": "sha256:…",
 "input_tokens": 0, "output_tokens": 0, "upstream_status": 529, "refused": false}
{"seq": 7, "ts": "…", "episode_id": "…", "model": "…", "request_digest": "sha256:…",
 "refused": true, "refusal_limit": "max_cost_usd"}
```

## Semantic layer — `trace.semantic.jsonl`

Typed step records describing what happened, independent of agent family.

```json
{"seq": 1,  "ts": "…", "kind": "instruction", "text_digest": "sha256:…"}
{"seq": 9,  "ts": "…", "kind": "exec", "argv_digest": "sha256:…", "exit_code": 0,
 "stdout_ref": "artifacts/exec-9.out"}
{"seq": 14, "ts": "…", "kind": "observation", "screenshot_ref": "shots/0003.png"}
{"seq": 15, "ts": "…", "kind": "action", "action": {"type": "click", "coordinate": [512, 340]}}
{"seq": 22, "ts": "…", "kind": "verifier", "exit_code": 0, "rewards": {"reward": 1.0}}
{"seq": 23, "ts": "…", "kind": "timing", "total_ms": 808, "model_ms": 0,
 "sandbox_ms": 28, "framework_ms": 780,
 "phases": [{"name": "provision", "duration_ms": 305}, {"name": "setup", "duration_ms": 48},
            {"name": "agent", "duration_ms": 19}, {"name": "verify", "duration_ms": 20}]}
```

Kinds: `instruction`, `agent_output`, `exec`, `observation`, `action`, `verifier`,
`timing`, `note`. Screenshots and other large payloads are written as files and
referenced by relative path — never inlined as base64.

## Timing

A `timing` record closes every episode. One duration answers nothing useful: twenty
minutes is a slow model, a slow sandbox or slow framework code, and each has a different
fix.

The split is **derived from records already on disk** — model time from the transport
layer's latencies, sandbox time from `exec` durations, framework time as the remainder —
so it cannot drift from what happened. `model_ms + sandbox_ms + framework_ms == total_ms`
holds by construction; the shares are clamped, because concurrent calls can otherwise sum
past the wall clock and produce a negative remainder.

`phases` covers provisioning through verification. A phase that timed out or crashed
still contributes its duration: that is the one you most want.

An episode driven by a policy harness must contain at least one `observation` and one
`action`, or a typed failure explaining why not.

## Migrated agents

An adapted third-party agent keeps its native log, stored under
`artifacts/agent-native/`. It is evidence, not a substitute: the two layers above are
still produced, so results stay comparable across agents.

## Reserved extension

A `training` slot is reserved for token-level data (identifiers, log-probabilities,
sampling masks). It is absent in current output and defined only when a trainer
integration is actually built.
