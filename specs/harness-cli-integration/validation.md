# Harness framework implementation validation

Date: 2026-10-23. Branch: `feat/harness-cli-integration`, based on ALE
`95fc769e456fffbc7e26bf7414993cb7657d7944`. These are local worktree results,
not a claim of a merged release or live-model qualification.

## Automated checks

- Full unit suite: 805 passed, 224 deselected, approximately 13 seconds.
- Public conformance and integration selection without infrastructure/live-model
  markers: 26 passed. This includes official Harbor ATIF v1.8 schema comparison and
  validation with Harbor's model.
- Independent `ale-verify` suite: 101 passed.
- Ruff, formatting, and all five import-boundary contracts: passed.
- `uv lock --check`: passed; no dependency change.
- `git diff --check`: passed.

The full unit suite is `uv run just test`; trusted verification uses
`uv run pytest packages/ale-verify/tests`; lint uses `uv run just lint`.
The selected conformance/integration command is:

```sh
UV_NO_CONFIG=1 UV_NO_SYNC=1 uv run pytest \
  -m '(conformance or integration) and not (needs_docker or needs_kvm or needs_llm or needs_gui or needs_gpu or needs_hf_gated or needs_hf_write or needs_hf_public)' \
  -q --tb=short
```

The Harbor checkout requires a newer uv than the installed host version, so its
comparison used existing installed environments without dependency synchronization.

## Transfer and conversion measurements

A real local Docker container exercised `CliHarness` and `CliLogCollector` with a
deterministic process that emitted stdout and stderr while remaining active:

| Observation | Result |
|---|---|
| UTF-8 instruction | All 262 bytes preserved, including Chinese, emoji, quotes, and literal shell syntax |
| Initial stdout | 8 MiB pulled in approximately 0.117 seconds |
| Appended stdout | Exactly the new 1 MiB pulled, approximately 0.020 seconds |
| Repeated unchanged sync | Zero content bytes read |
| Concurrent collection | Both pulls completed before the process exited |
| Final evidence | 9 MiB byte-identical stdout, separate stderr, no collection failure |

These times describe a local cached Docker image, not a remote-provider throughput
guarantee. A shared Provider conformance check also verifies metadata scans and ranged
reads during a running command. QEMU's file connection was exercised against a real
local threaded guest TCP server, not a booted VM.

The automated in-memory transfer measurement starts with 32 MiB of history and adds
2 MiB. Only those 2 MiB are read, in eight 256 KiB chunks; measured peak traced Python
allocation was approximately 537 KiB. This isolates collector overhead; it is not a
network benchmark. The test enforces transfer volume and a generous allocation bound,
without asserting machine-dependent timing.

Local parser measurements for 10,000 events were approximately 0.19–0.25 seconds for
Claude and 0.23–0.34 seconds for Codex, plus 0.02–0.04 seconds for serialization.
Conversion still rebuilds from the local mirror when bytes change; it does not keep
per-CLI incremental parser state. Periodic conversion runs off the Gateway event loop.

## Failure behavior covered

Focused tests exercise default-off native scans; UTF-8 instruction bytes and fixed
fallback prompts; separated streams; invocation boundaries on resume; partial JSONL
tails; append offsets; rewritten, replaced, disappearing, and newly appearing files;
snapshot validation; missing file identity; Windows paths and simulated junctions;
per-source fairness; bounded chunks; cancellation staging cleanup; final batch progress;
missing standard logs; transient and final parse failures; concurrent blob writes; and
cleanup failure after evidence capture.

A blocked parser test verifies that the episode event loop remains responsive and that
cancelling a periodic conversion prevents its late result from replacing the final
trajectory. Collected sessions outside a parser's coverage are reported as parse issues
and retained under minimal retention instead of silently discarded.

## Coverage limits

No paid/live native-model runs or real Windows/QEMU VM acceptance were performed.
The existing live acceptance callers were migrated and collection-checked. They still
need their usual credentials and infrastructure for execution.

Native session collection defaults off, so a default trajectory uses stdout evidence.
Enabling it preserves the declared session trees; it does not guarantee full conversion
of every native child-session format. Current per-CLI limitations are documented in
each package README. Changed snapshot files transfer in full; only verified append
sources use suffix reads.
