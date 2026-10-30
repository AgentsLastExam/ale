# Task authoring contract

A Task is one self-contained folder: nothing outside it affects it, so it can be reviewed,
moved, and exported on its own. This document owns its directory, configuration,
environment, data visibility, and stage interfaces. Design and acceptance standards belong to
the [Task quality standard](task-quality-standard.md); scoring and Judge APIs belong to
[Verification](verification.md).

The format is Harbor's (`task.toml` schema 1.4, `environment/`, `tests/`, `solution/`): a
field ALE implements as Harbor does keeps Harbor's name and place, and what ALE adds or means
differently lives in an `[ale]` table or a file Harbor does not read, so no Harbor name carries
another meaning. ALE runs Tasks in its own runtime; a Task need not run unmodified on Harbor.

## Task folder

```text
my-task/
├── task.toml                  # Task configuration
├── instruction.md             # instruction shown to the evaluated agent
├── environment/               # the machine the agent starts on
│   ├── Dockerfile             # container or Linux VM image; context environment/ without setup/
│   ├── build.ps1              # Windows image, on the same context
│   └── setup/                 # optional per-episode initialization
│       └── setup.sh           # setup.ps1 on Windows
├── tests/                     # verification
│   ├── test.sh                # test.ps1 on Windows
│   ├── verify.py
│   └── Dockerfile             # optional separate-verifier image (context tests/)
├── solution/                  # known-good solution for validation
│   └── solve.sh               # solve.ps1 on Windows
└── tools/                     # optional Task-requested agent resources
    ├── skills/<name>/SKILL.md
    └── mcp/<server>.toml
```

Required files are `task.toml`, `instruction.md`, and the tests and solution entries for the
Task OS: `tests/test.sh` and `solution/solve.sh` on Linux, `test.ps1` and `solve.ps1` on
Windows. `[ale].sandbox` and the OS decide what `environment/` builds. A container Task builds
`environment/Dockerfile` or uses `[environment].docker_image`. A Linux VM Task builds
`environment/Dockerfile` or uses `[ale].vm_image`. A Windows Task builds `environment/build.ps1`
on `[ale].vm_base`, or uses `[ale].vm_image` alone. Every image build has `environment/` as its
context, `setup/` excluded. A Linux image named directly wins over a build definition.

## task.toml

`task.toml` is strict. A Harbor field whose default differs from ALE's is required, so its
value never depends on which runtime reads it. An unknown key fails, and a Harbor key ALE does
not implement fails with a message saying so. The annotated shape below describes every key;
omit optional ones the Task does not use.

```toml
schema_version = "1.4"                      # required; exactly "1.4"
artifacts = ["/home/user/output", "/home/user/games/*.sgf"]  # optional outcome paths

[task]                                      # required
name = "agentslastexam/my-task"             # required org/name, unique in its collection
description = "Summarize the corpus."       # optional; descriptive
version = "1.0"                             # optional; descriptive
authors = [{ name = "Ada", email = "ada@example.org" }]  # optional; descriptive
keywords = ["document-analysis"]            # optional; descriptive

[metadata]                                  # optional free-form values; no runtime behavior
network_justification = "the Task reads one publisher's live catalog"

[environment]                               # required: the solver's machine
os = "linux"                                # optional: linux | windows; default linux
# docker_image = "registry.example/image:tag"  # a container image used directly
cpus = 1                                    # required; integer >= 1
memory_mb = 4096                            # required; integer >= 128
storage_mb = 8192                           # optional writable storage; integer >= 256
gpus = 0                                    # optional GPU count, not indices; default 0

[agent]                                     # required: the evaluated agent and the oracle
user = "user"                               # required account; must exist in the image
timeout_sec = 900                           # required positive seconds
network_mode = "allowlist"                  # required: no-network | allowlist | public
allowed_hosts = ["catalog.example.org"]     # required, and only, for allowlist

[verifier]                                  # required
timeout_sec = 300                           # required positive seconds
environment_mode = "separate"               # optional: shared | separate

[verifier.environment]                      # optional: a separate verifier's own sandbox
# docker_image = "registry.example/verifier:tag"  # a container image used directly
cpus = 1                                    # required in this table
memory_mb = 4096                            # required in this table
storage_mb = 8192                           # optional
gpus = 0                                    # optional; default 0

[ale]                                       # optional
sandbox = "container"                       # container | vm; default container
# vm_image = "/path/to/disk.qcow2"            # a VM image used directly
# vm_base = "/path/to/windows-base.qcow2"     # the base environment/build.ps1 builds on
sudo = false                                # working sudo (Administrator on Windows) for user
skills = ["tools/skills/reviewer"]          # Task Skill directories
mcp_servers = ["tools/mcp/search.toml"]     # Task MCP descriptors

# [ale.verifier]                            # optional: a separate verifier in a VM
# sandbox = "vm"
# vm_image = "/path/to/verifier.qcow2"      # used directly; without it, tests/Dockerfile
```

