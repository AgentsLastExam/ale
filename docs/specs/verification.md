# Verification

Verification is an ordinary Task-owned program. ALE supplies optional Judge and check
tools, then reads one JSON result after the program exits successfully. There is no
scoring registry, automatic aggregation, callback, or required order of tool calls.

## Result

The verifier writes Harbor's reward file, `/logs/verifier/reward.json`
(`C:\logs\verifier\reward.json` on Windows): a flat JSON object whose values are numbers.
Its `reward` must be a finite number from 0 through 1; booleans and numeric strings are not
numbers. ALE interprets only `reward` and never averages or treats other values as rewards.
Beside it, `verification.json` may hold the complete result: any JSON object, nested values
included, whose `reward` equals `reward.json`'s. NaN, infinity and duplicate object keys
make either file invalid.

After checking the range, ALE rounds `reward` to two decimal places using decimal
round-half-up: `0.994` becomes `0.99`, `0.995` and `0.999` become `1.0`, and `0.004`
becomes `0.0`. This same value is saved and used by `run`, `reverify`, and `validate`.
Other fields and Judge responses keep their original precision; rounding never makes an
out-of-range score valid.

```python
from ale_verify import checks, write

check = checks.text_equals("/home/user/output/answer.txt", "42")
write({"reward": check["score"], "answer": check})
```

`write(result)` validates and rounds `reward`, then atomically replaces `verification.json`
with the complete result and `reward.json` with `{"reward": ...}`, without modifying the
caller's object. It does not end the program or mark the episode successful. Repeated
calls replace the previous result. Other languages or Python programs may write the same
files directly, `verification.json` being optional; the engine applies the same checks.
An exception, timeout or nonzero exit still fails verification after a result was written.

