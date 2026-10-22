# Unified episode logging

An episode keeps six orthogonal canonical records. A fact has one authoritative home;
other files may carry only stable identifiers, references, or small run-level projections.

| Artifact | Owns | Does not own |
|---|---|---|
| `trajectory.json` | agent-visible messages, reasoning actually exposed, tool calls/results, images, subagents | setup/verify commands, rewards, aggregate usage |
| `trace.transport.jsonl` | Gateway calls in API-key and subscription modes (verification Judge calls marked `purpose: judge`), retries, refusals, usage, cost when price is known, call-to-step links | full conversation messages |
| `trace.execution.jsonl` | framework phases, task/framework commands, output, policy, cleanup failures | agent-owned tool calls, rewards |
| `result.json` | terminal status, all named rewards, failure, phase timings | conversation, provenance |
| `verification.json` | the complete Task-authored result, including nested details | terminal status, solver trajectory, native Agent transcript |
| `lock.json` | immutable provenance and enforced termination limits | live run state |

`events.jsonl`, `trace.semantic.jsonl`, per-episode ledger databases, scalar/primary
rewards, and unconditional `agent-output-*.txt` files do not exist.

Compatibility boundary: during early development a record is read only by the engine
commit recorded in its RunLock (`framework.commit`). ALE ships no migrations or legacy
readers; a record whose shape differs from the reading engine fails validation instead of
being reinterpreted with values that were never observed.

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
    "version": "2.1.227",
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
- `incomplete` and `incomplete_reason`: valid observed prefix after interruption, or
  `conversion_failed` when the Harness parser raised and only the instruction and final
  message could be recorded;
- `parse_issues`: bounded `{reason, detail}` records of native evidence the parser could not
  convert.

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
  usage, cost, stop reason, latency, and `forwarded`/`failed`/`refused` disposition.
  `input_tokens` counts every input token in every dialect, and `cache_read_tokens` and
  `cache_write_tokens` say how many of them the provider's prompt cache served or stored.
  `cost_usd` is a list-price estimate for an exact model ID, or `null` when that model has
  no listed price; it is never a guessed or zero cost;
- `replay`: the original call ID, request digest, replay reason, and `charged=false`;
- `trajectory_link`: call ID, trajectory ID, and ATIF step ID.

Retries never double-charge. Only a successful provider response (2xx) or a Gateway refusal is replayed
to a retry of the same request; after a provider error (5xx, or a provider 429) the retry is forwarded
and recorded as a new `call`. A streamed 2xx response counts as successful only when it reached its
dialect's terminal event (`message_stop`, a completed or incomplete Responses result, or `[DONE]`)
without an in-band error; an interrupted or failed stream is forwarded again on retry. Failed and refused calls remain visible. Default records
contain digests and accounting metadata, not provider request/response bodies or a second
copy of the conversation.

Subscription traffic takes the same path:
Claude Code, Codex CLI, and Grok Build send every model call to the Gateway, which only
changes how it authenticates upstream, so those calls are recorded here like API-key calls.
Provider billing remains external; `cost_usd` is an ALE estimate only when pricing is known.

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
On reopen, stale queued/running rows become interrupted. An explicit resumed invocation
creates new episode IDs for work that has no completed result.
The reuse identity includes the observed asset repository, Task path, and fixed asset commit
alongside Task, image, agent, and configuration identity. A changed asset revision cannot reuse
an older completed episode. Dirty or unversioned assets disable completed-episode reuse.
Its configuration identity is `RunConfig.reuse_hash`, which excludes the scheduling settings
`episodes`, `concurrency`, `episode_retries`, and `resume`; resuming a Run with a larger count
or another parallelism runs only the missing episodes. RunLock still records the complete
`config_hash`.

`RunConfig.episode_retries` sets the number of additional automatic attempts for an
incomplete episode; its default is zero. `ale run --episode-retries N` and
`--set episode_retries=N` override the run TOML. Each attempt runs in fresh Sandboxes
with a new Gateway session and trajectory, preserving the logical episode ID and seed.
Only `status=completed` stops retries successfully; a completed zero score is still a
completed episode. `status=timeout` and `status=budget_exceeded` are limit terminations
and terminal without retry: the timed-out phase and its deadline, or the exhausted limit,
remain in the failure details, and another attempt would grant a fresh limit. Judge infrastructure failures stop without another episode attempt;
their internal retry budget is defined in [Verification](verification.md). Host cancellation
interrupts the loop immediately. Missing results
caused by a host interruption are not treated as an unlimited retry request.

There is one run-level ledger row per logical episode and one current result directory:

```text
runs/<run>/<episode>/
  result.json                  # last attempt; only this result participates in the run
  lock.json                    # provenance of the same attempt
  trajectory.json              # evidence of the same attempt, when produced
  attempts/1/                  # first attempt, only if a later retry replaced it
    result.json
    lock.json
    logs/...
  attempts/2/...               # next replaced attempt, if any
```

Every archived attempt retains its complete records, artifacts, and relative blob paths.
Archives are never additional evaluation samples. Consumers of a run enumerate immediate
episode directories (`*/result.json`), not recursive result files. While a retry is
running, the current directory has no terminal result; the previous failed result is
already archived. The ledger remains running until the last result is durable. Exhaustion
leaves that last failed result current and makes the run fail. Successful logical episodes
are not repeated by another episode's retry or by resuming the same completed run.

When automatic retries are enabled, failed attempts destroy their owned Sandboxes even
when retention requested `keep`; successful final attempts use the configured policy.
The same result layout applies to validation and separate reverification; their execution
boundaries are specified in [Standard Environment](standard-environment.md).

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

Streamed responses that do not supply this explicit object record no exact token data.

Native logs are diagnostic evidence, never canonical truth.

An Agent Judge is not a solver Harness and produces no ATIF trajectory. When invoked,
its sanitized native stdout/stderr are retained as response events in
`agent-judge.jsonl` alongside its Judge events; otherwise that path is absent.

## Verification result

`verification.json` preserves the complete Task-authored JSON result. Only `overall`
is projected into episode rewards. `llm-judge.jsonl` and `agent-judge.jsonl` sit alongside
it and are collected independently, including after failure.
See [Verification](verification.md) for the result and tool contracts.

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
