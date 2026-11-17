# Task authoring contract

A Task is one self-contained folder: nothing outside it affects it, so it can be reviewed,
moved, and exported on its own. This document owns its directory, configuration,
environment, data visibility, and stage interfaces. Design and acceptance standards belong to
the [Task quality standard](task-quality-standard.md); scoring and Judge APIs belong to
[Verification](verification.md).

The format is based on Harbor's task format (`task.toml`, `environment/`, `tests/`,
`solution/`). A field ALE implements as Harbor does keeps Harbor's name, table and meaning.
A field ALE adds sits in the Harbor table of what it configures, under a name Harbor gives
no other meaning: a separate namespace would not stop one name meaning two things, and would
have to mirror every table it extends. `schema_version` is the ALE version in which the
format last changed, and ALE accepts exactly that value, so a file written for another
format fails as it loads instead of being read with a different meaning.

## Task folder

```text
my-task/
├── task.toml                  # Task configuration
├── instruction.md             # instruction shown to the evaluated agent
├── environment/               # the machine the agent starts on
│   ├── Dockerfile             # Linux container image; context environment/ without setup/
│   ├── build.sh               # Linux or macOS VM image on [environment].image, same context
│   ├── build.ps1              # Windows VM image on [environment].image, same context
│   └── setup/                 # optional per-episode initialization
│       └── setup.sh           # setup.ps1 on Windows
├── tests/                     # verification
│   ├── test.sh                # test.ps1 on Windows
│   ├── verify.py
│   └── Dockerfile             # optional separate-verifier image (context tests/)
└── solution/                  # known-good solution for validation
    └── solve.sh               # solve.ps1 on Windows
```

Required files are `task.toml`, `instruction.md`, the solution entry for the Task OS and the
tests entry for the verifier's OS, the Task's unless a separate verifier names another:
`solve.sh` and `test.sh` on Linux and macOS, `solve.ps1` and `test.ps1` on Windows.
The solver's image follows from `environment/` and `[environment]`'s `image` or `software`:

- `environment/Dockerfile` builds a Linux container. It names its base in `FROM`, so an
  `image` beside it fails, and any `build.sh` beside it is only a file in its context.
- `environment/build.sh` on Linux and macOS, `environment/build.ps1` on Windows, builds a VM
  on the VM image `image` names.
