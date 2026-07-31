# Writing a task

A task is a folder. Four files make a complete one, and the scaffold gives you all four
already working, so your first move is to change something rather than to fill in blanks.

## Start

```bash
git clone https://github.com/AgentsLastExam/ale-tasks-base ale-tasks-<domain>
cd ale-tasks-<domain> && git remote rename origin upstream
# edit domain.yaml: set `name`

ale new-task tasks/my_first
ale lint tasks/my_first        # passes now
ale validate tasks/my_first    # scores 1.0 now
```

Clone rather than fork: GitHub allows one fork per organisation, and several domains
usually live in the same one. The result behaves identically.

The scaffold's oracle writes exactly what its verifier expects, so you begin from a green
run. Change one thing, re-run `ale validate`, and you always know which change broke it.

## The four files

```
tasks/my_first/
├── task.yaml        # what to provision, what data, what to keep
├── instruction.md   # the prompt — the only file the agent sees
├── verify/run.sh    # scores the result; copied in after the agent is gone
└── oracle/run.sh    # your own solution; run in place of the agent by `ale validate`
```

`setup/run.sh` is optional and runs in the sandbox before the agent.

Nothing declares these scripts. A stage's folder is copied in when that stage runs and
its `run.sh` executes if present, so the layout on disk *is* the execution order.

## Skills and MCP required by one task

Declare agent resources in `task.yaml`:

```yaml
tools:
  skills:
    - { path: skills/reviewer }
  mcp_servers:
    - { path: mcp/search.toml }
```

These paths are relative to the task folder and cannot escape it or enter `verify/` or
`oracle/`. They are added to Run-level resources, not substituted for them. Duplicate
identical logical names collapse to one; the same name with different content is an
error.

Framework-owned built-ins are deliberately not valid Task declarations. An operator
enables `cua-desktop` at Run level when evaluating GUI tasks.

See `ale-tasks-base/tasks/demo/resource_injection` for a complete Task whose verifier
requires the agent to read an injected Skill and call an injected Task MCP server.
Its MCP fragment is generated at call time. The task receipt is useful to the verifier
but is not trusted proof of tool use; live harness acceptance must match it against the
collected native transcript and canonical MCP trajectory.

`ale-tasks-152/tasks/demo/tool_smoke` is the complementary harness acceptance task. The
agent inventories callable endpoints, exercises every safe bounded tool, and reports
failed or untestable tools explicitly. Parameter modes are not separate tools: for
example, `web.run` remains one callable whether it performs search, open, or finance.
Tools that end or yield the current invocation are recorded as untested, and the report
is updated after each call so a later failure cannot erase earlier evidence.

The task's oracle proves only that its verifier accepts a valid report. A harness is
accepted only after a real model run matches the report against native logs, ATIF,
Gateway transport, execution trace, verifier result, and an independent LLM audit.

For an image or other media input, stage it as a normal task file or setup asset and say
where it is:

```markdown
Inspect /home/user/input/screenshot.png and write the answer to
/home/user/output/result.txt.
```

The file is present in the sandbox, so the agent can use its own supported file-reading
tools. Whether a model can interpret that media is a model choice, not a Task capability
declaration.

## Identity comes from the path

`tasks/demo/hello` in domain `demo` becomes `demo-hello`. Never write an id in the file.

It is opaque: nothing in the framework parses it back into parts, so you can reorganise
folders without touching data, prompts or stored results.

## Where your data goes is your decision

There is no framework layout to conform to. A mount says where its data comes from and
where it lands; both are yours to choose:

```yaml
setup:
  assets:
    - repo: agents-last-exam/ale-tasks-assets
      revision: 1d0d026c7a21226f619b74f3912e23573c7105ab
      path: mydomain/my_first/input
      dest: /ale/input
```

**Pin a commit, not a branch.** A branch moves, and then two runs that named the same
source read different bytes — which quietly makes their scores incomparable. `ale lint`
rejects anything that is not a commit.

The framework creates the destinations and artifact paths you declared.
**Everything else your task needs, your setup script creates.** If
`setup/run.sh` writes to `/ale/input` and no mount put anything there, `mkdir -p` it
first — a directory that happens to exist in one image is not a contract.

## Keeping answers away from the agent

Put them in the verify stage:

```yaml
verify:
  assets:
    - { repo: …, revision: …, path: mydomain/my_first/reference, dest: /ale/reference }
```

The guarantee is **timing**, not a flag. A verify-stage mount is materialised during
scoring, so while the agent works it is not hidden — it is absent. Same for `verify/` and
`oracle/` themselves.

Anything the agent must not see goes here. There is no way to mark a file secret and
leave it in the setup stage, because that would be a promise the framework could not keep.

## Instructions are rendered once, strictly

```markdown
Solve ${n} cases and write the answers to /ale/output/result.txt
```

`${param}` comes from `params`, merged with the variant's. Strict both ways: an
undeclared placeholder and an unused parameter are both errors. Write paths **literally** —
your task chose them and the image is fixed, so there is nothing to compute.

Legacy patterns (`{self.input_dir}`, `E:\agenthle`, `${output_dir}`) are rejected.

For a value that only exists at run time — a generated secret, a URL for a service your
setup started — do not template it. Have setup write a file and tell the agent to read it.
That keeps the instruction static, hashable, and free of the answer.

## Variants

```yaml
params: { n: 3 }
variants:
  - { name: base }
  - { name: hard, params: { n: 10 } }
```

One task instance each, own rendered instruction, own identity. They share an `id` and
differ in `variant`, so results aggregate either way without anyone parsing a string.

## Scoring

Use a relocatable entry point:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$(dirname "$0")/check.py"
```

ALE stages its Python 3.12+, standard-library `ale_verify` package only during
verification. The verifier must use `python3`; it must not select a Conda environment,
virtual environment, or another interpreter path:

```python
from ale_verify import Verification, checks

