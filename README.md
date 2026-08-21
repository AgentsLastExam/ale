# ALE

ALE runs agents against isolated Tasks, verifies their work, and records the evidence
needed to reproduce every result.

## Quickstart

```bash
git clone git@github.com:AgentsLastExam/ale.git
cd ale
just bootstrap
uv run ale --help
```

Run a Task with a provider API key named in the checkout's `.env`:

```bash
uv run ale run /path/to/task --agent claude-code \
  --model qwen3.7-max \
  --base-url https://dashscope.aliyuncs.com/apps/anthropic \
  --api-key-env QWEN_API_KEY
```

Or exercise the complete pipeline without a model credential:

```bash
uv run ale run /path/to/task --agent oracle
uv run ale run /path/to/task --agent nop
```

## Start here

| Need | Read |
|---|---|
| Understand the runtime and CLI | [`packages/ale-run/README.md`](packages/ale-run/README.md) |
| Understand contracts and persisted records | [`packages/ale-core/README.md`](packages/ale-core/README.md) |
| Write verification code | [`packages/ale-verify/README.md`](packages/ale-verify/README.md) |
| Configure subscription login | [`docs/guides/subscription-auth.md`](docs/guides/subscription-auth.md) |
| Design and author a Task | [`docs/specs/task-quality-standard.md`](docs/specs/task-quality-standard.md), [`docs/specs/task-authoring.md`](docs/specs/task-authoring.md) |
| Navigate specifications and decisions | [`docs/README.md`](docs/README.md) |
| Develop ALE | [`docs/guides/development.md`](docs/guides/development.md) |

## Repository layout

```text
packages/ale-core/    public contracts and interfaces
packages/ale-run/     orchestration, providers, harnesses, Gateway and CLI
packages/ale-verify/  sandbox-local verification library
images/               Sandbox bases, image builders and Provider runtimes
docs/                 maintained specifications, decisions and guides
tests/                engine unit, conformance, integration and live acceptance tests
```

Task content lives in independent Task repositories. The engine repository contains
framework code and foundational images, not benchmark Tasks.

Before changing the repository, read [`AGENTS.md`](AGENTS.md).
