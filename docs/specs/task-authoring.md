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
or uses `image.ref`. Windows builds `image/run.ps1` on `image.base_ref`, or directly uses
`image.ref` without an image script. `ref` and `base_ref` are mutually exclusive.

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
  # ref: registry.example/image:tag # use an existing image directly

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
artifact_excludes:                  # optional exact paths relative to each directory artifact
  - .agent_runtime_env              # Linux container directory snapshots only
# artifact_include_files:           # alternative to excludes; exact file paths only
#   - silicon/bandstructure.dat     # relative to each directory artifact
# artifact_include_directories:     # optionally capture entire subtrees as well
#   - input                         # relative to each directory artifact
# artifact_preserve_file_directories: true  # preserve selected files' directory type
# artifact_omit_unreadable_files: true      # separate verification only

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
`artifact_excludes` omits exact relative files or subtrees from every directory snapshot;
paths must be normalized, unique and non-overlapping. Use exclusions only for content that
verification does not need. They are available for Linux container Tasks only. Separate
verification receives the filtered snapshot, while shared verification sees the original
sandbox filesystem. The exclusions are recorded with each directory in `snapshot.json`.

Alternatively, `artifact_include_files` captures only the listed normalized, unique,
non-overlapping paths relative to each directory artifact. List every file the verifier
needs; an absent or non-regular path is omitted by default. With
`artifact_preserve_file_directories: true`, a selected file path resolving to a directory
is preserved as an empty directory without traversing its contents; use this only when
the verifier needs the type but not the directory contents. This option requires
`artifact_include_files`. ALE reads selected files inside the solver sandbox as the agent account,
using ordinary filesystem semantics, including following symlinks, and snapshots regular
bytes rather than links. Unreadable files fail capture explicitly by default.
With separate verification, `artifact_omit_unreadable_files: true` omits selected
files when the agent cannot access them and records their paths in `snapshot.json`.
Use this only when the verifier should treat an unreadable output as missing. It
does not apply to selected subtrees or suppress other capture errors.
Unlisted paths are not traversed. This is useful for outputs
that contain arbitrary runtime links or bulky intermediates. It is available for Linux
container Tasks only, cannot be combined with `artifact_excludes`, and each selected
artifact must be a directory when it exists. If the artifact itself is absent, its
snapshot records `kind: missing`; separate verification removes the corresponding
path so the verifier sees it as absent. A missing artifact remains an error without
file or directory selection. The selected paths are recorded in `snapshot.json`.

`artifact_include_directories` adds whole selected subtrees to this file selection.
Paths in both lists must be normalized, unique and non-overlapping. ALE follows links
within each selected subtree as the agent, snapshots file contents rather than links,
and preserves empty directories. A missing path or a path that is not a directory is
omitted; an unreadable file, dangling link or special file inside a selected subtree
fails capture. This selection can be used without `artifact_include_files`, follows
the same Linux container and exclusion restrictions, and is recorded in
`snapshot.json`.

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
base matching the Task OS and `image.kind`, then add every stable dependency, service, file,
permission, and piece of initial state required by the Task. The Linux bases are:

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

Windows image builds declare a base and provide `image/run.ps1`:

```yaml
os: windows
image:
  kind: vm
  base_ref: /path/to/windows-base.qcow2
```

The base includes WinGet and Chocolatey. Put installation and configuration in
`image/run.ps1`; Dockerfiles are unsupported on Windows. For example:

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
exit codes. Scripts must finish within one hour; nonzero exits fail the build. Suppress
installer reboots; builds requiring a mid-script restart are unsupported. ALE owns shutdown.
Build-only materials are removed before saving; copy required files to their final locations.
Only `image/` is staged.
The whole build is cached by base, builder, and all image inputs including assets.
To use a prepared image directly, declare `image.ref` and omit `image/run.ps1` and
`image.base_ref`. `base_ref` is supported only for Windows solver builds and requires
the script. Windows separate verification reuses the solver image or supplies
`verify.image.ref`.

### Software assets

On either OS, install software directly in the image recipe when a clean base can acquire the
pinned version and deploy it unattended from declared inputs. Machine authentication is allowed;
interactive login, verification codes, browser sessions and manual installer steps are not a
repeatable build input. Version pins alone do not guarantee that an upstream keeps old files.

Otherwise, acquire and, if needed, install or compile the software once in a preparation
environment. Keep the reusable offline materials as real files under
`image/assets/softwares/<software_id>/`, for example `image/assets/softwares/editor_1/`.
Use one directory per software/version, with underscores rather than nested version directories.
Reuse a compatible existing package before preparing another. Slow acquisition or compilation
can also justify an offline asset. Installed VM state alone is not a rebuildable asset.

Keep deployment scripts and Task-specific configuration in `image/`. Linux uses the Dockerfile;
Windows uses `run.ps1`. Scripts consume Task-local materials, choose their own extraction or
installation method, and declare external dependencies. Linux has no required package format.
For Windows software requiring repackaging, prefer the
[MSIX workflow](../guides/windows-task-images.md). No Task depends on another Task's files.

Record the source, version, integrity hash, compatible base, and deployment requirements. Keep
credentials out of assets. Validate from a clean base as the actual agent user, including required
writes, child tools and save/reopen, without repeating the interactive acquisition process.
The [asset contract](task-assets.md) owns synchronization and its current implementation limits.

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