- `image` alone is used directly.
- `[environment].software` names licensed software instead of an image; see
  [Software](#software).

Windows and macOS Tasks run in VMs, or on a machine that has the software they name, so a
Dockerfile fails there. A Linux image used directly is a VM when `image` is an absolute path
to a qcow2 or names an ALE VM disk, and a container when it names a container image
([Sandbox image](sandbox-image.md#source-selection-and-identity)). Every image build has
`environment/` as its context, `setup/` excluded.

## task.toml

`task.toml` is strict, and small. Only `schema_version` and `[task].name` are required; every
other key has a default, so a Task states what makes it different. An unknown key fails, and a
Harbor key ALE does not implement fails with a message saying so. The annotated shape below
describes every key, with `ALE` marking the ones Harbor does not have.

```toml
schema_version = "0.2.0"                    # required; exactly this
artifacts = [                               # outcome paths
  "output",                                 # below where the agent starts
  "/home/user/games/*.sgf",
  { source = "/srv/app", exclude = [".venv", "*.pyc"] },
]

[task]                                      # required
name = "agentslastexam/my-task"             # required org/name, unique in its collection
description = "Summarize the corpus."       # descriptive
version = "1.0"                             # descriptive
authors = [{ name = "Ada", email = "ada@example.org" }]  # descriptive
keywords = ["document-analysis"]            # descriptive

[metadata]                                  # free-form values; no runtime behavior
network_justification = "the Task reads one publisher's live catalog"

[environment]                               # the solver's machine
os = "linux"                                # linux | windows | macos; default linux
# image = "registry.example/image:tag"      # ALE; used directly, or a build script's base
# software = ["rhino-7"]                    # ALE; licensed software instead of an image
build_timeout_sec = 3600                    # seconds the Task's own build may run; default 3600
cpus = 1                                    # whole CPUs; default 1
memory_mb = 4096                            # default 4096
storage_mb = 8192                           # writable storage; default the Provider's
gpus = 0                                    # GPU count, not indices; default 0
# gpu_types = ["RTX-PRO-6000", "L40S"]      # models any of which will do; default any
# gpu_memory_mb = 8192                      # ALE; MiB needed on each GPU; default whole GPUs
container_features = []                     # ALE; such as ["perf", "mount"]; default none
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}", REGION = "eu" }  # see Variables
skills_dir = "/skills"                      # absolute; the Task's Skills in the image

[[environment.mcp_servers]]                 # one table per server
name = "search"
transport = "stdio"                         # required: stdio | streamable-http (or http)
command = "/opt/search-mcp/server"          # stdio: a program in the image
args = ["--index", "/srv/index"]            # stdio
# url = "https://mcp.example.org/mcp"       # streamable-http
# headers = { Authorization = "Bearer ${SEARCH_TOKEN}" }  # streamable-http

[agent]                                     # the evaluated agent and the oracle
user = "user"                               # account in the image; root needs sudo; default user
timeout_sec = 900                           # positive seconds; default 900
network_mode = "allowlist"                  # no-network | allowlist | public; default no-network
allowed_hosts = ["catalog.example.org"]     # required, and only, for allowlist
sudo = false                                # ALE; sudo works exactly when true (Administrator on Windows)
# workdir = "project"                       # ALE; where the agent starts; default its home

[verifier]
timeout_sec = 600                           # positive seconds; default 600
environment_mode = "separate"               # shared | separate
env = { REFERENCE_DB = "${REFERENCE_DB}" }  # the verifier only

[verifier.environment]                      # a separate verifier's own sandbox
os = "linux"                                # default [environment].os
# image = "registry.example/verifier:tag"   # ALE; used directly instead of tests/Dockerfile
build_timeout_sec = 3600                    # default 3600
cpus = 1                                    # default [environment].cpus
memory_mb = 4096                            # default [environment].memory_mb
storage_mb = 8192                           # default the Provider's
gpus = 0                                    # default 0
# gpu_types = ["H100"]                      # default any
# gpu_memory_mb = 4096                      # ALE; default whole GPUs
container_features = []                     # ALE; default none
env = {}                                    # replaces [environment].env

[solution]
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}" }  # the oracle only
```

`[agent].workdir` is below the agent's home when relative, so a Task need not spell its home
per OS. It must be a directory when the agent starts; stages keep their fixed directories.

Harbor's other fields fall into two groups. Nothing in ALE replaces multi-step Tasks (`steps`,
`multi_step_reward_strategy`), compose sidecars (`docker-compose.yaml`, which `ale lint` refuses,
an artifact's `service`, `[[verifier.collect]]`), or TPU selection (`tpu`). ALE's design
covers the rest. `[environment].image` names every image, so `docker_image` is an unknown
key. `environment/setup/` can wait for a service, so there is no `healthcheck`.
Stages run in fixed directories under the framework's identity, so there is no
`[environment].workdir` or `[verifier].user`. `[agent].network_mode` is the one network setting.
ALE keeps its copies in an inventory of its own, so an artifact has no `destination`. An origin
belongs in `[metadata]`, so there is no `source`.

These values differ from Harbor's:

- A Sandbox always has a size and a time limit, and its network stays closed until a Task opens
  it, so some defaults differ: `cpus` is 1, `memory_mb` 4096 and `[agent].timeout_sec` 900, where
  Harbor sets no limit; `[agent].user` is `user`, not the image's `USER`;
  `[agent].network_mode` is `no-network`, not `public`; `build_timeout_sec` is 3600, not 600.
- `os = "windows"` is a VM, or a machine that has the software the Task names, with `.ps1`
  stage entries, because ALE runs Windows there, where PowerShell is always available;
  Harbor's is a Windows container with `.bat` entries.
- `os = "macos"` is ALE's: a macOS VM with `.sh` stage entries, which Harbor does not run.
- `artifacts` may be globs, and nothing is collected undeclared, where Harbor always collects
  `/logs/artifacts`.
- `${NAME}` in an `env` table resolves from the checkout's `.env`, not the process environment,
  and an empty value counts as unset; see [Variables](#variables).
- A `skills_dir` that does not exist fails the Episode.
- `tests/Dockerfile` without separate verification fails, where Harbor ignores it.
- `gpu_types` holds at least one name and requires `gpus`, and every Provider honors it,
  where Harbor's Docker environment ignores it; see [GPUs](#gpus).
- `allowed_hosts` refuses entries that reach the Host; see [Resources and network](#resources-and-network).
- The verifier writes `reward.json` only, with `reward` between 0 and 1; see [tests/](#tests).

When the instruction requires deliverable files, every instructed destination must equal or be
contained by a declared `artifacts` path, or matched by a declared glob. Commands and examples in `instruction.md` must write to
that same destination; do not declare one path while instructing the agent to use another.

### Artifact copying

`artifacts` accepts file paths, directory paths, and globs such as `*.sgf` or `**/*.json`. A
relative path is below the directory the agent starts in, and a separate verifier receives
it below its own start directory, the same `[agent].workdir` below its own account's home;
an absolute path is restored where it was. A table adds `exclude`, patterns that
match as `tar --exclude` does, as in Harbor: `.venv` is that name at any depth and `*.pyc` every
such file. ALE never walks what it leaves out, so a dependency tree costs nothing.

ALE copies each match according to its actual type and skips links, missing paths, and copy
failures. No link is followed anywhere on an artifact's path, a directory above it included:
one the agent made could point at a file only the framework may read. These are fixed
defaults; the verifier decides how to score the resulting output. A copy stops at 100,000
entries or 64 directories deep. An artifact cut off there fails a separate verifier's Episode
as a Task error, because scoring part of an output gives a wrong score rather than a low one.
Copying uses the framework's privileged identity on every supported OS; see
[Permissions](sandbox-image.md#permissions).

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
Its image is `[verifier.environment].image`, a Linux container built from
`tests/Dockerfile`, or else the solver's, as
[Verifier images](sandbox-image.md#verifier-images) says. The Verification APIs and records
are defined only in [Verification](verification.md).

`[verifier.environment].os` runs the verifier on another system than the solver, such as a
Linux container that reads a file a Windows program saved, when the verifier's tools exist
only there. Its `tests/` entry, verifier directory and Python are that system's. It needs
an image of its own, since the solver's runs another system, and receives only artifacts
declared relative, below its own start directory, with `[agent].workdir` relative too: an
absolute path on one system names nothing on the other.

### Resources and network

`[environment]` selects container, VM or software behavior; Tasks never name a Provider.
`[agent].network_mode` applies to the evaluated agent and the oracle: `no-network` reaches
only the Gateway, `allowlist` also the declared hosts, `public` everything. An
`allowed_hosts` entry `pypi.org` is that host alone, `*.pypi.org` that host and every name
below it at any depth, which every Provider that offers `allowlist` can enforce, and an
address or CIDR range a client connecting to that address, never a name that resolves into
it. Entries reaching the Host itself (loopback, link-local, and ranges holding them) fail,
because allowed traffic leaves from the Host. An allowlist carries TLS and needs a client
that honors the proxy variables or, on a desktop, the agent account's system proxy (WinINet
on Windows, GNOME's settings on Linux, the network services' web proxies on macOS), both of
which the framework points at the proxy for the agent and the oracle; raw TCP to an allowed
address stays blocked. Setup and verification network is framework-controlled; its
enforcement is specified in [Security](security.md). The quality standard owns resource
feasibility and network-policy selection. `ale lint` requires
`[metadata].network_justification` for `public`.

`[environment]` resources are the whole Sandbox's allocation. The image's own processes, such
as the GUI base images' desktop, share it with the agent and every program the agent starts,
so a GUI Task leaves an agent CLI room above the desktop. `storage_mb` bounds all that is
written over the image, by the image's own processes, setup, the Harness's installation,
the agent and the verifier alike, as a disk of that size holds it, so that a Task has the
same room wherever it runs. A Provider holds a Sandbox to it by giving it writable storage
of that size at least, where a write past it fails as on a full disk, or by measuring what
it has written and ending it once that passes `storage_mb`; one that can do neither does
not serve the Task, so such a Task never fills the Host's disk. `memory_mb` holds files in
memory, such as a tmpfs, too, and, with shared verification, the verifier beside what the
agent left. A Sandbox lost to either ends its Episode
([Episode protocol](episode-protocol.md#agent-or-oracle)). The Host's swap never adds to
`memory_mb`, so that a Task gets the same memory on every Host; a guest system that pages to
its own disk, as Windows and macOS do, keeps its paging
([Providers](../../packages/ale-run/src/ale/run/providers/README.md#memory)).

### GPUs

`gpus` is a count of whole NVIDIA GPUs. `gpu_types` lists the models the Task accepts, by
Harbor's names such as `H100`, `L40S` or `RTX-PRO-6000`; any one of them will do. Without
it, every model is accepted, as in Harbor. A Task that runs only on some models names them:
one rendering with RTX cameras, for instance, needs GPUs with RT cores, which an H100 lacks.

A name accepts a GPU whose model, as `nvidia-smi` reports it, contains all of the name's
words in order, ignoring case and punctuation: `RTX-PRO-6000` accepts
`NVIDIA RTX PRO 6000 Blackwell Server Edition` and `A100-80GB` accepts
`NVIDIA A100-SXM4-80GB`, while `A10` does not accept an A100. Harbor's names for GKE
accelerators, such as `nvidia-tesla-t4` and `h100-mega`, stand for the model they name.

Every Provider gives the Sandbox only accepted models, or refuses it before any Task code
runs: a Task that names models would fail on others, and that failure would be scored as
the agent's. The RunLock records the requested names beside the models observed in the
Sandbox. How each Provider chooses is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#gpu-models).

`gpu_memory_mb` is the device memory, in MiB, that the Sandbox needs on each of its GPUs,
and requires `gpus`. It counts everything its processes allocate there, each process's CUDA
context included, whose size
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#gpu-memory) gives.
Without it the Sandbox takes its GPUs whole. With it, a Provider that shares GPUs may give
each to several such Sandboxes, holding each to what it named, so a Task that uses a few GiB
does not take a whole 80 GB GPU; a Provider that does not gives whole GPUs, of a model with
at least that much memory where the leaf knows the model's memory. A Sandbox that goes past
it fails as on a full GPU, or loses the process that went past it, as on a memory limit
([Sandbox image](sandbox-image.md#gpu-memory)). Frameworks that size their memory pool from
what the GPU reports must be configured to stay within it, since a Sandbox still sees the
whole GPU.

### Container features

A container Sandbox lacks some kernel features, as containers do, because each widens what
its code can reach of the kernel it runs on. `container_features` names the ones a Task
needs, each at most once. Each name guarantees what root in the Sandbox can do; setup and
verification run as root, and the agent becomes root with `[agent].sudo`:

| Name | Root in the Sandbox can |
|---|---|
| `nested_containers` | run a container engine installed in the Sandbox, such as Docker, and start containers with it |
| `perf` | open software performance events on its own processes with `perf_event_open`; hardware counters depend on the machine |
| `bpf` | create eBPF maps and load eBPF programs with `bpf`; which program types load depends on the kernel |
| `io_uring` | set up and use `io_uring` instances |
| `fuse` | open `/dev/fuse`, through which a program serves a file system; mounting that file system takes `mount` too |
| `raw_sockets` | open raw and packet sockets, as `ping` and packet capture do |
| `user_namespaces` | create user namespaces, as rootless container engines and browser sandboxes do |
| `mount` | mount file systems, such as a tmpfs, and create mount namespaces |

A Task names what it needs and never a Provider or runtime. A Run gives each Sandbox only a
Provider that gives every feature it names, and refuses to start when none it may use does
([Provider selection](sandbox-image.md#provider-selection)). A Provider widens a Sandbox
only for the features it names, so a Task that names none runs as isolated as its Provider
runs any container. A separate verifier names its own in `[verifier.environment]`, and
takes the solver's when that table is absent, as it takes its size.

A VM has a kernel of its own, so a VM image takes none: naming one fails as the Task loads
when its folder makes it a VM, and before anything is prepared when its registry does. Which
Provider gives which feature, and how, is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#container-features).

### Variables

Harbor's `env` tables set environment variables for the Task's commands:

| Table | Received by |
|---|---|
| `[environment].env` | setup, the agent and the oracle, and a shared verifier |
| `[verifier].env` | the verifier |
| `[verifier.environment].env` | a separate verifier, in place of `[environment].env` |
| `[solution].env` | the oracle |
| `[[environment.mcp_servers]].headers` | the agent's MCP client |

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
Sandbox's configuration or a Host command line, so every Sandbox, a container, a VM or a
Station's Lane, on Linux, Windows or macOS, receives them the same way.

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
GUI base matching the Task OS and sandbox (macOS has none: name a Tart image, as below),
then add every stable dependency, service, file, permission, and piece of initial state
required by the Task. The Linux bases are:

- container: `ghcr.io/agentslastexam/container-ubuntu22-base` (Ubuntu 22.04), for `FROM`;
- VM: `ghcr.io/agentslastexam/vm-ubuntu24-base` (Ubuntu 24.04), a VM disk for
  `[environment].image`.

Name an exact version, such as `:0.1.1`, never `latest`; `ale new-task` writes the container
base's current version, and `ale lint` reports a registry `FROM` or `image` with no tag or
`latest`. A Task keeps building from the version it names until its author moves it
([Sandbox image](sandbox-image.md#versions)).

The submitted initial environment must be reproducible from `environment/`; an exploratory
sandbox snapshot is not Task source. Build-local scripts may implement non-trivial
installation and configuration. `build_timeout_sec` bounds the Task's own build, which is
not part of the image's identity, so changing it rebuilds nothing.

A container image builds `environment/Dockerfile` with `environment/` as context, `setup/`
excluded. Build-only large inputs can use read-only BuildKit mounts; runtime data belongs in
the image.

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:0.1.1

COPY --chmod=0755 install.sh /tmp/install.sh
RUN /tmp/install.sh && rm /tmp/install.sh
COPY --chmod=0644 corpus.json /home/user/input/corpus.json
```

A VM image is built by a script on the base `[environment].image` names. A Linux VM Task
provides `environment/build.sh`:

```toml
[environment]
image = "ghcr.io/agentslastexam/vm-ubuntu24-base:0.2.0"
```

```bash
#!/usr/bin/env bash
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends sqlite3
install -d -o user -g user "$ALE_HOME/output"
```

A Windows Task provides `environment/build.ps1`; its base includes WinGet and Chocolatey:

```toml
[environment]
os = "windows"
image = "ghcr.io/agentslastexam/vm-windows10-base:0.1.3"
```

```powershell
$ErrorActionPreference = 'Stop'
choco install -y 7zip --no-progress
if ($LASTEXITCODE -ne 0) { throw "7zip installation failed: $LASTEXITCODE" }
Copy-Item .\input.txt "$env:ALE_HOME\input.txt"
```

`ale prepare TASK` boots a temporary VM of the base that already runs ALE's current guestd
and Cua Driver, copies `environment/`, less `setup/`, into it and runs the script under the
framework's identity (root, Administrator on Windows) with that directory as cwd,
`ALE_HOME` set to the agent home and the network open. The copy's files are not executable,
whatever their mode in the folder, as every upload's are not
([Permissions](sandbox-image.md#permissions)), so the script runs a helper through its
interpreter, `bash install.sh` rather than `./install.sh`. It then removes the directory and
keeps the VM's disk as the Task's layer on the base. A Linux or Windows base must already
have the `[agent].user` account, because ALE installs the framework for it before the script
runs. Scripts may use the system's package managers or local unattended installers; pin
package versions where supported and check native command exit codes. Scripts must finish
within `build_timeout_sec`; nonzero exits fail the build. Suppress installer reboots; builds
requiring a mid-script restart are unsupported. ALE owns shutdown. Copy required files out
of the build directory to their final locations, and set the owner of what the script
writes for the agent. Only that context is staged, and the whole build is cached by base,
builder, and every file in it.

A macOS Task (`os = "macos"`) builds the same way on a Tart image named by `image`, with
`environment/build.sh`. `ale prepare TASK` boots the base with ALE's framework installed for
`admin`, the account ALE controls the VM through, which the base must therefore sign in,
runs `bash build.sh` there as for Linux, and keeps the VM's disk as a local image on the
Mac. ALE frames the result for `[agent].user` when a sandbox first uses it, so `build.sh`
may create that account; `ALE_HOME` is then `/Users/<user>`, the home the build must give
it, and the build fails if the account is missing or lives elsewhere. Such an account must
sign in automatically with its first login settings done, as the
[macOS guide](../guides/macos-orchard.md#choose-an-image) says.

### Software

A Task whose program cannot be put in an image, such as licensed software bound to the
machine it is installed on, names it instead: `[environment].software = ["rhino-7"]` lists
the ids of the software its agent needs, each once, as a Provider that serves software knows
them. ALE builds nothing for it, so such a Task has no `environment/Dockerfile`, build
script or `image`. The machine that has the software decides the agent's account and home,
so the Task sets no `[agent].user`, declares only relative `artifacts` and a relative
`[agent].workdir`, and its instruction may name paths relative to where the agent starts.
A separate verifier with an image of its own, `[verifier.environment].image` or
`tests/Dockerfile`, verifies it, often on another system
([Verification topology](#verification-topology)), because verification never runs on the
machine its operator keeps. A Run refuses the Task when no Provider it lists serves the
software.

The `station` leaf serves such a Task's solver in a Lane of a Station whose manifest names
every id it lists, and only when the Task declares `network_mode = "public"`, with its
justification, and no `[agent].sudo` or `storage_mb`, since its Lanes share their machine
([Station guide](../guides/station.md),
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#station)).

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
`/home/user/...` on Linux, `/Users/user/...` on macOS and `C:\Users\user\...` on Windows.
ALE does not add or rewrite a workspace prefix. A Task that names software, whose machine
decides the agent's home, uses paths relative to where the agent starts instead
([Software](#software)).

## Skills and MCP servers

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server; never depend on ambient Host configuration. As in
Harbor, a Task's own are part of its image, so the image's identity covers them. Put their files
below `environment/` and have the Dockerfile, `build.ps1` or `build.sh` place them.

`[environment].skills_dir` is an absolute directory in the image holding one directory per
Skill. The Harness copies its contents to the agent's skills directory before the agent starts.

`[[environment.mcp_servers]]` uses Harbor's `name`, `transport`, `command`, `args`, `url` and
`headers`. `transport` is required because Harbor's default, `sse`, is not implemented; `http`
means `streamable-http`, as in Harbor. A stdio server is a program in the image; one that needs
its own directory or variables starts through a wrapper script there. A `streamable-http` URL
must be reachable under `[agent]`'s network, which ALE checks before any Sandbox exists: under
`allowlist` that means `https`.

`headers` belongs to a `streamable-http` server. As in Harbor, and unlike an `env` value, a
header holds `${NAME}` or `${NAME:-default}` anywhere in its text, so `Bearer ${SEARCH_TOKEN}`
works. References resolve as [Variables](#variables) describe, for an agent's run only: an
oracle has no MCP client. A credential header (`Authorization`, `Cookie`, or a name that marks
a credential) holds only references with no default, after at most an auth scheme, because a
`task.toml` is committed, and needs `https` unless the server is on the Sandbox's loopback.
The Harness writes the resolved value into the agent's own MCP
configuration, so the agent can read it: give the server a credential scoped to this use. Agent
CLIs expand `$`, `{{` and a leading `!` in that configuration, so a value holding one fails
before any Sandbox exists rather than arriving changed.

An operator can add Skills and MCP servers for a run without rebuilding the image. They stay on
the Host, are uploaded for the Episode, and, as in Harbor, replace a Task's of the same name for
that run; the RunLock records what the run used. See
[Security](security.md#8-agent-resources-are-declared-not-discovered).

## tests/

`tests/test.sh` (or Windows `tests/test.ps1`) evaluates the final outcome and writes its
`reward` to `/logs/verifier/reward.json` (Windows `C:\logs\verifier\reward.json`; macOS,
whose root volume is read-only, `/opt/ale/logs/verifier/reward.json`). Verifier
implementation conventionally lives in `tests/verify.py`. ALE uploads the whole directory to
its root-only stage directory, not to Harbor's `/tests`, and runs the entry with that
directory as cwd. Stage code uses ordinary relative paths. It finds a solver output at the
absolute path `artifacts` declares, or one declared relative below its start directory:
`[agent].workdir` below `ALE_HOME`, the home of the agent account in the Sandbox it runs in.
Before each tests entrypoint starts, ALE selects a Python 3.12+: the image's
own when it has one, since that is where the Task installs what `verify.py` imports, and
otherwise the framework's own. It stages `ale_verify` and its schema validator into that
interpreter's site-packages, checks the import, then sets `ALE_VERIFY_PYTHON` in the
entrypoint's process environment.
It is available to the tests and their child processes, including during `validate` and
`reverify`; it is not initialized by the image build, setup stage, or SDK import.
ALE does not install Python into the image's environment or replace the solver's default
interpreter.

The Linux and macOS entry is:

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