A Windows Task sets `os = "windows"` and `[ale].sandbox = "vm"`. `vm_image` and `vm_base` are
mutually exclusive; `vm_base` requires Windows and `environment/build.ps1`.

When the instruction requires deliverable files, every instructed destination must equal or be
contained by a declared `artifacts` path, or matched by a declared glob. Commands and examples in `instruction.md` must write to
that same destination; do not declare one path while instructing the agent to use another.

### Artifact copying

`artifacts` accepts absolute file paths, directory paths, and globs such as `*.sgf` or
`**/*.json`. ALE copies each match according to its actual type and skips links, missing
paths, and copy failures. No link is followed anywhere on an artifact's path, a directory
above it included: one the agent made could point at a file only the framework may read.
These are fixed defaults; the verifier decides how to score the resulting output. Copying uses the framework's
privileged identity on every supported OS; see [Permissions](sandbox-image.md#permissions).

Run-level `artifacts.collect` defaults to `"none"` (no copy or restore). Set it to `"host"`
to retain outputs or transfer them to a separate verifier. `run`, `validate`, and `reverify`
use the same policy. Separate-verifier images must not contain prepopulated candidate
answers; tasks that grade original links or live state should use shared verification.

### Verification topology

Verification is shared unless `environment_mode = "separate"` or `[verifier.environment]`
says otherwise, as in Harbor. Separate verification runs in an independent sandbox with the
`[verifier.environment]` resources, or the solver's without sudo when that table is absent.
Its image is the first of `[verifier.environment].docker_image` or `[ale.verifier].vm_image`,
`tests/Dockerfile` (a VM when `[ale.verifier].sandbox = "vm"`), and the solver image.
`[ale.verifier]` and `tests/Dockerfile` require separate verification. A Windows verifier
reuses the solver image or names a `vm_image`. The Verification APIs and records are defined
only in [Verification](verification.md).

### Resources and network

`[ale].sandbox` selects container or VM behavior; Tasks never name a Provider.
`[agent].network_mode` applies to the evaluated agent and the oracle: `no-network` reaches only
the Gateway, `allowlist` also the declared hosts, `public` everything. Setup and verification
network is framework-controlled; its enforcement is specified in [Security](security.md).
The quality standard owns resource feasibility and network-policy selection. A public-network
justification uses `[metadata].network_justification`.

`[environment]` resources are the whole Sandbox's allocation. The image's own processes, such
as the GUI base images' desktop, share it with the agent and every program the agent starts,
so a GUI Task leaves an agent CLI room above the desktop.

## environment/

The image is the reproducible initial computer state supplied to the agent. Start from the ALE
GUI base matching the Task OS and sandbox, then add every stable dependency, service, file,
permission, and piece of initial state required by the Task. The Linux bases are:

- container: `ghcr.io/agentslastexam/container-ubuntu22-base` (Ubuntu 22.04);
- VM: `ghcr.io/agentslastexam/vm-ubuntu24-base` (Ubuntu 24.04).

Name an exact version, such as `:0.1.1`, never `latest`; `ale new-task` writes the
engine's current version. A Task keeps building from the version it names until its
author moves it ([Sandbox image](sandbox-image.md#versions)).

The submitted initial environment must be reproducible from `environment/`; an exploratory
sandbox snapshot is not Task source. A container image builds `environment/Dockerfile` with
`environment/` as context, `setup/` excluded. Build-local scripts may implement non-trivial
installation and configuration. Build-only large inputs can use read-only BuildKit mounts;
runtime data belongs in the image.

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:0.1.1

COPY --chmod=0755 install.sh /tmp/install.sh
RUN /tmp/install.sh && rm /tmp/install.sh
COPY --chmod=0644 corpus.json /home/user/input/corpus.json
```

A Linux VM Task builds `environment/Dockerfile` the same way, with the VM base as the final
stage. ALE converts that OCI image into the bootable VM; Task code does
not implement boot or disk assembly.

A Windows Task declares a base and provides `environment/build.ps1`:

```toml
[environment]
os = "windows"

[ale]
sandbox = "vm"
vm_base = "/path/to/windows-base.qcow2"
```

The base includes WinGet and Chocolatey. Put installation and configuration in
`build.ps1`; Dockerfiles are unsupported on Windows. For example:

```powershell
$ErrorActionPreference = 'Stop'
choco install -y 7zip --no-progress
if ($LASTEXITCODE -ne 0) { throw "7zip installation failed: $LASTEXITCODE" }
Copy-Item .\input.txt "$env:ALE_HOME\input.txt"
```

`ale prepare TASK` copies `environment/`, less `setup/`, into a temporary Windows VM, runs
`build.ps1` with that directory as cwd and `ALE_HOME` set to the agent home, installs ALE's
current guestd and Cua Driver, then saves the configured qcow2. Scripts may use WinGet,
Chocolatey, or local unattended installers; pin package versions where supported and check
native command exit codes. Scripts must finish within one hour; nonzero exits fail the build. Suppress
installer reboots; builds requiring a mid-script restart are unsupported. ALE owns shutdown.
Build-only materials are removed before saving; copy required files to their final locations.
Only that context is staged, and the whole build is cached by base, builder, and every file
in it. A prepared image used directly is named by `[ale].vm_image` without `build.ps1`.

## environment/setup/

`environment/setup/` is optional framework-controlled initialization executed after the
sandbox starts. It is never part of an image build. Use it only for state that must vary by
episode: reset mutable data, generate per-episode values, create writable copies, or start
services that depend on dynamic state. Stable installation and configuration belong in the
image.

Setup and verification use the framework's privileged identity; solver and oracle use
the agent identity. See [Permissions](sandbox-image.md#permissions). Image/setup code must
explicitly set required ownership, write and execute permissions instead of relying on Host modes.

## instruction.md

`instruction.md` is the evaluated-agent instruction, used verbatim. Its input and output paths
refer to the declared initial environment and artifacts. Use literal sandbox paths:
`/home/user/...` on Linux and `C:\Users\user\...` on Windows. ALE does not add or rewrite a
workspace prefix.

## tools/

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server. Put Task-owned resources below `tools/skills/` and
`tools/mcp/` and declare them in `[ale].skills` and `[ale].mcp_servers`; never depend on
ambient Host configuration. ALE uploads them from the Task folder, while Harbor's
`skills_dir` and `mcp_servers` describe resources already in the image.

## tests/

`tests/test.sh` (or Windows `tests/test.ps1`) evaluates the final outcome and writes its
`reward` to `/logs/verifier/reward.json` (Windows `C:\logs\verifier\reward.json`). Verifier
implementation conventionally lives in `tests/verify.py`. ALE uploads the whole directory to
its root-only stage directory, not to Harbor's `/tests`, and runs the entry with that
directory as cwd. Stage code uses ordinary relative paths; solver outputs use literal
absolute paths. The verification image must expose system Python 3.12
or newer. Before each tests entrypoint starts, ALE selects an existing compatible Python,
stages `ale_verify` and its schema validator into that interpreter's site-packages, checks
the import, then sets `ALE_VERIFY_PYTHON` in the entrypoint's process environment.
It is available to the tests and their child processes, including during `validate` and
`reverify`; it is not initialized by the image build, setup stage, or SDK import.
ALE does not install Python or replace the solver's default interpreter.

The Linux entry is:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$ALE_VERIFY_PYTHON" verify.py
```

The Windows entry is:

```powershell
& $env:ALE_VERIFY_PYTHON .\verify.py
exit $LASTEXITCODE
```

APIs, scoring, and record behavior are defined in [Verification](verification.md).

## solution/

`solution/solve.sh` (or Windows `solution/solve.ps1`) supplies a successful final state for
validation. It may execute a solution normally or write that state directly from reference
data it carries. The solution is absent during evaluated-agent execution.

Validation's untouched and oracle reward requirements are defined in
[the episode protocol](episode-protocol.md#validation).
