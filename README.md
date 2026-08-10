# ALE

Evaluation orchestrator for Agents' Last Exam: run agents against sandboxed tasks,
score them, and record exactly what produced every result.

One engine repository, many independent Task folders. This repo contains framework code
and foundational sandbox images, not benchmark Task content.

## Quickstart

Bootstrap the engine from a clean machine:

```bash
git clone git@github.com:AgentsLastExam/ale.git && cd ale
just bootstrap                                    # or: uv sync --frozen
uv run ale --help
```

`just doctor` explains anything missing.

### Task format

A standard Task is a self-contained folder with an explicit name and its own
`image/Dockerfile` or a matching-kind `image.ref`. Every Task explicitly declares
`image.kind: container|vm`; ALE builds locally when the fixed Dockerfile exists and
otherwise asks the matching Provider to acquire the ref. There is no `domain.yaml`,
Domain Kit, shared Image Tree, or Task `files/`.

The manifest remains `core/v1`. The implemented contract is documented in
[docs/specs/task-folder.md](docs/specs/task-folder.md).

Solver model access goes through the Gateway, so put a key in the checkout's `.env`
(copy `.env.example`). The evaluated agent receives only a URL and per-episode token;
the upstream key stays on the Host. Trusted verification Judges are separate: their
run-configured key is injected only into the verifier command environment.

The gateway supports Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses.
A run names its endpoint and which variable holds the key, so several can be configured
at once and nothing has to be edited between runs:

```bash
uv run ale run /path/to/task --agent claude-code \
  --model qwen3.7-max \
  --base-url https://dashscope.aliyuncs.com/apps/anthropic \
  --api-key-env QWEN_API_KEY
```

The key is *named*, never passed — a value on a command line is in shell history and in
every process listing on the machine.

To try the pipeline with no key and no model at all:

```bash
uv run ale run /path/to/task --agent oracle   # runs the task's own solution
uv run ale run /path/to/task --agent nop      # does nothing; scores a real zero
```

### The rest of the surface

```bash
uv run ale lint /path/to/task          # static checks
uv run ale validate /path/to/task      # untouched zero, then record oracle result
uv run ale new-task /path/to/task      # self-contained scaffold
uv run ale assets status /path/to/task-repo
uv run ale assets pull /path/to/task-repo
uv run ale assets push /path/to/task-repo
uv run ale sandbox list                     # retained debug sandboxes
uv run ale sandbox destroy HANDLE
uv run ale run <task> -n 5 --run-id sweep     # five episodes, resumable by that id
uv run ale run <task> --require-reportable    # fail unless provenance could be published
```

`--run-id` is what makes an interrupted run resumable: episodes are matched by what they
are, not by when they ran, so re-invoking the same command finishes the work rather than
repeating it.

## What a run leaves behind

Under `runs/<run>/<episode>/`:

- `lock.json` — everything that produced the result: Task source, prepared solver and
  prepared solver/verifier images, actual Providers, agent identity, one optional asset commit/dirty observation, requested
  and observed resources, sandbox lifecycle outcomes, configuration, seed, and engine
  commit. A run whose lock cannot back a published number says so.
- `trace.transport.jsonl` — every model call, written by the gateway and by nothing else.
  Calls that failed upstream are recorded too, with their status: silence about a failed
  call is indistinguishable from an idle agent.
- `trajectory.json` — Harbor ATIF v1.7 agent-visible messages, tools, observations,
  media references, subagents, and continuations.
- `trace.execution.jsonl` — setup/verify/framework phases, commands, streamed output,
  policy application, and cleanup diagnostics.
- `result.json` — terminal status, all named rewards, failure, phase timings, and
  destroyed/retained sandbox outcomes.
- `verification.json` — the local derivation of rewards, including checks, aggregates,
  and direct Judge attempts.
- `blobs/` — content-addressed large or binary payloads referenced by the records above.
- `artifacts/` — the paths the task declared, if this run asked to keep them.

Task semantics: [docs/specs/task-design.md](docs/specs/task-design.md). Writing Tasks:
[docs/guides/task-authoring.md](docs/guides/task-authoring.md). The standard lifecycle:
[docs/specs/standard-environment.md](docs/specs/standard-environment.md). Verification:
[docs/specs/verification.md](docs/specs/verification.md). The complete documentation map
starts at [docs/README.md](docs/README.md).

## Concepts

| Term | Meaning |
|---|---|
| **Task folder** | the complete self-contained authored source unit |
| **TaskSpec** | the effective serializable specification of one selected Task instance |
| **Task collection** | a directory or source containing independent Task folders |
| **Environment** | how one task becomes one episode (provision → agent → verify) |
| **Sandbox** / **Provider** | an isolated execution instance / the backend supplying it |
| **Harness** | binds an agent to the framework — *autonomous* (agent owns its loop) or *policy* (framework owns the observe/act loop) |
| **GuestServer** | `ale-guestd`, the in-sandbox service every exec, file transfer and screenshot goes through |
| **Gateway** | controlled solver model egress; enforces limits, isolates credentials, records every solver call |
| **Episode** / **Run** | one administration of one task by one agent / a batch of episodes plus its ledger |
| **Verdict** / **RunLock** | the result envelope / the provenance record binding a result to everything that produced it |

The full glossary is [`docs/specs/lexicon.md`](docs/specs/lexicon.md); each term has
exactly one meaning across the codebase.

## Repository layout

```
packages/ale-core/    contracts, interfaces, conformance testkit
packages/ale-run/     providers, gateway, guest service, harnesses, engine, CLI
packages/ale-verify/  sandbox-local checks, direct Judges, records
images/base/          sandbox base images (published to GHCR)
docs/README.md        maintained documentation entry point
docs/adr/             current architectural decisions and rationale
docs/specs/           living normative specifications
docs/guides/          contributor and operator workflows
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
See [`docs/guides/development.md`](docs/guides/development.md) for the reasoning, and
[`CLAUDE.md`](CLAUDE.md) for the rules coding agents must follow.

## Governance

Start at [`docs/README.md`](docs/README.md). It links the current specifications, guides,
architectural rationale, and project constitution.
