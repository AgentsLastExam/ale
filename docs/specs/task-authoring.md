# Task authoring contract

A Task is one self-contained folder. This document owns its directory, configuration,
environment, data visibility, and stage interfaces. Design and acceptance standards belong to
the [Task quality standard](task-quality-standard.md); scoring and Judge APIs belong to
[Verification](verification.md).

## Task folder

```text
tasks/my_task/
├── task.yaml                    # Task configuration
├── instruction.md              # instruction shown to the evaluated agent
├── image/
│   ├── Dockerfile              # Linux; run.ps1 on Windows
│   ├── install.sh              # optional image-build helper (install.ps1 on Windows)
│   └── assets/                 # optional separately managed image data
├── setup/                       # optional per-episode initialization
│   ├── run.sh                   # Linux; run.ps1 on Windows
│   └── assets/
├── verify/
│   ├── run.sh                   # Linux; run.ps1 on Windows
│   ├── verify.py
│   ├── Dockerfile              # optional separate-verifier image
│   └── assets/
├── oracle/
│   ├── run.sh                   # Linux; run.ps1 on Windows
│   └── assets/
└── tools/                       # optional Task-requested agent resources
    ├── skills/<name>/SKILL.md
    └── mcp/<server>.toml
```

Required files are `task.yaml`, `instruction.md`, and the verify/oracle entry for the
declared OS: `run.sh` for Linux or `run.ps1` for Windows. Linux builds `image/Dockerfile`
or uses `image.ref`. Windows builds `image/run.ps1` on `image.ref`, or uses the ref directly
when no image script exists.

Task source, scripts, manifests, and ordinary small fixtures are tracked by Git. `assets/` is for
data managed separately from source because of its size or lifecycle. Assets live under the stage
that consumes them:

- `image/assets/` is available while building the initial environment and becomes agent-visible
  only when image code places it there;
- `setup/assets/` is available only to setup;
- `verify/assets/` is available only to verification;
- `oracle/assets/` is available only to the oracle.

These four directories are ignored by Git and managed by the platform. Task code references their
contents through normal stage-relative paths such as `assets/reference.json`.
PowerShell stages use the same convention, for example
`python.exe .\helper.py .\assets\seed.json`. `ALE_STAGE_DIR` remains available when a
subprocess requires an absolute stage path.

## task.yaml

`task.yaml` is strict: unknown fields fail. The annotated shape below describes every field and
its nesting; omit optional sections that the Task does not use.

```yaml
spec_type: core/v1                 # required; current Task schema
name: my-task                      # required stable ID, unique in its Task collection
environment: core/standard         # optional; this is the only supported Environment
os: linux                           # optional: linux | windows; default: linux

image:                              # required initial sandbox image
  kind: container                   # required: container | vm
  # ref: registry.example/image:tag # required without Dockerfile; always required on Windows

resources:                          # optional solver allocation; defaults shown
  cpus: 1                           # integer >= 1
  memory_mb: 1024                   # integer >= 128
  storage_mb: null                  # null or integer >= 256; writable storage request
  gpus: 0                           # integer >= 0; GPU count, not physical indices
  sudo: false                       # whether the evaluated agent receives sudo

network:                            # optional evaluated-agent/oracle network policy
  mode: block                       # block | allowlist | open; default: block
  allowed_hosts: []                 # required and non-empty only for allowlist

timeouts:                           # optional positive seconds; defaults shown
  setup: 120
  agent: 900
  verify: 300

artifacts:                          # optional outcome paths captured from the sandbox
  - /home/user/output               # absolute, unique, and non-overlapping

tools:                              # optional; use only when the Task explicitly requires them
  skills:
    - path: tools/skills/reviewer   # Task-relative Skill directory
  mcp_servers:
    - path: tools/mcp/search.toml   # Task-relative MCP descriptor

params:                             # optional instruction template values
  count: 3                          # instruction.md may reference this as ${count}

verify:                             # optional; shared verification is the default
  environment_mode: separate        # shared | separate
  image:                            # optional separate-verifier image
    kind: container                 # container | vm
    ref: registry.example/verifier:tag # omit ref when verify/Dockerfile exists
  resources:                        # required for separate verification
    cpus: 1
    memory_mb: 1024
    storage_mb: null
    gpus: 0

variants:                           # optional named instances of this same Task
  - name: hard                      # unique; "base" is reserved
    params: {count: 10}             # may override only params...
    resources: {cpus: 2}            # ...solver resources...
    timeouts: {agent: 1800}         # ...and timeouts

metadata:                           # optional descriptive values; no runtime behavior
  tags: [document-analysis]
  # network_justification: explain why open network is necessary

extras:                             # optional namespaced extensions; no standard behavior
  example.org: {difficulty: hard}
```

When the instruction requires deliverable files, every instructed destination must equal or be
contained by a declared `artifacts` path. Commands and examples in `instruction.md` must write to
that same destination; do not declare one path while instructing the agent to use another.

### Verification topology

