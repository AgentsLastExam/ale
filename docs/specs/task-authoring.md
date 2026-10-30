# Task authoring contract

A Task is one self-contained folder: nothing outside it affects it, so it can be reviewed,
moved, and exported on its own. This document owns its directory, configuration,
environment, data visibility, and stage interfaces. Design and acceptance standards belong to
the [Task quality standard](task-quality-standard.md); scoring and Judge APIs belong to
[Verification](verification.md).

The format is Harbor's (`task.toml` schema 1.4, `environment/`, `tests/`, `solution/`): a
field ALE implements as Harbor does keeps Harbor's name, table and meaning. A field ALE adds
sits in the Harbor table of what it configures, under a name Harbor gives no other meaning: a
separate namespace would not stop one name meaning two things, and would have to mirror every
table it extends. ALE runs Tasks in its own runtime and a Task reaches Harbor through a
conversion, so `schema_version = "1.4+ale.1"` marks the file as ALE's.

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
└── solution/                  # known-good solution for validation
    └── solve.sh               # solve.ps1 on Windows
```

Required files are `task.toml`, `instruction.md`, and the tests and solution entries for the
Task OS: `tests/test.sh` and `solution/solve.sh` on Linux, `test.ps1` and `solve.ps1` on
Windows. `[environment]` decides what `environment/` builds. A container Task builds
`environment/Dockerfile` or uses `docker_image`. A Linux VM Task sets `vm = true` to build
`environment/Dockerfile`, or uses `vm_image`. A Windows Task builds `environment/build.ps1` on
`vm_base`, or uses `vm_image` alone. Every image build has `environment/` as its context,
`setup/` excluded. A Linux image named directly wins over a build definition.

## task.toml

`task.toml` is strict. A Harbor field whose default differs from ALE's is required, so its
value never depends on which runtime reads it. An unknown key fails, and a Harbor key ALE does
not implement fails with a message saying so. The annotated shape below describes every key,
with `ALE` marking the ones Harbor does not have; omit optional ones the Task does not use.

```toml
schema_version = "1.4+ale.1"                # required; exactly this
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
vm = false                                  # ALE; optional: a Linux VM built from a Dockerfile
# docker_image = "registry.example/image:tag"  # a container image used directly
# vm_image = "/path/to/disk.qcow2"          # ALE; a VM image used directly
# vm_base = "/path/to/windows-base.qcow2"   # ALE; the base environment/build.ps1 builds on
cpus = 1                                    # required; integer >= 1
memory_mb = 4096                            # required; integer >= 128
storage_mb = 8192                           # optional writable storage; integer >= 256
gpus = 0                                    # optional GPU count, not indices; default 0
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}", REGION = "eu" }  # optional; see Variables
skills_dir = "/skills"                      # optional; the Task's Skills in the image

[[environment.mcp_servers]]                 # optional; one table per server
name = "search"
transport = "stdio"                         # required: stdio | streamable-http
command = "/opt/search-mcp/server"          # stdio: a program in the image
args = ["--index", "/srv/index"]            # stdio: optional
# url = "https://mcp.example.org/mcp"       # streamable-http

[agent]                                     # required: the evaluated agent and the oracle
user = "user"                               # required account; must exist in the image
timeout_sec = 900                           # required positive seconds
network_mode = "allowlist"                  # required: no-network | allowlist | public
allowed_hosts = ["catalog.example.org"]     # required, and only, for allowlist
sudo = false                                # ALE; working sudo (Administrator on Windows)
# workdir = "/home/user/project"            # ALE; where the agent starts; default its home

[verifier]                                  # required
timeout_sec = 300                           # required positive seconds
environment_mode = "separate"               # optional: shared | separate
env = { REFERENCE_DB = "${REFERENCE_DB}" }  # optional; the verifier only

[verifier.environment]                      # optional: a separate verifier's own sandbox
vm = false                                  # ALE; optional: tests/Dockerfile builds a VM
# docker_image = "registry.example/verifier:tag"  # a container image used directly
# vm_image = "/path/to/verifier.qcow2"      # ALE; a VM image used directly
cpus = 1                                    # required in this table
memory_mb = 4096                            # required in this table
storage_mb = 8192                           # optional
gpus = 0                                    # optional; default 0
env = {}                                    # optional; replaces [environment].env