Before every attempt the engine clears the verification staging directory and empties the
verifier directory, which only the framework can then write
([Security](security.md#4-the-agent-cannot-change-what-it-is-measured-under)).

On the host, `verification.json` contains the complete result, which is `reward.json`'s
object when the verifier wrote no `verification.json`. Episode rewards contain only
`reward`; the episode status and failure remain in `result.json`.

## Judge tools

```python
from pathlib import Path

from ale_verify import judge, write


def verify():
    report = Path("/home/user/output/report.pdf")
    if not report.is_file():
        return {"reward": 0, "reasoning": "Required report is missing."}

    assessment = judge(
        "llm",
        "report_review",
        prompt="Assess correctness from 0 to 1 and explain your reasoning.",
        files=[str(report)],
        output_schema={
            "type": "object",
            "properties": {
                "correctness": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {"type": "string"},
            },
            "required": ["correctness", "reasoning"],
        },
    )
    return {"reward": assessment["correctness"], "review": assessment}


write(verify())
```

`judge(kind, name="judge", *, prompt, output_schema=None, files=(), reference=None,
trajectory=False, mcp_servers=())` returns a normal dictionary. `kind` is `llm` or
`agent`. Names label logs and need not be unique. Optional `output_schema` uses JSON
Schema draft 04, 06 or 07; without it the response must still be a JSON object. Schema
validation does not insert defaults or coerce values. References must be self-contained;
external schema downloads are unsupported. Task Python handles all subsequent scoring.

Both Judge kinds use the model provider, model and reasoning effort configured by the run.
A Judge names its provider rather than a key or URL, and its key and endpoint resolve as the
Gateway's do, from `ALE_<PROVIDER>_API_KEY` and `ALE_<PROVIDER>_BASE_URL` (see
[Security](security.md#3-provider-credentials-stay-on-the-host)). An LLM Judge also names the
dialect it speaks; an Agent Judge speaks its CLI's (Responses for Codex, Messages for Claude
Code).

```toml
[verification.llm]
provider = "openai"
dialect = "openai-responses"  # or openai-chat-completions, anthropic
model = "gpt-5.6-luna"
reasoning_effort = "medium"

[verification.agent]
provider = "anthropic"
adapter = "claude-code"  # or codex-cli
version = "2.1.227"
model = "claude-sonnet-5"
reasoning_effort = "high"
```

Agent Judge uses the configured exact CLI version, an invocation-specific native home,
and the framework's elevated identity (root on Linux, Administrator on Windows).
Tasks may call either Judge repeatedly, interleave ordinary checks, or inspect files
after a Judge runs. Agent Judge can modify the live workspace; already collected solver
artifacts and trajectory are not overwritten by those modifications.

`files` supplies absolute regular-file paths. LLM Judge supports text, PNG, JPEG, GIF,
WebP and PDF evidence; Agent Judge receives file paths to inspect. `reference` supplies
inline text; `trajectory=True` includes the solver trajectory. Missing or unusable evidence
raises `EvidenceError`; the tool does not decide that it deserves zero. Authors should check
expected submissions before invoking a Judge and assign missing or unusable submissions
the task's intended deductions or zero score. Passing missing evidence to a Judge still
raises. Invalid JSON or schema-mismatched model output receives the validation error and
schema for up to three repair attempts after the initial response; only exhaustion raises.
An unhandled Judge failure is not a score.

Evidence is bounded: 20 MiB per file, 32 MiB per invocation, 256 KiB for inline references,
and 1 MiB for text and trajectory inputs. These are transport limits, not scoring policy;
inputs are never silently truncated. There is no separate structured-response or
rubric-scoring mode.

## Judge records

Judge calls append durable events to `llm-judge.jsonl` or `agent-judge.jsonl` in the
episode directory, according to Judge kind. Each includes prompts, schemas, evidence
hashes, responses (including invalid ones), errors, attempts, model and usage when
available. Agent events also include native stdout/stderr, labeled by invocation.
Credentials are redacted. Logs survive
later task exceptions and are collected independently of a final result; a forcibly killed
process may leave an incomplete final event. Media evidence is referenced by hash rather
than duplicated in logs. RunLock records the observed Judge provenance and schema hash.

Both Judge types reach their configured endpoint through the Host Gateway, as the agent
reaches its model. Each episode opens one Gateway session per configured Judge that holds
the Judge's endpoint, protocol and key; the verifier receives only the Gateway's address
and that session's token (`ALE_LLM_JUDGE_TOKEN`, `ALE_AGENT_JUDGE_TOKEN`), so code the solver
wrote cannot read a credential it could spend elsewhere. Judge sessions have no limits and
never replay a cached response, so a retried verdict samples again; their calls appear in
the transport trace with `purpose: judge` and are left out of the agent's trajectory and
usage. A Judge whose key is not set on the Host gets no session. The RunLock records the
endpoint each Judge's provider resolved to, with an LLM Judge's request path, not the
Gateway address the verifier saw.

A Judge supplies evidence for task scoring, so ALE sets no ceiling of its own on how long it thinks or how
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
invalid or refused response gets at most three further attempts; invalid verdicts receive a
schema-repair prompt that names the error, and a verdict wrapped in a Markdown JSON fence
is accepted. Agent Judge runs each CLI attempt with no timeout but that remaining budget,
retries CLI failures and timeouts with the same evidence in a fresh native session, and
repairs invalid verdicts in the existing session. Missing credentials and native session
identity mismatches stop immediately. Unhandled Judge failures fail verification rather than becoming a score.

### Preflight

Before agent work, ALE scans Task Python for `judge(...)`, imported aliases and
`ale_verify.judge(...)`, and probes the required configured Judge kinds. Dynamic kinds
require both. This catches common configuration mistakes; runtime checks remain necessary
for dynamically loaded code and imports the static scan cannot discover.

## Runtime

The framework stages `ale_verify` and its pinned pure-Python JSON Schema dependency in
the verifier's Python environment. Both are included in runtime provenance. The selected
interpreter is supplied as `ALE_VERIFY_PYTHON`; see [Task authoring](task-authoring.md#tests).
Built-in checks return ordinary dictionaries with `score`, `diagnostic`, `raw_value` and
`evidence`. Authors may use them, ordinary Python, or their existing scoring functions.
