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

## Semantic layer — `trace.semantic.jsonl`

Typed step records describing what happened, independent of agent family.

```json
{"seq": 1,  "ts": "…", "kind": "instruction", "text_digest": "sha256:…"}
{"seq": 9,  "ts": "…", "kind": "exec", "argv_digest": "sha256:…", "exit_code": 0,
 "stdout_ref": "artifacts/exec-9.out"}
{"seq": 14, "ts": "…", "kind": "observation", "screenshot_ref": "shots/0003.png"}
{"seq": 15, "ts": "…", "kind": "action", "action": {"type": "click", "coordinate": [512, 340]}}
{"seq": 22, "ts": "…", "kind": "verifier", "exit_code": 0, "rewards": {"reward": 1.0}}
```

Kinds: `instruction`, `agent_output`, `exec`, `observation`, `action`, `verifier`,
`note`. Screenshots and other large payloads are written as files and referenced by
relative path — never inlined as base64.

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
