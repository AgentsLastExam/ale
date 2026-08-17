# Writing a standard Task

A standard Task is one self-contained folder implementing the
[Task quality standard](task-quality-standard.md) for a container or VM Sandbox. This document
is the normative authoring and folder contract.

The author-facing protocol is intentionally small: ALE prepares the image, provisions a Sandbox,
runs optional trusted setup, gives the agent or oracle autonomous control, captures declared
artifacts, runs trusted verification, and then tears down or retains the Sandbox. Task authors
need the phase boundaries below, not the engine's internal orchestration. The complete engine
flow remains specified in [Standard Environment](standard-environment.md).

## Task folder

```text
tasks/my_first/
├── task.yaml
├── instruction.md
├── image/
│   ├── Dockerfile
│   └── assets/                 # optional, ignored by Git
├── setup/                      # optional
│   ├── run.sh
│   └── assets/                 # optional, ignored by Git
├── verify/
│   ├── run.sh
│   ├── verify.py
│   ├── Dockerfile              # optional separate-verifier image
│   └── assets/                 # optional, ignored by Git
├── oracle/
│   ├── run.sh
│   └── assets/                 # optional, ignored by Git
└── tools/                      # optional
    ├── skills/<name>/SKILL.md
    └── mcp/<server>.toml
```

Required files are `task.yaml`, `instruction.md`, `verify/run.sh`, and `oracle/run.sh`.
Verifier code conventionally lives at `verify/verify.py`.
Use either `image/Dockerfile` or `image.ref`. Do not create `domain.yaml`, repository `kits/`,
repository images, Task `files/`, or top-level `skills/` and `mcp/`.

## Manifest

`task.yaml` is strict. Its complete top-level field set is:

| Field | Rule |
|---|---|
| `spec_type` | required literal `core/v1` |
| `name` | required stable, collection-unique Task ID |
| `image` | required `kind`; optional `ref` |
| `environment` | optional literal `core/standard` |
| `resources` | solver CPU, memory, storage, GPU, and sudo request |
| `network` | solver/oracle network policy |
| `timeouts` | setup, agent, and verify deadlines |
| `artifacts` | absolute, unique, non-overlapping solver output paths |
| `tools` | Task-owned Skill and MCP declarations |
| `params` | instruction template inputs |
| `verify` | shared or separate verification placement |
| `variants` | bounded parameter/resource/timeout instances |
| `metadata` | descriptive data with no standard runtime behavior |
| `extras` | namespaced extensions with no standard runtime behavior |

```yaml
spec_type: core/v1
name: my-first
environment: core/standard
image: {kind: container}

resources:
  cpus: 1
  memory_mb: 1024
  storage_mb: null
  gpus: 0
  sudo: false

network: {mode: block}
timeouts: {setup: 120, agent: 900, verify: 300}

artifacts:
  - /home/user/output

tools:
  skills:
    - {path: tools/skills/reviewer}
  mcp_servers:
    - {path: tools/mcp/local-search.toml}

params: {count: 3}
metadata: {tags: [cli]}
extras: {}

variants:
  - name: hard
    params: {count: 10}
    resources: {cpus: 2, memory_mb: 4096}
    timeouts: {agent: 1800}
```

`spec_type`, `name`, and `image.kind` are required. `name` is stable and independent of the folder
path. Unknown fields fail. The top level always defines `base`; variants can contain
only `name`, `params`, `resources`, and `timeouts`. A change to image, setup,
verification, artifacts, network, tools, metadata, or expected outcome is another Task.

Selectors are explicit:

```bash
uv run ale run ../tasks/my_first                 # base only
uv run ale run ../tasks/my_first@hard            # hard only
uv run ale run '../tasks/my_first@{base,hard}'    # ordered selection
```

The same selection rules apply to `validate`.

## Choose and prepare the image

Use `image: {kind: container}` or `image: {kind: vm}`. If `image/Dockerfile` exists it
always wins and a build failure never falls back. Without it, declare a registry ref:

```yaml
image: {kind: container, ref: ghcr.io/example/task:1}
```

`ale prepare` reports the selected source and immutable build/ref/materialization stages
without constructing an Environment, agent, verifier, or sandbox.

`image/` is the sole Docker build context. The final stage starts directly from an ALE
CLI, GUI, or VM GUI base matching the declared kind and installs all stable Task-specific software and state.

```dockerfile
FROM ghcr.io/agentslastexam/sandbox-base-cli:latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY assets/corpus.json /home/user/input/corpus.json
RUN mkdir -p /home/user/output \
    && chown -R user:user /home/user/input /home/user/output
```

ALE first builds a final OCI image. For `vm`, the engine materializes that OCI rootfs into
a bootable qcow2; Tasks still author only a normal Dockerfile ending in
`sandbox-base-vm-gui`. There is no Image Tree or generated BuildKit context. Ordinary `COPY` sees only `image/`, so
it cannot include setup, oracle, or verification material. Multi-stage Docker builds
are allowed; the final runtime stage still starts from an ALE base.

## Stage-local assets

Task code, scripts, manifests, and ordinary small fixtures live outside `assets/` and are tracked
by Git. Data that should be managed separately from Task source lives under the stage that uses it:

```text
image/assets/...
setup/assets/...
verify/assets/...
oracle/assets/...
```

These four directories are ignored by Git and managed by the platform. Authors only choose the
correct visibility stage and reference files from that stage's code.

Image code uses normal Docker paths:

```dockerfile
COPY assets/corpus.json /home/user/input/corpus.json
```

