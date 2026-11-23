# orbit-run

`orbit-run` is Orbit's executable engine. It loads Tasks, prepares images, provisions a
Sandbox, runs the selected Harness, verifies the result and writes the episode record.

```text
CLI → Task loading/image preparation → Episode → EpisodeProtocol
    → Provider/Sandbox → Harness/Gateway → Verification → Result/RunLock
```

## Main areas

| Path | Responsibility |
|---|---|
| `src/orbit/run/cli/main.py` | Typer command definitions and presentation |
| `src/orbit/run/cli/tasks.py` | Task run, calibration and image-preparation composition |
| `src/orbit/run/protocol/` | the episode protocol: phase ordering for one Task episode |
| `src/orbit/run/providers/` | Docker, Slurm, QEMU, Firecracker, Daytona, Orchard (macOS) and Fleet Sandbox implementations, and the choice among them; see its local README |
| `src/orbit/run/harnesses/` | native agent adapters; see its local README |
| `src/orbit/run/gateway/` | authenticated model routing, limits and Transport Trace; see its local README |
| `src/orbit/run/guestd/` | dependency-free service running inside the Sandbox |
| `src/orbit/run/tasksets/` | Task-folder discovery and `task.toml` loading |
| `src/orbit/run/episode.py` | one episode's resources, recording and terminal result |

## Common commands

```bash
uv run orbit run TASK --agent claude-code
uv run orbit run NEXT_TASK --agent claude-code --continue-from runs/RUN/EPISODE
uv run orbit calibrate TASK
uv run orbit lint TASK
uv run orbit prepare TASK
uv run orbit sandbox list
```

Configuration is layered as CLI overrides, run TOML, Harness preset and model defaults.
For `run`, `calibrate`, `prepare`, and `reverify`, `ORBIT_RUN_CONFIG` selects the default run
TOML when `--config` is absent. An explicit `--config` replaces that default file; `--set`
overrides values from the selected file. Use `--set section.key=value` for any setting
without a dedicated flag. `sandbox.providers` lists the Providers that start a Run's
Sandboxes ([Choose Providers](../../docs/guides/development.md#choose-providers)). Model
provider API keys belong in the checkout's gitignored `.env` under the `ORBIT_` names in
[`../../.env.example`](../../.env.example); subscription setup is documented in
[`../../docs/guides/subscription-auth.md`](../../docs/guides/subscription-auth.md).

The durable runtime contracts are indexed by [`../../docs/README.md`](../../docs/README.md).