For shared verification, omit `verify` or set only `environment_mode: shared`. Separate
verification runs in an independent sandbox, requires explicit resources, and may reuse the
solver image, use `verify/Dockerfile`, or use `verify.image.ref`. The Verification APIs and
records are defined only in [Verification](verification.md).

### Variants

Variants represent parameter, resource, or timeout changes to the same expected outcome. A change
to image, setup, verification, network, artifacts, or tools is a different Task.

### Resources and network

`image.kind` selects container or VM behavior; Tasks never name a Provider. `network` applies to
the evaluated agent and oracle; its enforcement is specified in [Security](security.md).
The quality standard owns resource feasibility and network-policy selection. An open-network
justification uses `metadata.network_justification`.

## image/

The image is the reproducible initial computer state supplied to the agent. Start from the ALE GUI
base matching `image.kind`, then add every stable dependency, service, file, permission, and piece
of initial state required by the Task:

- container: `ghcr.io/agentslastexam/container-ubuntu22-base:latest` (Ubuntu 22.04);
- VM: `ghcr.io/agentslastexam/vm-ubuntu24-base:0.1.0` (Ubuntu 24.04).

The submitted initial environment must be reproducible from `image/`; an exploratory sandbox
snapshot is not Task source. Large build inputs belong in `image/assets/` and are copied by the
image build. Image-local scripts may implement non-trivial installation and configuration.

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest

COPY install.sh /tmp/install.sh
RUN /tmp/install.sh && rm /tmp/install.sh
COPY assets/corpus.json /home/user/input/corpus.json
```

For a Linux VM Task, use the VM base as the final stage. ALE converts that OCI image into the bootable VM;
Task code does not implement boot or disk assembly.

Windows Tasks declare `os: windows`, `image.kind: vm`, and `image.ref` pointing to the
private Windows base. Put installation and configuration in `image/run.ps1`; Dockerfiles
are unsupported on Windows. The base includes WinGet and Chocolatey. For example:

```powershell
$ErrorActionPreference = 'Stop'
choco install -y 7zip --no-progress
if ($LASTEXITCODE -ne 0) { throw "7zip installation failed: $LASTEXITCODE" }
Copy-Item .\assets\input.txt "$env:ALE_HOME\input.txt"
```

`ale prepare TASK` copies `image/` into a temporary Windows VM, runs `run.ps1` with that
directory as cwd and `ALE_HOME` set to the agent home, then saves the configured qcow2.
Scripts may use WinGet, Chocolatey, or
local unattended installers; pin package versions where supported and check native command
exit codes. Scripts must finish within 30 minutes; nonzero exits fail the build. Suppress
installer reboots; builds requiring a mid-script restart are unsupported. ALE owns shutdown.
Build-only materials are removed before saving; copy required files to their final locations.
Only `image/` is staged.
The whole build is cached by base, builder, and all image inputs including assets.
Without `image/run.ps1`, the ref is used directly. Windows separate verification reuses
the solver image or supplies `verify.image.ref`.

## setup/

`setup/` is optional framework-controlled initialization executed after the sandbox starts. Use it only for
state that must vary by episode: reset mutable data, generate per-episode values, create writable
copies, or start services that depend on dynamic state. Stable installation and configuration
belong in `image/`.

On Linux, `setup/run.sh` executes as `root`, while both the evaluated solver and the validation
oracle execute as the image's agent account (`user` in the ALE base images). `verify/run.sh`
executes as `root`. ALE does not change ownership of Task-created paths. If image or setup code
creates a directory or file that the solver or oracle must modify, it must explicitly grant that
account the required ownership or permissions, for example with `chown` or `install -o user`.

## instruction.md

`instruction.md` is the evaluated-agent instruction. Its input and output paths refer to the
declared initial environment and artifacts. Declared `${name}` placeholders are rendered from `params`.
Use literal sandbox paths: `/home/user/...` on Linux and `C:\Users\user\...` on
Windows. ALE does not add or rewrite a workspace prefix.

## tools/

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server. Put Task-owned resources below `tools/` and declare them
in `task.yaml`; never depend on ambient Host configuration.

## verify/

`verify/run.sh` (or Windows `verify/run.ps1`) evaluates the final outcome and writes the reward map. Verifier implementation
conventionally lives in `verify/verify.py`; private references belong in `verify/assets/`.
ALE runs the stage with `verify/` as cwd. Stage code and assets use ordinary relative paths;
solver outputs use literal absolute paths. The verification image must expose system Python 3.12
or newer; ALE stages the `ale_verify` package into that interpreter's site-packages.

The Linux entry is:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 verify.py
```

The Windows entry is:

```powershell
python.exe .\verify.py
```

APIs, scoring, and record behavior are defined in [Verification](verification.md).

## oracle/

`oracle/run.sh` (or Windows `oracle/run.ps1`) supplies a successful final state for validation.
It may execute a solution normally or use protected reference data from `oracle/assets/` to
write or upload that state directly. Oracle assets are absent during evaluated-agent execution.

Validation's untouched and oracle reward requirements are defined in
[StandardEnvironment](standard-environment.md#validation).
