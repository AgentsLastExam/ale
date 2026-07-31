# Unified episode logging

An episode keeps six orthogonal canonical records. A fact has one authoritative home;
other files may carry only stable identifiers, references, or small run-level projections.

| Artifact | Owns | Does not own |
|---|---|---|
| `trajectory.json` | agent-visible messages, reasoning actually exposed, tool calls/results, images, subagents | setup/verify commands, rewards, aggregate usage |
| `trace.transport.jsonl` | solver Gateway model calls, retries, refusals, usage, cost, call-to-step links | full conversation messages or verification Judge calls |
| `trace.execution.jsonl` | framework phases, task/framework commands, output, policy, cleanup failures | agent-owned tool calls, rewards |
| `result.json` | terminal status, all named rewards, failure, phase timings | conversation, provenance |
| `verification.json` | criterion diagnostics, metrics, aggregates, and Judge invocations/attempts | terminal status, solver trajectory, native Agent transcript |
| `lock.json` | immutable provenance and enforced termination limits | live run state |

`events.jsonl`, `trace.semantic.jsonl`, per-episode ledger databases, scalar/primary
rewards, and unconditional `agent-output-*.txt` files do not exist.

## ATIF trajectory

`trajectory.json` is a Harbor ATIF v1.7 document, not an ALE envelope. It is validated
before atomic replacement:

```json
{
  "schema_version": "ATIF-v1.7",
  "session_id": "native-session",
  "trajectory_id": "trajectory-...",
  "agent": {
    "name": "claude-code",
    "version": "2.1.220",
    "model_name": "claude-opus-4-8"
  },
  "steps": [
    {"step_id": 1, "source": "user", "message": "Complete instruction"}
  ]
}
```

Step IDs start at 1 and are contiguous. The complete instruction is the first
agent-visible user content. Native tool-use/result events are grouped into one agent
step; every observation `source_call_id` resolves to a tool call in that step. An empty
result is valid, while a missing result remains absent.

All ALE extensions live under `extra.ale`:

- `native_event_ids`: observed native identities;
- `transport_call_ids`: Gateway calls correlated after parsing;
- `attachments`: non-image BlobRefs;
- `mcp`: logical MCP server and tool;
- `normalized_action`: CUA action correlated with the same MCP call;
- `incomplete` and `incomplete_reason`: valid observed prefix after interruption.

Images use ATIF image parts whose `source.path` is episode-relative. Subagents use
embedded `subagent_trajectories` with unique `trajectory_id` values and resolvable
`subagent_trajectory_ref` entries. Same-sandbox native resume extends the same logical
session; `continued_trajectory_ref` is used only when a continuation is stored as a
separate document.

Optional token IDs, masks, log probabilities, sampling metadata, reasoning, and metrics
are written only from exact observed evidence. ALE never retokenizes text or fabricates
missing training fields.

## Transport trace

Only the Gateway writes `trace.transport.jsonl`. Every complete line has
`schema_version`, monotonic `seq`, timestamp, episode ID, and one of:

- `call`: stable `call_id`, model, request/response digests, provider response ID,
  usage, cost, stop reason, latency, and `forwarded`/`failed`/`refused` disposition;
- `replay`: the original call ID, request digest, replay reason, and `charged=false`;
- `trajectory_link`: call ID, trajectory ID, and ATIF step ID.

Retries never double-charge. Failed and refused calls remain visible. Default records
contain digests and accounting metadata, not provider request/response bodies or a second
copy of the conversation.

## Execution trace

`trace.execution.jsonl` is the chronological diagnostics channel for ALE and task
stages. One `ale.execution` standard-library logger is bound through `contextvars` to the
current episode, phase, and component. Task scripts do not write JSONL themselves: their
stdout/stderr is progressively captured by the guest protocol and finalized by the
command recorder.

Event kinds:

- `phase_started`, `phase_finished`;
- `command_started`, `command_finished`;
- `policy_applied`;
- `log`, `failure`;
- `partial_output_recovered`.

Setup, oracle, verifier, harness preparation/launch, artifact collection, and cleanup are
framework/task execution. Agent-owned Bash/MCP/CUA calls belong only in ATIF.