Setup, verify, and oracle receive their entire stage directories. Prefer relative access:

```bash
python3 helper.py assets/seed.json
```

`ALE_STAGE_DIR` remains available when a subprocess truly needs an absolute stage path.
Verifier and oracle code use ordinary relative paths such as `assets/expected.json`.
ALE does not create an `assets/` directory for a Task that has none.

## Keep setup dynamic

`setup/` is optional. It is uploaded once, runs as trusted root from the staged setup
directory, and has open egress. Use it only for work that cannot be frozen:

- reset or seed mutable episode state;
- generate per-episode values;
- initialize writable copies from baked data;
- start or reset a service after dynamic state exists;
- wait for real readiness.

Package installation, compilation, fixed downloads, service installation, static
configuration, and stable permissions belong in the Dockerfile.

## Write literal sandbox paths

The Task owns its filesystem. Put exact absolute paths in the instruction and artifact
declarations; ALE does not add a workspace prefix.

```markdown
Read `/home/user/input/orders.json` and write `/home/user/output/report.json`.
```

Use `${name}` only for declared parameters. Undeclared placeholders and unused params
are errors. Setup-created values should be written to a documented file for the solver.

Artifact paths must be absolute, unique, and non-overlapping. ALE does not pre-create
them. After Harness cleanup it captures each declared regular file or directory as
immutable solver evidence; missing paths, symlinks, special files, and transfer failures
are explicit errors.

Artifact collection is Run configuration, not a Task field. `artifacts.collect = "host"`
captures declared artifacts; `"none"` does not inspect, copy, retain, or restore them. A separate
verifier that needs solver outputs therefore requires host collection.

## Resources and network

`resources` requests enforced CPU, memory, optional writable storage, NVIDIA GPU count,
and solver sudo. Tasks never choose physical GPU indices. A Provider either admits the
whole request or fails explicitly.

`network.mode` is `block`, `allowlist`, or `open`; `allowed_hosts` is required only for
`allowlist`. It governs solver/oracle traffic. Author for `block` by default: install stable
software and services in the image and bake or stage every required input. Use `allowlist` only
for intrinsic external dependencies that cannot be made local. Use `open` only when a restricted
network would materially change the capability being evaluated, and explain why in
`metadata.network_justification`.

Setup and verification are trusted phases with open egress. The image kind routes each physical
Sandbox to the matching configured Provider. Tasks do not name a Provider implementation.

## Skills and MCP

Task-owned agent resources live only below `tools/` and are declared relative to the
Task root. Run/preset resources may still be layered by the operator.

```toml
schema_version = 1
name = "local-search"
transport = "stdio"
command = "python3"
args = ["{mcp}/server.py"]
```

The descriptor and adjacent server code are staged together under the agent home;
`{mcp}` resolves to that server's staged directory. Streamable HTTP MCP is also supported
when its host satisfies Task network policy. Task resources do not select a Harness and
never inherit ambient host Skills, MCP, or credentials.

## Verification topology

Shared verification is the default and needs no manifest block. It runs in the completed
solver sandbox after Harness cleanup and evidence capture.

```yaml
verify:
  environment_mode: separate
  resources:
    cpus: 1
    memory_mb: 1024
    storage_mb: null
    gpus: 0
```

Separate verification starts an independent sandbox, does not rerun setup, and restores
only declared artifacts to their exact absolute paths. Its resources are mandatory and
independent. If `verify/Dockerfile` exists, declare `verify.image.kind`; ALE builds it with
`verify/` as context. Without a verifier Dockerfile or image, it reuses the solver image.
A dedicated VM ref uses `verify.image: {kind: vm, ref: ghcr.io/example/verifier-vm:1}`.
Container uses the same shape with `kind: container`. Local Dockerfile wins
over a simultaneously authored ref. The
same `verify/run.sh` and `verify.py` work in both modes.

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 verify.py
```

```python
from ale_verify import Verification, checks

verification = Verification()
verification.check("format", checks.file_exists("/home/user/output/report.json"))
verification.stat("files_checked", 1)
verification.aggregate("overall")
verification.write()
```

`check()` and `judge()` add reward criteria, `stat()` adds diagnostics,
`aggregate()` defaults to the weighted mean of stored criteria, and `write()` finalizes
one canonical `verification.json` plus the reward envelope. LLM and Agent Judges call
their configured endpoints directly from the verification sandbox. Task code owns prompt,
rubric, evidence, and invocation-local MCP; run TOML owns model, endpoint, credential
environment name, reasoning effort, Agent adapter, and exact CLI version. Judge failures
are infrastructure failures, never zero rewards.

## Oracle

The oracle performs the requested work as the same unprivileged identity as a solver.
An untouched episode must produce a non-empty all-zero reward map. An oracle episode must produce
the same reward names and full credit for every required outcome. Image, setup, artifact,
verifier, Judge, timeout, and infrastructure failures remain failures rather than synthetic
scores.

## Debug retention

Sandbox retention is operator-owned run configuration, never a Task or variant field:

```toml
[sandbox_retention]
solver = "keep"
verifier = "destroy"
```

Both default to `destroy`. Shared mode has one physical sandbox and keeps it if either
role requests keep. Separate mode applies policies independently. Retained container and
QEMU VM sandboxes are sanitized, recorded with actionable handles, and managed with:

```bash
uv run ale sandbox list
uv run ale sandbox destroy HANDLE
```

`HANDLE` is emitted as `docker:<runtime-id>` or `qemu:<runtime-id>`.
