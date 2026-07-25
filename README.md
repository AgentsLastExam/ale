# ALE

Evaluation orchestrator for Agents' Last Exam: run agents against sandboxed tasks,
score them, and record exactly what produced every result.

One engine repository, many task repositories. This repo contains no task content.

## Quickstart

```bash
git clone git@github.com:AgentsLastExam/ale.git && cd ale
just bootstrap                  # or: uv sync --frozen
uv run ale run demo/readfile_secret --agent claude-code
```

Task content is fetched automatically from the domain's task repository (pinned in
[`registry.toml`](registry.toml)) and cached. `just doctor` explains anything missing.

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