Command output at or below 16 KiB stays inline. Larger output becomes a text BlobRef.
Credentials, bearer tokens, provider keys, and configured secrets are redacted before
text reaches disk. Timeouts kill the process group, drain both pipes, preserve received
output with `complete=false`, and record a terminal command outcome. A host restart
recovers deterministic `.partial` files as incomplete blobs. A torn JSONL suffix is
reported and never silently repaired.

## Blobs

External payloads use:

```text
blobs/<image|audio|video|document|text|binary>/sha256-<digest>[.<safe-extension>]
```

The reference owns `path`, MIME type, byte size, SHA-256, and completeness. Identical
bytes deduplicate within the episode. Extensions come from trusted MIME mappings.
Canonical JSON never embeds base64. No blob manifest exists.

## Result and run status

`result.json` is the episode's atomic terminal truth. A completed result has a non-empty,
finite named reward map and no failure. A failed result has failure information and no
rewards. ALE supplies no primary reward or aggregation policy.

`runs/<run>/ledger.db` is a rebuildable run-level projection for queued/running/current
phase/terminal/interrupted state. It uses WAL and stores bounded failures and final reward
maps, never full trajectory, trace, lock, or native-log documents. Rows are inserted
before the concurrency semaphore, marked running before episode orchestration, updated
by explicit phase callbacks, and projected terminal only after `result.json` is durable.
On reopen, stale queued/running rows become interrupted; retries receive new episode IDs.

## Retention

```toml
[logging]
native_logs = "minimal"
transport_payloads = "digests"
token_data = "none"
```

- `native_logs="minimal"` deletes successfully converted native logs after ATIF
  validation. `debug` retains byte-faithful logs under `logs/<harness>/`.
- Conversion failure or interruption always retains available native evidence.
- `transport_payloads="debug"` may retain provider payload evidence under
  `logs/gateway/`; `digests` is the default.
- `token_data="exact"` permits exact observed token evidence; `none` is the default.

Exact mode accepts only an explicit upstream `ale_token_data` object containing observed
token IDs, completion log probabilities, sampled mask, sampling temperature, and any
multimodal processor fields. ALE may preserve a temperature explicitly sent in the
request, but never retokenizes text or fabricates missing fields. The temporary evidence
is correlated by Gateway call ID, moved into the linked ATIF agent step, then deleted;
malformed evidence fails conversion and remains under `logs/gateway/tokens/`.

TODO: extend exact-token extraction to streamed provider responses once a local
OpenAI-compatible model endpoint exposing token IDs, log probabilities, masks, and
multimodal processor state is available for end-to-end Prime-RL testing.

Native logs are diagnostic evidence, never canonical truth.

An Agent Judge is not a solver Harness and produces no ATIF trajectory. When invoked,
its bounded, sanitized native JSONL transcript is retained at
`logs/agent-judge.jsonl`; otherwise that path is absent.

## Verification record

`verification.json` is atomically replaced after each accepted mutation. It preserves
completed deterministic criteria and judge attempts even when later verification fails.
The verifier owns this file until `run.sh` exits. ALE then validates it against the
reward envelope and copies it unchanged to the episode. No intermediate snapshot is
sent to the Host, and Judge calls never merge into the solver's `trajectory.json` or
Transport Trace.

## Interoperability

Harbor consumes `trajectory.json` directly and receives the complete reward map.
Prime Verifiers conversion creates message/tool nodes, branch/subagent relationships,
linked model calls, timings, metrics, and all named rewards. Prime-RL conversion requires
aligned token IDs, sampled mask, log probabilities, temperatures, and multimodal
processor data when applicable; otherwise it raises `IncompleteTrainingDataError`.
Scalar-only destinations require an explicit caller-supplied aggregation callback.

The Harbor v1.7 schema source is the adjacent
`harbor/src/harbor/models/trajectories/` model set. Schema parity is checked by
`tests/conformance/test_harbor_atif.py`. After reviewing a Harbor version change,
regenerate the checked fixture from the ALE repository root:

```bash
uv run --project ../harbor python -c \
  'import json; from pathlib import Path; from harbor.models.trajectories import Trajectory; Path("tests/fixtures/atif-v1.7.schema.json").write_text(json.dumps(Trajectory.model_json_schema(), indent=2, sort_keys=True) + "\n")'
```

Commit the fixture and the corresponding ALE model/conformance changes together.
