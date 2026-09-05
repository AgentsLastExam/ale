# Writing a standard Task

A Task is one self-contained folder implementing the
[Task quality standard](task-quality-standard.md). ALE creates the declared initial sandbox,
lets an agent work autonomously, and verifies the resulting outcome.

## Task folder

```text
tasks/my_task/
├── task.yaml                    # Task configuration
├── instruction.md              # instruction shown to the evaluated agent
├── image/
│   ├── Dockerfile              # reproducible initial environment
│   ├── install.sh              # optional image-build helper
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
declared OS: `run.sh` for Linux or `run.ps1` for Windows. The Task must use either
`image/Dockerfile` or `image.ref`.

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
  # ref: registry.example/image:tag # required only when image/Dockerfile is absent

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

Request only what the Task needs. `image.kind` selects container or VM behavior; Tasks never name
a Provider. For `network.mode: block`, first trace a normal solver-feasible completion route, then
prepare all data, software, and local services required by that route in the initial environment.
Do not rely on the solver downloading packages at runtime. Use `allowlist` for intrinsic external
dependencies and `open` only when restriction would materially change the capability being
evaluated; explain that choice in `metadata.network_justification`.

## image/

The image is the reproducible initial computer state supplied to the agent. Start from the ALE GUI
base matching `image.kind`, then add every stable dependency, service, file, permission, and piece
of initial state required by the Task:

- container: `ghcr.io/agentslastexam/container-ubuntu22-base:latest` (Ubuntu 22.04);
- VM: `ghcr.io/agentslastexam/vm-ubuntu24-base:0.1.0` (Ubuntu 24.04).

Windows VM Tasks declare `os: windows` and currently use a private prebuilt qcow2
reference. ALE does not publish licensed Windows image bytes or offer a general Windows
image builder.

Develop it in two passes:

1. **Explore.** Start the matching base sandbox, construct and inspect the required environment
   interactively, and preserve a snapshot as the reference state.
2. **Reproduce.** Encode those changes in `image/Dockerfile` and image-local scripts, build from a
   clean base, and compare the result with the reference snapshot until the required state matches.

The snapshot is an authoring aid, not Task source. The submitted initial environment must be
reproducible from `image/`; large build inputs belong in `image/assets/` and are copied by the
Dockerfile. Prefer image-local scripts when they make non-trivial setup easier to test and maintain.

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest

COPY install.sh /tmp/install.sh
RUN /tmp/install.sh && rm /tmp/install.sh
COPY assets/corpus.json /home/user/input/corpus.json
```

For a VM Task, use the VM base as the final stage. ALE converts that OCI image into the bootable VM;
Task code does not implement boot or disk assembly.

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

Describe the required outcome and constraints, not a preferred implementation path. Any input or
output location needed to complete the Task must agree with the initial environment and
`task.yaml`. Declared `${name}` placeholders are rendered from `params`.
Use literal sandbox paths: `/home/user/...` on Linux and `C:\Users\user\...` on
Windows. ALE does not add or rewrite a workspace prefix.

## tools/

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server. Put Task-owned resources below `tools/` and declare them
in `task.yaml`; never depend on ambient Host configuration.

## verify/

`verify/run.sh` (or Windows `verify/run.ps1`) evaluates the final outcome and writes the reward map. Verifier implementation
conventionally lives in `verify/verify.py`; private references belong in `verify/assets/`. Use the
APIs and scoring behavior defined in [Verification](verification.md) rather than duplicating them
here.

## oracle/

`oracle/run.sh` (or Windows `oracle/run.ps1`) checks that successful final state can receive credit. Prefer an executable
solution that performs the Task normally. When that is not practical, the oracle may instead use
protected reference data from `oracle/assets/` to write or upload a full-credit final state
directly. That reference must never be visible to the evaluated agent.

An untouched run must receive a non-empty all-zero reward map. Every oracle reward must equal one;
lower oracle credit fails validation.

## Debugging

Retain a sandbox when its final state needs inspection. ALE lists and destroys retained container
and VM sandboxes with `ale sandbox list` and `ale sandbox destroy HANDLE`.
