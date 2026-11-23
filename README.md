# Orbit

Orbit runs agents against isolated Tasks, verifies their work, and records the evidence
needed to reproduce every result.

## Quickstart

```bash
git clone git@github.com:AgentsLastExam/orbit.git
cd orbit
just bootstrap
uv run orbit --help
```

Copy `.env.example` to `.env` and fill the key of the provider you use. Each Harness
calls its preset's provider with `ORBIT_<PROVIDER>_API_KEY`, at `ORBIT_<PROVIDER>_BASE_URL`
when set and the provider's own API otherwise:

```bash
uv run orbit run /path/to/task --agent claude-code --model claude-sonnet-5
```

For a one-off endpoint, name the `.env` variable holding its key:

```bash
uv run orbit run /path/to/task --agent claude-code \
  --model qwen3.7-max \
  --base-url https://dashscope.aliyuncs.com/apps/anthropic \
  --api-key-env ORBIT_QWEN_API_KEY
```

Or exercise the complete pipeline without a model credential:

```bash
uv run orbit run /path/to/task --agent oracle
uv run orbit run /path/to/task --agent nop
```

## Start here

| Need | Read |
|---|---|
| Understand the runtime and CLI | [`packages/orbit-run/README.md`](packages/orbit-run/README.md) |
| Understand contracts and persisted records | [`packages/orbit-core/README.md`](packages/orbit-core/README.md) |
| Write verification code | [`packages/orbit-verify/README.md`](packages/orbit-verify/README.md) |
| Configure subscription login | [`docs/guides/subscription-auth.md`](docs/guides/subscription-auth.md) |
| Design and author a Task | [`docs/specs/task-quality-standard.md`](docs/specs/task-quality-standard.md), [`docs/specs/task-authoring.md`](docs/specs/task-authoring.md) |
| Navigate specifications and decisions | [`docs/README.md`](docs/README.md) |
| Run macOS Tasks | [`docs/guides/macos-orchard.md`](docs/guides/macos-orchard.md) |
| Develop Orbit | [`docs/guides/development.md`](docs/guides/development.md) |

## Repository layout

```text
packages/orbit-core/    public contracts and interfaces
packages/orbit-run/     orchestration, providers, harnesses, Gateway and CLI
packages/orbit-verify/  sandbox-local verification library
images/                 Sandbox bases, image builders and Provider runtimes
docs/                   maintained specifications, decisions and guides
tests/                  engine unit, conformance, integration and live acceptance tests
```

Task content lives in independent Task repositories. The engine repository contains
framework code and foundational images, not benchmark Tasks.

Before changing the repository, read [`AGENTS.md`](AGENTS.md).
