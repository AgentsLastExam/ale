# Rebuilding a legacy Task

> **Feature 006 guide.** This document describes migration into the implemented
> self-contained container/VM Task protocol. The public manifest version remains `core/v1`.

Legacy Tasks are source material, not compatibility inputs. Preserve the prompt, useful
data, known solution, and correct verification behavior; rebuild their packaging and
initial state as one independent Task folder.

## Target folder

```text
tasks/<task>/
├── task.yaml
├── instruction.md
├── image/
│   ├── Dockerfile
│   └── assets/             # optional large local input
├── setup/
│   ├── run.sh
│   └── assets/
├── verify/
│   ├── run.sh
│   ├── verify.py
│   └── assets/
├── oracle/
│   ├── run.sh
│   └── assets/
└── tools/{skills,mcp}/
```

The rebuilt Task does not require `domain.yaml`, a repository Kit, a shared domain image,
or a sibling Task. It has an explicit stable `name`.

## 1. Separate solver input from answers

Classify every legacy data component before moving it:

| Legacy material | Target |
|---|---|
| small solver-visible input | commit under `image/` and `COPY` it |
| large solver-visible input | ignored `image/assets/`, restored by explicit asset sync |
| small answer/reference | keep under `verify/` |
| large answer/reference | ignored `verify/assets/`, read as `assets/...` from verifier cwd |
| large oracle-only input | ignored `oracle/assets/`, read as `assets/...` from oracle cwd |
| generated episode state | create in `setup/run.sh` |

Large solver input is developed at its final Task-local source path:

```text
tasks/legacy-task/image/assets/
```

Synchronize it explicitly, then let the Task Dockerfile place it:

```bash
uv run ale assets pull ../ale-tasks-cli/tasks/legacy-task
```

```dockerfile
COPY assets/input.json /home/user/input/input.json
```

Large references use `verify/assets/`:

```python
from pathlib import Path

expected = Path("assets/expected.json")
```

Never put answer material in the Task image. Docker layers retain bytes even when a later
layer deletes them.

## 2. Rebuild the environment in one Dockerfile

Declare the runtime explicitly with `image: {kind: container}` or `image: {kind: vm}`.

Start the final stage directly from the appropriate ALE base:

```dockerfile
FROM ghcr.io/agentslastexam/sandbox-base-cli:0.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends <required-packages> \
    && rm -rf /var/lib/apt/lists/*
```

Use `sandbox-base-gui` for desktop Tasks. Install every stable Task dependency, service,
runtime, and configuration here. Do not choose or create an Image Tree node and do not
publish a shared domain image.

Legacy `Required System Package` values are investigation clues, not fields to preserve.
Confirm what the old Task actually needed and encode only that in its Dockerfile.

The old Docker or VM image may help reconstruct dependencies. Prefer the local Dockerfile;
a ref-only Task declares `image.ref`. Tasks requiring a complete machine declare
`kind: vm`, end their Dockerfile in `sandbox-base-vm-gui`, and may install normal services
such as Docker. ALE owns guestd, boot, partition, and qcow conversion.

## 3. Split host-side legacy code

Legacy `main.py` commonly mixed several jobs:

| Legacy responsibility | Target |
|---|---|
| stable dependency installation | `image/Dockerfile` |
| fixed solver input placement | `image/Dockerfile` |
| episode reset, seed, dynamic service state | `setup/run.sh` |
| scoring | `verify/run.sh` and Task-local verifier code |
| path interpolation | delete; use literal absolute paths |

Both setup and verification execute inside the completed Task sandbox. Task-specific
helpers live beside the script that uses them:

```text
verify/
├── run.sh
├── verify.py
└── legacy_parser.py
```

Do not extract a Domain Kit. If a helper is broadly useful and stable across unrelated
Tasks, propose it separately for `ale_verify`; otherwise local duplication is cheaper than
a cross-Task runtime dependency.

## 4. Reduce setup to dynamic work

Move all deterministic installation and download out of setup. Keep only work that changes
per episode:

- reset or seed writable state;
- mint a secret or choose a port;
- start or reset a service after that state exists;
- wait for readiness;
- create writable copies from baked data.

Setup runs as root with open egress. That capability is for trusted dynamic initialization,
not for rebuilding the same image every run.

## 5. Replace path placeholders

Write sandbox paths literally:

| Legacy placeholder | Target |
|---|---|
| `input_dir`, `software_dir`, `task_dir` | path chosen in the Dockerfile |
| `remote_output_dir` | explicit artifact path such as `/home/user/output` |
| Python wrapper/interpreter path | `python3` where the protocol requires it |
| true difficulty parameter | `${name}` from `params` or a variant |
| runtime-generated URL or secret | setup writes a file; instruction tells the solver to read it |

Strict rendering rejects undeclared placeholders and unused parameters.

## 6. Narrow variants

The top-level manifest is always `base`. Additional variants may change only:

- `params`;
- `resources`;
- `timeouts`.

If a legacy variant changes software, input corpus, setup, verification, tools, network
policy, or expected outputs, rebuild it as a separate Task folder with its own `name`.

## 7. Rebuild verification faithfully

Verification should inspect requested final output or state, not the legacy agent's path.
Move deterministic checks into Task-local Python and use `ale_verify` primitives where
appropriate.

Avoid full gold-file comparison when the prompt requested only a subset of properties.
Preserve infrastructure errors as failures rather than returning zero.

Keep shared verification unless the verifier needs a clean image or must receive only
declared solver evidence. For separate mode, declare explicit verifier resources and
choose solver-image reuse, Task-local `verify/Dockerfile` with explicit nested kind, or a
structured external `verify.image` ref. Declare
every required regular file/directory artifact by its exact absolute path; ALE restores it
there without rerunning setup.

Move legacy Task Skills and MCP below `tools/skills/` and `tools/mcp/`. A stdio MCP server
implementation may live beside its descriptor and use `{mcp}` in command arguments; do
not duplicate it into the solver Docker context merely to make it executable.

## 8. Implement and run the oracle

The oracle runs under the same unprivileged account as a real solver. Validation executes:

1. untouched setup followed by verification;
2. setup, oracle, then verification.

Untouched verification must complete with a non-empty all-zero reward map. The oracle must
complete with matching reward names. Its actual reward values are recorded; partial credit
is visible rather than converted to success or hidden as failure.

## Migration checklist

```bash
# Feature 006 target commands, run from the ALE engine checkout.
uv run ale lint ../ale-tasks-cli/tasks/<task>
uv run ale prepare ../ale-tasks-cli/tasks/<task>
uv run ale validate ../ale-tasks-cli/tasks/<task>
```

- explicit `name` in `task.yaml`;
- explicit `image.kind` and either local `image/Dockerfile` or `image.ref`;
- small solver input under `image/`;
- large solver input in ignored `image/assets/`, copied with ordinary Docker `COPY`;
- all gold material under `verify/` or ignored `verify/assets/`;
- setup contains only dynamic initialization;
- verifier code is Task-local;
- literal absolute paths replace legacy path derivation;
- variants are only parameter/resource/timeout changes;
- untouched validation is zero and oracle validation completes;
- one real solver trajectory is audited without fitting reward to that solver.

The rebuilt Task's identity and reproducibility come from its explicit name, source digest
excluding the four exact asset roots, one asset repository commit/dirty observation, and
the exact prepared and Provider-observed image content. RunLock remains schema 2 during
pre-release development; incompatible earlier local records are not resume evidence.
