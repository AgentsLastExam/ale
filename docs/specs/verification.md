# Verification

ALE verification is one Task-owned program executed inside the active verification
sandbox. The independent `ale_verify` package supplies deterministic checks, direct LLM
and Agent Judges, aggregation, and an atomic record. There is no Domain Kit,
Verification Service, Judge Gateway, or Host callback.

The Task's verification entry point, working directory, Python requirement, and private assets
are defined in [Task authoring](task-authoring.md#verify). The persisted record follows
[JSON schema version 1](schemas/verification-record-v1.json).

## Stateful API

Task-owned inspection computes a `CheckResult` named `content_result` from the actual outcome
and its acceptance conditions. Recording that result uses the same API for every domain:

```python
from ale_verify import Verification

verification = Verification()
verification.check("correctness", content_result)
verification.aggregate("overall")
verification.write()
```

- `check(name, CheckResult, weight=1.0)` adds a deterministic reward criterion.
- `judge(kind, name, ..., weight=1.0)` adds an `llm` or `agent` reward criterion.
- `stat(name, value)` records a finite observation that is not a reward.
- `aggregate(name, inputs=None, weights=None)` adds a reward equal to the mean of selected
  criteria. Stored criterion weights are used by default; explicit weights must match the
  selected inputs exactly.
- `write()` finalizes the record and writes the complete named reward map. It is required
  exactly once after at least one criterion or aggregate.

A Task verifier must expose an explicit `overall` aggregate and retain the underlying named
criteria as detailed evaluation evidence.

Every accepted mutation atomically replaces `verification.json`. Names are non-empty and
unique across criteria, stats, and aggregates. Scores are finite values from 0 through 1;
weights are finite and positive. Calls after completion, and checks after an Agent Judge
has started, fail explicitly.

The built-in standard-library checks cover files, text, JSON, CSV, SQLite, numeric
tolerance, local HTTP, subprocess output, and selected trajectory observations. Task-local
domain logic belongs beside `verify.py` and composes with the same `Verification` object.

## Judges

A Judge rubric explicitly assigns every choice a score and description:

```python
verification.judge(
    "llm",
    "correctness",
    prompt="Judge only whether the report answers the requested question.",
    files=["/home/user/output/report.json"],
    rubric={
        "no": {"score": 0.0, "description": "Materially incorrect."},
        "partial": {"score": 0.5, "description": "Correct but incomplete."},
        "yes": {"score": 1.0, "description": "Correct and complete."},
    },
)
```

Task code owns the Judge kind, prompt, rubric, evidence paths, optional reference, optional
solver trajectory, and invocation-local Agent Judge MCP servers. It does not select a model,
reasoning effort, endpoint, credential, harness, or harness version. These are execution
configuration supplied by the runtime, outside the Task and the `judge()` API.

LLM Judge protocol is inferred from the endpoint and supports Anthropic Messages, OpenAI
Chat Completions, and OpenAI Responses. Agent Judge supports the small package-owned
`codex-cli` and `claude-code` adapters. Its exact numeric CLI version is mandatory; ALE
uses that installed binary or installs that exact version inside verification. Agent
Judge runs as root in a fresh private native home and may inspect the completed workspace.
It creates a bounded native transcript, not an ATIF trajectory.

### Evidence inputs

`files` contains absolute paths to local regular files. LLM Judge sends UTF-8 text as text,
and PNG, JPEG, GIF, WebP, and PDF content as native multimodal inputs. Image and PDF types
are identified from their contents; binary bytes are never decoded as text or replaced by
filenames. Responses uses `input_image` and `input_file`, Chat Completions uses `image_url`
and `file`, and Anthropic Messages uses `image` and `document` blocks. The configured
model and endpoint must support the supplied modalities; rejection is a verification failure.

Agent Judge accepts bounded binary or text files and receives their paths for inspection
through its harness. `reference` is inline text supplied to both Judge types, including on
retries. `trajectory=True` includes the selected solver trajectory; it is otherwise omitted
from Judge inputs. A configured Agent Judge Task-instruction path must be readable UTF-8.

Each file is limited to 20 MiB, with a combined evidence limit of 32 MiB per invocation.
Inline references are limited to 256 KiB; LLM text files and selected trajectories are
limited to 1 MiB each. Oversized, missing, non-regular, or unsupported evidence fails
explicitly; evidence is never silently truncated. Symbolic-link evidence files are rejected.
Records retain paths, byte sizes, and SHA-256 hashes, not inline media payloads. Schema-repair
and transport retries retain the same selected evidence.

LLM Judge transmits selected files; it does not render documents or extract additional evidence.
Agent Judge can inspect the supplied files with its configured harness tools. Evidence and rubric
validity are governed by the [Task quality standard](task-quality-standard.md#3-evaluate-evidence-fairly).

Both Judge types call the configured endpoint directly from the sandbox. The named API
key is injected only into the verify command environment, redacted from captured output,
and never written to Task configuration or records. A Judge makes one initial attempt and
at most three retries;
invalid verdicts receive a schema-repair prompt, while transient failures retry.

## Records and failure

`verification.json` contains ordered criteria, stats, aggregates, Judge invocations and
attempts, diagnostics, status, and failure. ALE collects it after the OS entry exits, verifies that
its rewards and stats exactly match the reward envelope, and stores it at the episode
root. A launched Agent Judge also produces sanitized `logs/agent-judge.jsonl`.

Malformed output, missing configuration or credentials, endpoint/provider failure,
timeout, refusal, invalid Judge schema, missing evidence, and verifier exceptions are
verification or infrastructure failures. They never silently fall back to a zero score.

Placement and artifact restoration are specified by
[standard-environment.md](standard-environment.md); persistent record ownership is
specified by [trace.md](trace.md).
