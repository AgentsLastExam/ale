# Verification

ALE verification is one Task-owned program executed inside the active verification
sandbox. The independent `ale_verify` package supplies deterministic checks, direct LLM
and Agent Judges, aggregation, and an atomic record. There is no Domain Kit,
Verification Service, Judge Gateway, or Host callback.

## Task entry point

The standard shape is:

```text
verify/
├── run.sh
├── verify.py
├── Dockerfile       # optional, separate mode only
└── assets/          # optional, synchronized outside Git
```

`run.sh` executes the verifier by a relative path:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 verify.py
```

ALE runs the stage with `verify/` as cwd. Task code should use ordinary relative paths
for its own code and assets, and literal absolute paths for solver outputs. The selected
verification image must expose system `python3` 3.12 or newer; ALE stages the package
into that interpreter's site-packages.

## Stateful API

```python
from ale_verify import Verification, checks

verification = Verification()
verification.check("format", checks.file_exists("/home/user/output/report.json"))
verification.stat("files_checked", 1)
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

Task code owns the prompt, rubric, evidence paths, optional reference, optional solver
trajectory, and invocation-local Agent Judge MCP servers. Run TOML owns execution
configuration:

```toml
[verification.llm]
model = "gpt-5-mini"
reasoning_effort = "medium"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[verification.agent]
adapter = "codex-cli"
version = "1.2.3"
model = "gpt-5-mini"
reasoning_effort = "medium"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

LLM Judge protocol is inferred from the endpoint and supports Anthropic Messages, OpenAI
Chat Completions, and OpenAI Responses. Agent Judge supports the small package-owned
`codex-cli` and `claude-code` adapters. Its exact numeric CLI version is mandatory; ALE
uses that installed binary or installs that exact version inside verification. Agent
Judge runs as root in a fresh private native home and may inspect the completed workspace.
It creates a bounded native transcript, not an ATIF trajectory.

Both Judge types call the configured endpoint directly from the sandbox. The named API
key is injected only into the verify command environment, redacted from captured output,
and never written to Task configuration or records. A Judge makes at most three attempts;
invalid verdicts receive a schema-repair prompt, while transient failures retry.

## Records and failure

`verification.json` contains ordered criteria, stats, aggregates, Judge invocations and
attempts, diagnostics, status, and failure. ALE collects it after `run.sh`, verifies that
its rewards and stats exactly match the reward envelope, and stores it at the episode
root. A launched Agent Judge also produces sanitized `logs/agent-judge.jsonl`.

Malformed output, missing configuration or credentials, endpoint/provider failure,
timeout, refusal, invalid Judge schema, missing evidence, and verifier exceptions are
verification or infrastructure failures. They never silently fall back to a zero score.

Placement and artifact restoration are specified by
[standard-environment.md](standard-environment.md); persistent record ownership is
specified by [trace.md](trace.md).
