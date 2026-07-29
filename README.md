# ALE

Evaluation orchestrator for Agents' Last Exam: run agents against sandboxed tasks,
score them, and record exactly what produced every result.

One engine repository, many task repositories. This repo contains no task content.

## Quickstart

Three commands from a clean machine to a scored result:

```bash
git clone git@github.com:AgentsLastExam/ale.git && cd ale
just bootstrap                                    # or: uv sync --frozen
uv run ale run demo/hello --agent claude-code
```

Task content is fetched from the domain's task repository (pinned in
[`registry.toml`](registry.toml)) and cached; the sandbox image is pulled on first use.
`just doctor` explains anything missing.

Model access goes through the gateway, so put a key in the checkout's `.env` (copy
`.env.example`). It never enters a sandbox — the agent gets a URL and a per-episode token,
and the gateway meters and records every call whatever the agent does.

Any Anthropic-compatible endpoint works. A run names its endpoint and which variable holds
the key, so several can be configured at once and nothing has to be edited between runs:

```bash
uv run ale run demo/hello --agent claude-code \
  --model qwen-latest-series-invite-beta-v92 \
  --base-url https://dashscope.aliyuncs.com/apps/anthropic \
  --api-key-env QWEN_API_KEY
```

The key is *named*, never passed — a value on a command line is in shell history and in
every process listing on the machine.

To try the pipeline with no key and no model at all:

```bash
uv run ale run demo/hello --agent oracle   # runs the task's own solution
uv run ale run demo/hello --agent nop      # does nothing; scores a real zero
```

### The rest of the surface

```bash
uv run ale lint tasks/            # static checks, no container
uv run ale validate tasks/        # every oracle must produce non-empty all-ones rewards
uv run ale new-task tasks/mine    # scaffold a task that already passes both
uv run ale run <task> -n 5 --run-id sweep     # five episodes, resumable by that id
uv run ale run <task> --require-reportable    # fail unless provenance could be published
```

`--run-id` is what makes an interrupted run resumable: episodes are matched by what they
are, not by when they ran, so re-invoking the same command finishes the work rather than
repeating it.

## What a run leaves behind

Under `runs/<run>/<episode>/`:

- `lock.json` — everything that produced the result: task source and commit, resolved
  image digest, agent version and identity, every asset revision, the sandbox's user and
  whether it could elevate, configuration hash, seed, engine commit. A run whose lock
  cannot back a published number says so.
- `trace.transport.jsonl` — every model call, written by the gateway and by nothing else.
  Calls that failed upstream are recorded too, with their status: silence about a failed
  call is indistinguishable from an idle agent.
- `trajectory.json` — Harbor ATIF v1.7 agent-visible messages, tools, observations,
  media references, subagents, and continuations.
- `trace.execution.jsonl` — setup/verify/framework phases, commands, streamed output,
  policy application, and cleanup diagnostics.
- `result.json` — terminal status, all named rewards, failure, and phase timings.
- `blobs/` — content-addressed large or binary payloads referenced by the records above.
- `artifacts/` — the paths the task declared, if this run asked to keep them.

Writing tasks: [docs/task-authoring.md](docs/task-authoring.md). Building images:
[docs/specs/sandbox-image.md](docs/specs/sandbox-image.md). Porting old tasks:
[docs/migration-from-legacy.md](docs/migration-from-legacy.md). What is and is not
guaranteed: [docs/security-model.md](docs/security-model.md).

## Concepts

| Term | Meaning |
|---|---|
| **TaskSpec** | the complete, serializable specification of one task instance |
| **Taskset** | loads task instances; named variants expand here |
| **Environment** | how one task becomes one episode (provision → agent → verify) |
| **Sandbox** / **Provider** | an isolated execution instance / the backend supplying it |
| **Harness** | binds an agent to the framework — *autonomous* (agent owns its loop) or *policy* (framework owns the observe/act loop) |
| **GuestServer** | `ale-guestd`, the in-sandbox service every exec, file transfer and screenshot goes through |
| **Gateway** | the sole controlled egress for model and judge traffic; enforces limits, isolates credentials, records every call |
| **Episode** / **Run** | one administration of one task by one agent / a batch of episodes plus its ledger |
| **Verdict** / **RunLock** | the result envelope / the provenance record binding a result to everything that produced it |

The full glossary is [`docs/specs/lexicon.md`](docs/specs/lexicon.md); each term has
exactly one meaning across the codebase.

## Repository layout

```
packages/ale-core/    contracts, interfaces, conformance testkit
packages/ale-run/     providers, gateway, guest service, harnesses, engine, CLI
images/base/          sandbox base images (published to GHCR)
docs/adr/             one-page decision records (append-only)
docs/specs/           living normative specifications
registry.toml         domain → task repository mapping
```

## Development

`just --list` shows the developer surface. The essentials:

```bash
just bootstrap        # install this tree (per-tree .venv, shared cache: seconds)
just doctor           # prove this tree behaves like main; prints a fix per failure
just wt my-feature    # create ../ale-worktrees/my-feature and bootstrap it
just test             # unit tests
just lint             # ruff + import boundaries
```

Always use `uv run <cmd>`; never activate a virtualenv and never `pip install`.
See [`docs/development.md`](docs/development.md) for the reasoning, and
[`CLAUDE.md`](CLAUDE.md) for the rules coding agents must follow.

## Governance

Design decisions are recorded in [`docs/adr/`](docs/adr/); normative specifications in
[`docs/specs/`](docs/specs/). Both are deliberately compact. Contributions are reviewed
against the project constitution (`docs/constitution.md`).