verification = Verification()
verification.check("format", checks.file_exists("/home/user/output/result.json"))
verification.stat("checked_files", 1)
verification.aggregate("overall")
verification.write()
```

`check()` and `judge()` add named component rewards. `aggregate()` is explicit and uses
stored criterion weights, which default to equal weights. `write()` writes all named
rewards and metrics; it does not choose a hidden primary reward. The verifier must use
`write()` so ALE can validate the local Verification Record against the reward envelope.

Shared domain logic is an ordinary flat Python package:

```text
kits/verification_example/__init__.py
```

Select its exact import name in `task.yaml`:

```yaml
verify:
  kits: [verification_example]
```

Then Task-local code, the framework package, and the Domain Kit compose normally:

```python
from ale_verify import Verification
from verification_example import approved_record

verification = Verification()
verification.check("approved", approved_record("/home/user/output/result.json"))
verification.aggregate("overall")
verification.write()
```

There is no Kit manifest, alias, inventory, or lock file. ALE import-probes the selected
package and records the hash of the actual staged bytes for each episode.

LLM and agent judges are requested from the same state object with explicit scored
rubrics. Task code supplies the prompt, rubric, and evidence paths. It never supplies a
provider client, credential, model, Harness, or dialect:

```python
verification.judge(
    "llm",
    "correctness",
    prompt="Judge correctness.",
    rubric={
        "no": {"score": 0.0, "description": "Incorrect."},
        "yes": {"score": 1.0, "description": "Correct."},
    },
    files=["/home/user/output/result.txt"],
)
```

The operator configures execution outside task content:

```toml
[verification.llm]
model = "gpt-5.4-mini"
reasoning_effort = "medium"
base_url = "https://api.openai.com"
api_key_env = "OPENAI_API_KEY"

[verification.agent]
adapter = "codex-cli"
model = "gpt-5.4"
reasoning_effort = "high"
base_url = "https://api.openai.com"
api_key_env = "OPENAI_API_KEY"
```

Judges run synchronously inside the completed sandbox. LLM Judges call the configured
provider directly. Agent Judges invoke the selected image-provided CLI as root with an
isolated native home and retain a sanitized raw transcript at
`logs/agent-judge.jsonl`; they do not create another trajectory.

Judge infrastructure errors, timeouts, refusals, and malformed responses fail the
episode. They never become a synthetic zero reward.

A verifier that exits non-zero or writes nothing is a `task_error` — a defect in *your*
task — and is deliberately distinct from a zero score. Keep that distinction sharp: it is
what stops a broken verifier from looking like a hard task.

## Before you submit

```bash
ale lint tasks/           # every task in the repo
ale validate tasks/       # untouched all-zero, then oracle all-one
```

CI runs both. Every task must have `oracle/run.sh`; there is no threshold or manual
bypass. Validation runs the real verifier twice in independent sandboxes: setup directly
to verify must emit the same non-empty reward names all at exactly `0.0`, then the oracle
path must emit them all at exactly `1.0`. Configured judges run for both passes.

## Who runs what

Your `setup/run.sh` and `verify/run.sh` run as **root**. They are framework machinery,
executed on your task's behalf.

The **agent runs as an unprivileged user** — and so does your **oracle**, because it
stands in for the agent. That is deliberate: it means `ale validate` meets the same limits
a real run will, so a task that leaves the agent unable to write something fails the gate
instead of failing an evaluation later.

**You decide what the agent can touch.** The framework creates what you declared — asset
destinations, artifact paths, the workspace — and hands those to the agent. Anything your
setup then produces is yours to open up:

```bash
# setup/run.sh — runs as root
printf 'seed\n' > /ale/input/state.txt
chown user /ale/input/state.txt      # the agent has to be able to rewrite this
```

Forget it and your own `ale validate` will tell you, because the oracle hits the same wall.

If your task genuinely needs to install software or change system configuration, say so:

```yaml
resources: { cpus: 2, memory_mb: 4096, sudo: true }
```

The sandbox is configured for it and the grant is recorded in the run's provenance —
an episode with elevation was less isolated, and results should not be compared across
that line without it being visible.

## GUI tasks

A graphical program cannot talk to somebody else's session, so setup — which is root —
drops to the desktop user:

```bash
setsid --fork runuser -u user -- eog --fullscreen /ale/input/code.png </dev/null &
```

The session's environment is supplied for you; you do not need to know where its bus is.

Wait for what you actually need rather than sleeping. The first screenshot is taken the
moment setup returns, and a window that exists is not yet a window that fills the screen.

## Traps that have already cost time

Each of these produced a task that looked like it worked:

- **`set -e` with `pipefail` and a command substitution.** `x="$(cmd | tail -1)"` aborts
  the whole script when `cmd` fails — which it does on the first loop iteration, before
  the thing you are waiting for exists. Re-running by hand then passes, because by then it
  does exist. Add `|| true`.
- **A backgrounded process with a bare `&`.** Everything in the exec session's process
  group dies when setup returns, so your viewer is killed the moment setup finishes. By
  hand the shell stays alive and it survives. Use `setsid --fork`.
- **Painting the X root window.** GNOME draws its own background over it; nothing appears.
- **Setting the wallpaper with `gsettings` as root.** dconf cannot commit without the
  session bus, so the value changes and the screen never repaints.

## Two things people get wrong

**Assuming a directory exists.** The framework builds only what you declared. Create the
rest yourself.

**Reaching for an upstream image.** `python:3.12-slim` is a build environment, not a
sandbox: no unprivileged user, no command that keeps it alive. Build on one of ours, or
build a curated image `FROM` one — see `docs/specs/sandbox-image.md`.
