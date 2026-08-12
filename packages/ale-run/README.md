# ale-run

`ale-run` is ALE's executable engine. It loads Tasks, prepares images, provisions a
Sandbox, runs the selected Harness, verifies the result and writes the episode record.

```text
CLI → Task loading/image preparation → Episode → StandardEnvironment
    → Provider/Sandbox → Harness/Gateway → Verification → Result/RunLock
```

## Main areas

| Path | Responsibility |
|---|---|
| `src/ale/run/cli/main.py` | Typer command definitions and presentation |
| `src/ale/run/cli/tasks.py` | Task run, validation and image-preparation composition |
| `src/ale/run/environments/` | phase ordering for one Task episode |
| `src/ale/run/providers/` | Docker and QEMU Sandbox implementations; see its local README |
| `src/ale/run/harnesses/` | native agent adapters; see its local README |
| `src/ale/run/gateway/` | authenticated model routing, limits and Transport Trace; see its local README |
| `src/ale/run/guestd/` | dependency-free service running inside the Sandbox |
| `src/ale/run/tasksets/` | Task-folder discovery and manifest loading |
| `src/ale/run/episode.py` | one episode's resources, recording and terminal result |

## Common commands

```bash
uv run ale run TASK --agent claude-code
uv run ale validate TASK
uv run ale lint TASK
uv run ale prepare TASK
uv run ale sandbox list
```

Configuration is layered as CLI overrides, run TOML, Harness preset and model defaults.
Use `--set section.key=value` for any setting without a dedicated flag. Provider API-key
values belong in the checkout's gitignored `.env`; subscription setup is documented in
[`../../docs/guides/subscription-auth.md`](../../docs/guides/subscription-auth.md).

The durable runtime contracts are indexed by [`../../docs/README.md`](../../docs/README.md).
