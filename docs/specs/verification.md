# Verification

ALE verification is one Task-owned program executed inside the active verification
sandbox. The independent `ale_verify` package supplies deterministic checks, LLM
and Agent Judges, aggregation, and an atomic record. There is no Domain Kit,
Verification Service, or Host callback.

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
verification.write()
```

- `check(name, CheckResult, weight=1.0)` adds a deterministic reward criterion.
- `judge(kind, name, ..., weight=1.0)` adds an `llm` or `agent` reward criterion.
- `judge_json(name, prompt=..., score=..., files=(), reference=None, weight=1.0)`
  obtains an LLM Judge JSON object and passes it to a Task-owned scoring function.
- `stat(name, value)` records a finite observation that is not a reward.
- `aggregate(name, inputs=None, weights=None)` adds a reward equal to the mean of selected
  criteria. Stored criterion weights are used by default; explicit weights must match the
  selected inputs exactly.
- `write()` finalizes the record and writes the complete named reward map. It is required
  exactly once after at least one criterion or aggregate.

Every reward map has an `overall` reward, the total that reports and leaderboards read.
`write()` adds it as the weighted mean of every criterion unless the verifier defined its
own `overall` aggregate; the named criteria remain as detailed evaluation evidence. A
verifier with no criteria must define `overall` itself. An aggregate without
`inputs` covers every criterion, so after one is added, a further `check()` or `judge()`
fails instead of being silently left out. Explicit-`inputs` aggregates leave criteria open.

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

Some Tasks require one LLM response with several answers before applying a deterministic
score, such as a document review with independent completeness and layout fields. For
these, `judge_json` sends the Task's prompt through the same configured LLM Judge and
returns a JSON object. The Task's `score` function validates its fields and returns a
`CheckResult`; its score becomes the Judge criterion. The invocation, attempts and evidence
are retained in the verification record. The response is limited to 64 KiB of finite JSON and must
not contain the configured credential. When solver evidence is missing or unusable, no
Judge is called, the criterion scores zero, and the method returns an empty object. A
transport or JSON-format failure fails verification; it is never converted into a score.

```python
answers = verification.judge_json(
    "correctness", prompt="Return a JSON object ...", score=score_answers
)
verification.write()
```

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
limited to 1 MiB each. Evidence is never silently truncated.

A `files` entry is solver output. When one is missing, a symbolic link or other non-regular
file, oversized, or (for an LLM Judge) neither UTF-8 text nor a supported image or PDF, or
when the files exceed the combined limit, or when `trajectory=True` but no solver trajectory
was recorded, the solver left nothing valid to judge: the criterion scores 0 with that
reason, is recorded as a deterministic `check`, and no Judge runs. Excluding such an episode
from aggregates would reward agents that produce nothing, and an authored path that the oracle
cannot satisfy already fails validation. A relative evidence path or an invalid inline
`reference` is an authoring error that raises before any Judge work. A staged trajectory that
cannot be read is an infrastructure failure.
Records retain paths, byte sizes, and SHA-256 hashes, not inline media payloads. Schema-repair
and transport retries retain the same selected evidence.

LLM Judge transmits selected files; it does not render documents or extract additional evidence.
Agent Judge can inspect the supplied files with its configured harness tools. Evidence and rubric
validity are governed by the [Task quality standard](task-quality-standard.md#3-evaluate-evidence-fairly).

Both Judge types reach their configured endpoint through the Host Gateway, as the agent
reaches its model. Each episode opens one Gateway session per configured Judge that holds
the Judge's endpoint, protocol and key; the verifier receives only the Gateway's address
and that session's token (`ALE_LLM_JUDGE_TOKEN`, `ALE_AGENT_JUDGE_TOKEN`), so code the solver
wrote cannot read a credential it could spend elsewhere. Judge sessions have no limits and
never replay a cached response, so a retried verdict samples again; their calls appear in
the transport trace with `purpose: judge` and are left out of the agent's trajectory and
usage. A Judge whose key is not set on the Host gets no session. The RunLock records each
Judge's configured endpoint, not the Gateway address the verifier saw.

A Judge decides a score, so ALE sets no ceiling of its own on how long it thinks or how
much it writes, and offers no setting for one. An LLM Judge streams its response and sends
no output-token limit; Anthropic's protocol requires `max_tokens`, so it receives the
model's own maximum as reported by the endpoint's Models API (128000 when the endpoint has
none, lowered to the limit a provider names when it rejects that value). A stream that is
silent for ten minutes is treated as a dropped connection.

Transient provider failures (connection loss, timeouts, truncated streams, and HTTP 408,
409, 425, 429, 5xx and 529) are retried with the same request and exponential backoff that
honours `Retry-After`, without an attempt count. The only bound is verification's own
budget: the engine passes the seconds left in the verify phase as
`ALE_VERIFY_DEADLINE_SECONDS`, and a Judge stops retrying 30 seconds before it so that the
failure is recorded rather than killed. Without that budget, as when an author runs
`verify.py` directly, a transient failure gets three further attempts instead. Any other
provider rejection fails at once. An
invalid or refused verdict gets at most three further attempts; invalid verdicts receive a
schema-repair prompt that names the error, and a verdict wrapped in a Markdown JSON fence
is accepted. Agent Judge runs each CLI attempt with no timeout but that remaining budget,
retries CLI failures and timeouts with the same evidence in a fresh native session, and
repairs invalid verdicts in the existing session. Missing credentials and native session
identity mismatches stop immediately. Judge infrastructure failures are terminal for the
episode; outer episode retries do not repeat them.

### Judge preflight

`ale run`, `ale validate` and `ale reverify` scan every selected Task's `verify/` Python for
`.judge(...)` and `.judge_json(...)` calls before preparing images or starting any agent.
`judge_json` selects the LLM Judge; a literal `judge` kind selects that Judge, while a
kind computed at run time counts as both. Each Judge kind found must be
configured, and one tiny request through `ale_verify.probe_judge` must succeed with its
endpoint, key and model (an LLM Judge also with its reasoning effort). Otherwise the command
stops with the reason and nothing runs, so a configuration mistake cannot turn into a batch
of paid agent episodes with no verdict. The probe runs from the host; it proves the Judge
configuration, not the verifier sandbox's route to the endpoint.

## Records and failure

`verification.json` contains ordered criteria, stats, aggregates, Judge invocations and
attempts, diagnostics, status, and failure. ALE collects it after the OS entry exits, verifies that
its rewards and stats exactly match the reward envelope, and stores it at the episode
root. A launched Agent Judge also produces sanitized `logs/agent-judge.jsonl`.

Malformed output, missing configuration or credentials, endpoint/provider failure,
timeout, refusal, invalid Judge schema, and verifier exceptions are verification or
infrastructure failures. They never silently fall back to a zero score.

Placement and artifact restoration are specified by
[standard-environment.md](standard-environment.md); persistent record ownership is
specified by [trace.md](trace.md).