[solution]                                  # optional
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}" }  # optional; the oracle only
```

`vm = true` is needed only for a Linux VM built from a Dockerfile: `os = "windows"`, `vm_image`
and `vm_base` already mean a VM. `vm_image` and `vm_base` are mutually exclusive; `vm_base`
requires Windows and `environment/build.ps1`. `[agent].workdir` is absolute and must be a
directory when the agent starts; stages keep their fixed directories.

Harbor's other fields are left out because ALE's design already covers them. `environment/setup/`
can wait for a service, so there is no `healthcheck`. Stages run in fixed directories under the
framework's identity, so there is no `[environment].workdir` or `[verifier].user`.
`[agent].network_mode` is the one network setting, the framework bounds each kind of build, and
a Task that grades live state uses shared verification rather than `[[verifier.collect]]`.

Three values differ from Harbor's. `os = "windows"` is a VM with `.ps1` stage entries, because
ALE runs Windows in VMs, where PowerShell is always available; Harbor's is a Windows container
with `.bat` entries. `artifacts` are absolute and may be globs. A `skills_dir` that does not
exist fails the Episode.

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

Separate verification always transfers the declared artifacts to its verifier through the
Host, as in Harbor. Run-level `artifacts.collect` decides only whether the episode keeps a
copy: the default `"none"` keeps none, and `"host"` keeps them in either mode. `run`,
`validate`, and `reverify` use the same policy. Separate-verifier images must not contain
prepopulated candidate answers; tasks that grade original links or live state should use
shared verification.

### Verification topology

Verification is shared unless `environment_mode = "separate"` or `[verifier.environment]`
says otherwise, as in Harbor. Separate verification runs in an independent sandbox with the
`[verifier.environment]` resources, or the solver's without sudo when that table is absent.
Its image is the first of `[verifier.environment].docker_image` or `vm_image`,
`tests/Dockerfile` (a VM with `[verifier.environment].vm = true`), and the solver image.
`tests/Dockerfile` requires separate verification. A Windows verifier reuses the solver image
or names a `vm_image`. The Verification APIs and records are defined only in
[Verification](verification.md).

### Resources and network

`[environment]` selects container or VM behavior; Tasks never name a Provider.
`[agent].network_mode` applies to the evaluated agent and the oracle: `no-network` reaches only
the Gateway, `allowlist` also the declared hosts, `public` everything. As in Harbor, an
`allowed_hosts` entry `pypi.org` is that host and `*.pypi.org` every name below it, not
`pypi.org` itself; addresses and CIDR ranges are not supported. Setup and verification network
is framework-controlled; its enforcement is specified in [Security](security.md).
The quality standard owns resource feasibility and network-policy selection. A public-network
justification uses `[metadata].network_justification`.

`[environment]` resources are the whole Sandbox's allocation. The image's own processes, such
as the GUI base images' desktop, share it with the agent and every program the agent starts,
so a GUI Task leaves an agent CLI room above the desktop.

### Variables

Harbor's `env` tables set environment variables for the Task's commands:

| Table | Received by |
|---|---|
| `[environment].env` | setup, the agent and the oracle, and a shared verifier |
| `[verifier].env` | the verifier |
| `[verifier.environment].env` | a separate verifier, in place of `[environment].env` |
| `[solution].env` | the oracle |

A separate verifier without `[verifier.environment]` receives `[environment].env`, because
Harbor copies `[environment]` for it. `[verifier].env` and `[solution].env` win over the table
beneath them, and a variable the framework or the Harness sets for a command wins over every
table. Names beginning with `ALE_` are the framework's.

A value is a literal or a whole-value reference, `${NAME}` or `${NAME:-default}`. References
resolve from the checkout's `.env` (`ALE_ENV_FILE` overrides) and nothing else: a variable
only exported in the operator's shell, such as their own `GITHUB_TOKEN`, never reaches a
Sandbox by accident. An empty value counts as unset, as in a shell. `run`, `validate` and
`reverify` resolve every selected Task before any image is built, Sandbox started or model
called, and fail naming each Task's missing variables; `[solution].env` is resolved only for
the oracle. As in Harbor, `${...}` inside other text is not a reference and stays as written.
Values travel in each command's environment through the guest service, never in an image, a
Sandbox's configuration or a Host command line, so container and VM, Linux and Windows
receive them the same way.

A variable is a credential when its name, or the name it refers to, contains `KEY`, `SECRET`,
`TOKEN` (not `TOKENS`), `PASSWORD`, `CREDENTIAL` or `AUTH`. `ale lint` fails a credential
written as a literal or with a non-empty default; it is written `${NAME}` and its value lives
in the `.env`. Credential values from the `.env` are redacted from every record of the
episode (see [Trace](trace.md)), except values that are only digits or a boolean word, which
cannot be told apart from ordinary output. Artifacts are the agent's output and are copied
exactly. The RunLock records each variable's table, name and source (literal, `.env` or
default), never its value.

These tables never carry the Host's own credentials. A Task cannot refer to an `ALE_`
variable, and a resolved value equal to a model, Judge or Provider key fails resolution:
those keys reach a model only through the Gateway
([Security](security.md#3-provider-credentials-stay-on-the-host)).

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
in it. A prepared image used directly is named by `[environment].vm_image` without `build.ps1`.

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

## Skills and MCP servers

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server; never depend on ambient Host configuration. As in
Harbor, a Task's own are part of its image, so the image's identity covers them. Put their files
below `environment/` and have the Dockerfile or `build.ps1` place them.

`[environment].skills_dir` is an absolute directory in the image holding one directory per
Skill. The Harness copies its contents to the agent's skills directory before the agent starts.

`[[environment.mcp_servers]]` uses Harbor's `name`, `transport`, `command`, `args` and `url`.
`transport` is required because Harbor's default, `sse`, is not implemented, and `headers` is
not supported. A stdio server is a program in the image; one that needs its own directory or
variables starts through a wrapper script there. A `streamable-http` URL must be reachable
under `[agent]`'s network, which ALE checks before any Sandbox exists.

An operator can add Skills and MCP servers for a run without rebuilding the image. They stay on
the Host, are uploaded for the Episode, and, as in Harbor, replace a Task's of the same name for
that run; the RunLock records what the run used. See
[Security](security.md#8-agent-resources-are-declared-not-discovered).

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
