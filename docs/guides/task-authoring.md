# Writing a standard Task

A standard Task is one self-contained folder for the container or VM
`setup -> agent -> verify` protocol. Read
[task-design.md](../specs/task-design.md) before authoring and use
[task-folder.md](../specs/task-folder.md) as the normative format reference.

The conventional verifier program is `verify/verify.py`; `verify/run.sh` invokes it
with a relative path from the staged verification directory.

## Start from one folder

Run authoring commands from the ALE engine checkout. A Task repository does not install
its own copy of `ale-run`.

```bash
uv run ale new-task ../ale-tasks-cli/tasks/my_first
uv run ale lint ../ale-tasks-cli/tasks/my_first
uv run ale prepare ../ale-tasks-cli/tasks/my_first
uv run ale validate ../ale-tasks-cli/tasks/my_first
```

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
Use either `image/Dockerfile` or `image.ref`. Do not create `domain.yaml`, repository `kits/`,
repository images, Task `files/`, or top-level `skills/` and `mcp/`.

## Manifest

```yaml
spec_type: core/v1
name: my-first
environment: standard
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

Large files live at the exact path where Task code uses them:

```text
image/assets/...
setup/assets/...
verify/assets/...
oracle/assets/...
```

These directories are ignored by Git. Every Task repository maps to a same-named
Hugging Face dataset in the configured collection. Synchronization is explicit and may
select one Task, a repository, overlapping paths, or multiple repositories:

```bash
export ALE_ASSETS_COLLECTION='<owner>/<collection>'
uv run ale assets pull ../ale-tasks-cli
uv run ale assets pull --force ../ale-tasks-cli/tasks/a ../ale-tasks-extra
uv run ale assets status ../ale-tasks-cli
uv run ale assets push ../ale-tasks-cli/tasks/my_first
```

Pull restores those four roots directly into Task folders. Push uploads only their
contents and mirrors deletions below selected roots. Runtime never pulls assets. Local
state is one repository-local `.ale-cache/assets.json` marker; public provenance is only
repository name, Task path, remote commit, and `dirty`. No asset bytes are hashed during
normal status or execution. A dirty or never-synchronized Task may run for debugging but
cannot resume, deduplicate, or back a reportable result.

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

## Resources and network

`resources` requests enforced CPU, memory, optional writable storage, NVIDIA GPU count,
and solver sudo. Tasks never choose physical GPU indices. A Provider either admits the
whole request or fails explicitly.

`network.mode` is `block`, `allowlist`, or `open`; `allowed_hosts` is required only for
`allowlist`. It governs solver/oracle traffic. Setup and verification are trusted phases
with open egress. The image kind routes each physical sandbox to the matching configured
Provider. Tasks do not name a Provider implementation.

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

## Oracle and validation

The oracle performs the requested work as the same unprivileged identity as a solver.
`ale validate` runs independent untouched and oracle episodes:

- untouched must complete with a non-empty all-zero reward map;
- oracle must complete with the same reward names;
- arbitrary oracle values are recorded; non-one values are warnings, not fabricated full
  credit;
- any image, setup, artifact, verifier, Judge, timeout, or infrastructure failure remains
  a failure.

After validation, run a real agent and use its trajectory to discover ambiguity, missing
context, and verifier blind spots. Never fit reward to that agent's particular path.

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

## Submission checklist

- The folder is independently lintable and has no repository runtime dependency.
- Stable software and solver-visible state are in `image/`; setup is dynamic only.
- Large data lives only in the stage's ignored `assets/` directory.
- Verification material is absent until Harness cleanup.
- Prompt, context, artifacts, and reward describe the same result.
- Verification judges final state rather than one completion path.
- Variants change only params, resources, and timeouts.
- Untouched validation is all-zero and oracle validation completes with matching names.
- Every failure remains an explicit failure rather than a synthetic zero.
