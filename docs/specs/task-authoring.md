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
have to mirror every table it extends. The exception is `[[calibration]]`, which no Harbor
table configures; `validation/` and `calibration/` are ALE's directories. `schema_version`
is the ALE version in which the format last changed, and ALE accepts exactly that value, so
a file written for another format fails as it loads instead of being read with a different
meaning.

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
├── validation/                # ALE; optional verifier whose results the agent receives
│   ├── test.sh                # as tests/test.sh
│   └── verify.py
├── solution/                  # the reference solution, as in Harbor
│   └── solve.sh               # solve.ps1 on Windows
└── calibration/               # ALE; optional further known final states and their scores
    └── missing-summary/       # one directory per further case
        └── solve.sh
```

Required files are `task.toml`, `instruction.md`, and the tests and reference solution
entries for the Task OS: `tests/test.sh` and `solution/solve.sh` on Linux and
macOS, `test.ps1` and `solve.ps1` on Windows; every further
[calibration case](#calibration)'s directory holds the same entry, and
[`validation/`](#validation), when there is one, the tests entry. `[environment].image` and
`environment/` decide the solver's image:

- `environment/Dockerfile` builds a Linux container. It names its base in `FROM`, so an
  `image` beside it fails, and any `build.sh` beside it is only a file in its context.
- `environment/build.sh` on Linux and macOS, `environment/build.ps1` on Windows, builds a VM
  on the VM image `image` names.
- `image` alone is used directly.

Windows and macOS Tasks run only in VMs, so a Dockerfile fails there. A Linux image used
directly is a VM when `image` is an absolute path to a qcow2 or names an ALE VM disk, and a
container when it names a container image
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
  { source = "/srv/app", exclude = [".venv", "*.pyc"], destination = "app" },
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
# image = "registry.example/image:tag"      # or docker_image; used directly, or a build's base
build_timeout_sec = 3600                    # seconds the Task's own build may run; default 3600
cpus = 1                                    # whole CPUs; default 1
memory_mb = 4096                            # default 4096
storage_mb = 8192                           # writable storage; default the Provider's
gpus = 0                                    # GPU count, not indices; default 0
# gpu_types = ["RTX-PRO-6000", "L40S"]      # models any of which will do; default any
# gpu_memory_mb = 8192                      # ALE; MiB needed on each GPU; default whole GPUs
container_features = []                     # ALE; such as ["perf", "mount"]; default none
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}", REGION = "eu" }  # see Variables
# workdir = "/app"                          # the agent's start and verification's directory
skills_dir = "/skills"                      # absolute; the Task's Skills in the image

[environment.healthcheck]                   # optional; Harbor's, run once setup is done
command = "curl -sf localhost:8080/health"  # required; exit 0 is healthy
interval_sec = 5                            # between attempts; default 5
timeout_sec = 30                            # one attempt's limit; default 30
start_period_sec = 0                        # failures meanwhile do not count; default 0
start_interval_sec = 5                      # between attempts meanwhile; default 5
retries = 3                                 # failures in a row that fail the Task; default 3

[[environment.mcp_servers]]                 # one table per server
name = "search"
transport = "stdio"                         # stdio | sse | streamable-http (or http); default sse
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
# workdir = "project"                       # ALE; where the agent starts; default as above
validation_rounds = 0                       # ALE; validation/ results it gets; default 0

[verifier]
timeout_sec = 600                           # positive seconds; default 600
environment_mode = "separate"               # shared | separate | snapshot (ALE)
env = { REFERENCE_DB = "${REFERENCE_DB}" }  # the verifier only

[verifier.environment]                      # a separate or snapshot verifier's own sandbox
# image = "registry.example/verifier:tag"   # or docker_image; separate only; no tests/Dockerfile
build_timeout_sec = 3600                    # separate only; default 3600
cpus = 1                                    # default [environment].cpus
memory_mb = 4096                            # default [environment].memory_mb
storage_mb = 8192                           # default the Provider's
gpus = 0                                    # default 0
# gpu_types = ["H100"]                      # default any
# gpu_memory_mb = 4096                      # ALE; default whole GPUs
container_features = []                     # ALE; default none
env = {}                                    # replaces [environment].env
# workdir = "/grade"                        # verification's directory; default as above

[validation]                                # ALE; where validation/ runs; default [verifier]'s
environment_mode = "separate"               # shared | separate | snapshot

[validation.environment]                    # a separate or snapshot validation verifier's size
cpus = 1                                    # default [environment].cpus
memory_mb = 4096                            # default [environment].memory_mb
storage_mb = 8192                           # default the Provider's
gpus = 0                                    # default 0
# gpu_types = ["H100"]                      # default any
# gpu_memory_mb = 4096                      # default whole GPUs
container_features = []                     # default none
# workdir = "/grade"                        # validation/'s directory; default tests/'s

[solution]
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}" }  # the oracle only

[[calibration]]                             # ALE; one per added case or changed default
name = "missing-summary"                    # lowercase slug: calibration/missing-summary/
expect = { reward = [0.4, 0.6] }            # reward and other numbers: exact or [lo, hi]
# expect_validation = { reward = 1 }        # validation/'s instead; default expect
```

`[agent].workdir` is below the agent's home when relative, so a Task need not spell its home
per OS. It must be a directory when the agent starts. `[environment].workdir`, Harbor's
working directory, follows the same rules: the agent starts there unless `[agent].workdir`
names another start, and `tests/` and `validation/` run there, their entries named by full
path, so Harbor's verifiers find what they expect; `[verifier.environment].workdir` replaces it
for a verifier with a Sandbox of its own. Setup and the oracle keep their stage directories.
`[agent].validation_rounds`, an integer from 0, is how many results of
[`validation/`](#validation) the agent receives; above 0 it requires `validation/`. Run
policy may replace it with `validation_rounds`, a non-negative integer, recorded as the
timeouts' overrides are ([Episode protocol](episode-protocol.md#phase-order)).

Harbor's other fields fall into two groups. Nothing in ALE replaces multi-step Tasks (`steps`,
`multi_step_reward_strategy`), compose sidecars (`docker-compose.yaml`, which `ale lint` refuses,
an artifact's `service`, `[[verifier.collect]]`), or TPU selection (`tpu`). ALE's design
covers the rest. `docker_image` is `image` under Harbor's name, in `[environment]` and
`[verifier.environment]` alike: one field, so a table holds either, never both.
Stages run under the framework's identity, so there is no `[verifier].user`. `[agent].network_mode` is the one network setting.
An origin belongs in `[metadata]`, so there is no `source`.

These values differ from Harbor's:

- `validation/` and `[agent].validation_rounds` are ALE's: a verifier whose results the
  agent receives between its turns; see [validation/](#validation).
- A Sandbox always has a size and a time limit, and its network stays closed until a Task opens
  it, so some defaults differ: `cpus` is 1, `memory_mb` 4096 and `[agent].timeout_sec` 900, where
  Harbor sets no limit; `[agent].user` is `user`, not the image's `USER`;
  `[agent].network_mode` is `no-network`, not `public`; `build_timeout_sec` is 3600, not 600.
- `os = "windows"` is a VM with `.ps1` stage entries, because ALE runs Windows in VMs, where
  PowerShell is always available; Harbor's is a Windows container with `.bat` entries.
- `os = "macos"` is ALE's: a macOS VM with `.sh` stage entries, which Harbor does not run.
- `artifacts` may be globs, and nothing is collected undeclared, where Harbor always collects
  `/logs/artifacts`.
- `${NAME}` in an `env` table resolves from the checkout's `.env`, not the process environment,
  and an empty value counts as unset; see [Variables](#variables).
- A `skills_dir` that does not exist fails the Episode.
- `tests/Dockerfile` without separate verification fails, where Harbor ignores it.
- `environment_mode` also takes `snapshot`, ALE's: a verifier booted from a snapshot of the
  solver; see [Verification topology](#verification-topology).
- The reference solution, `solution/` as in Harbor, is also the [calibration case](#calibration)
  `solution`; further cases live in `calibration/<name>/`, and a `calibration/solution/`
  fails as the Task loads.
- `gpu_types` holds at least one name and requires `gpus`, and every Provider honors it,
  where Harbor's Docker environment ignores it; see [GPUs](#gpus).
- `allowed_hosts` refuses entries that reach the Host; see [Resources and network](#resources-and-network).
- The verifier writes `reward.json`, or `reward.txt` holding one number, with `reward` between
  0 and 1, and only `reward` is scored; see [Result](verification.md#result).

When the instruction requires deliverable files, every instructed destination must equal or be
contained by a declared `artifacts` path, or matched by a declared glob. Commands and examples in `instruction.md` must write to
that same destination; do not declare one path while instructing the agent to use another.

### Artifact copying

`artifacts` accepts file paths, directory paths, and globs such as `*.sgf` or `**/*.json`. A
relative path is below the directory the agent starts in. A table adds `exclude`, patterns that
match as `tar --exclude` does, as in Harbor: `.venv` is that name at any depth and `*.pyc` every
such file. ALE never walks what it leaves out, so a dependency tree costs nothing.
`destination`, as in Harbor, is where the Host keeps the copy: a relative path below the
episode's `artifacts/`, where a path lands as that name and a glob's matches below it. Without
one, each declaration gets a numbered directory there; those names and the inventory,
`snapshot.json`, are ALE's, and destinations may not overlap. A verifier still receives every
copy at its source path.

ALE copies each match according to its actual type and skips links, missing paths, and copy
failures. No link is followed anywhere on an artifact's path, a directory above it included:
one the agent made could point at a file only the framework may read. These are fixed
defaults; the verifier decides how to score the resulting output. A copy stops at 100,000
entries or 64 directories deep. An artifact cut off there fails a separate verifier's Episode
as a Task error, because scoring part of an output gives a wrong score rather than a low one.
Copying uses the framework's privileged identity on every supported OS; see
[Permissions](sandbox-image.md#permissions).

Separate verification transfers the declared artifacts to its verifier through the Host, as
in Harbor, but for those it [mounts views of](#volumes); a snapshot verifier needs none, as
it has the solver's disk. Run-level
`artifacts.collect` decides only whether the episode keeps a copy: the default `"none"`
keeps none, and `"host"` keeps them in any mode. `run`, `calibrate`, and `reverify` use the
same policy. Separate-verifier images must not contain prepopulated candidate answers. Tasks
that grade original links should use shared or snapshot verification, and those that grade
live state, such as a running service, shared verification.

### Volumes

An artifact named by an absolute path, without a glob, that is a directory in the solver's
image, is a volume of its own from the solver's start on a Provider that mounts volumes
(its profiles say so), so that output of tens of GB reaches a separate verifier without
being copied; it holds what the image has there, and one such artifact inside another is
part of the outer one's volume. The solver's snapshot keeps it, and every Sandbox that the
snapshot serves mounts a view of it, what one writes reaching no other: a validation round's
next solver, a snapshot or shared round's verifier booted from it, and a separate verifier
of its own image, when the solver's leaf serves that verifier, which is then saved for it
as for a snapshot verifier. A separate verifier's view holds what copying the artifact
would have given it, no link, special file or `exclude` match, with no entry bound; nothing
of it crosses the Host unless `artifacts.collect = "host"` keeps a copy. Where no view
serves a separate verifier, as on a Provider that mounts no volumes, the artifact is copied
as any other is. `storage_mb` counts what the Sandbox writes to its volumes.

Being a mount changes what the agent can do there: it can empty the directory but not
remove, rename or replace it, `rm -rf out && mkdir out` fails at the `rm`, a hard link
across it fails and a move across it copies. A Task's instruction says so where it matters.

### Verification topology

`[verifier].environment_mode` says where the verifier runs:

- `shared`: in the solver's Sandbox, with its resources. It sees the solver's whole machine,
  processes included, and what it does changes the solver.
- `snapshot`, ALE's: in a new Sandbox booted from a snapshot of the solver, with resources
  of its own. It sees the solver's whole disk but none of its processes, and nothing it does
  reaches the solver.
- `separate`: in a new Sandbox of the verifier's image, with resources of its own. It sees
  only the declared artifacts, some as [views](#volumes) of the solver's snapshot.

Verification is shared unless `environment_mode` or `[verifier.environment]` says otherwise,
as in Harbor: the table alone means `separate`, so `snapshot` is always written out. A
verifier of its own, separate or snapshot, has the `[verifier.environment]` resources, or
the solver's when that table is absent. A separate verifier's image is
`[verifier.environment].image`, a Linux container built from `tests/Dockerfile`, or else the
solver's, as [Verifier images](sandbox-image.md#verifier-images) says.

A snapshot verifier suits verification that needs all the solver left, such as tens of GB of
checkpoints, and more than the solver had, such as more GPUs to train with: a shared one
cannot have other resources and can change the solver, and a separate one gets only the
declared artifacts, copied through the Host. How the solver is saved and the verifier
booted, and which retention cannot hold, is in the
[Episode protocol](episode-protocol.md#verify). Its `[verifier.environment]` may set only
the Sandbox's size and capabilities (`cpus`, `memory_mb`, `storage_mb`, `gpus`, `gpu_types`,
`gpu_memory_mb`, `container_features`) and `env`. `image` and `build_timeout_sec` fail, as
its image is the solver's snapshot, and so does `tests/Dockerfile`. A verifier on the
agent's disk, shared or snapshot, runs as root among programs the agent could change
([Security](security.md#what-is-not-defended-against)).

The Verification APIs and records are defined only in [Verification](verification.md).

### Resources and network

`[environment]` selects container or VM behavior; Tasks never name a Provider.
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
same room wherever it runs. A Sandbox booted from a snapshot, a snapshot verifier or a
continuation, counts what the snapshot holds over the image too, as a disk of that size
holding the snapshot would, so a snapshot verifier's `storage_mb` covers what its solver
wrote. A Provider holds a Sandbox to it by giving it writable storage of that size at
least, where a write past it fails as on a full disk, or by measuring what it has written
and ending it once that passes `storage_mb`; one that can do neither does not serve the
Task, so such a Task never fills the Host's disk. `memory_mb` holds files in memory, such
as a tmpfs, too, and, with shared verification, the verifier beside what the agent left. A
Sandbox lost to either ends its Episode
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
runs any container. A separate or snapshot verifier names its own in
`[verifier.environment]`, and takes the solver's when that table is absent, as it takes its
size.

A VM has a kernel of its own, so a VM image takes none: naming one fails as the Task loads
when its folder makes it a VM, and before anything is prepared when its registry does. Which
Provider gives which feature, and how, is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#container-features).

### Variables

Harbor's `env` tables set environment variables for the Task's commands:

| Table | Received by |
|---|---|
| `[environment].env` | setup, the agent and the oracle, and, under shared verification, the verifier of `tests/` and of `validation/` |
| `[verifier].env` | the verifier, of `tests/` and of `validation/` alike |
| `[verifier.environment].env` | a verifier of its own, instead of `[environment].env` |
| `[solution].env` | the oracle |
| `[[environment.mcp_servers]].headers` | the agent's MCP client |

A separate or snapshot verifier without `[verifier.environment]` receives
`[environment].env`, because Harbor copies `[environment]` for it. `[verifier].env` and
`[solution].env` win over the table beneath them, and a variable the framework or the
Harness sets for a command wins over every table. Names beginning with `ALE_` are the
framework's.

A value is a literal or a whole-value reference, `${NAME}` or `${NAME:-default}`. References
resolve from the checkout's `.env` (`ALE_ENV_FILE` overrides) and nothing else: a variable
only exported in the operator's shell, such as their own `GITHUB_TOKEN`, never reaches a
Sandbox by accident. An empty value counts as unset, as in a shell. `run`, `calibrate` and
`reverify` resolve every selected Task before any image is built, Sandbox started or model
called, and fail naming each Task's missing variables; `[solution].env` is resolved only for
the [oracle](#calibration). As in Harbor, `${...}` inside other text is not a reference and
stays as written.
Values travel in each command's environment through the guest service, never in an image, a
Sandbox's configuration or a Host command line, so container and VM, Linux, Windows and
macOS receive them the same way.

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

## environment/setup/

`environment/setup/` is optional framework-controlled initialization executed after the
sandbox starts. It is never part of an image build. Use it only for state that must vary by
episode: reset mutable data, generate per-episode values, create writable copies, or start
services that depend on dynamic state. Stable installation and configuration belong in the
image.

`[environment.healthcheck]`, Harbor's readiness check, runs right after setup, in the
directory the agent starts in, under the framework's identity and setup's time limit. As in
Harbor and Docker's `HEALTHCHECK`, its first success passes, a failure counts only once
`start_period_sec` has passed, and `retries` failures in a row fail the Episode as a Task
error: a service that never became ready is the Task's fault, not the agent's. Its command
runs in `bash` (`cmd.exe` on Windows, as Harbor runs it) with `[environment].env`.

Setup and verification use the framework's privileged identity; solver and oracle use
the agent identity. See [Permissions](sandbox-image.md#permissions). Image/setup code must
explicitly set required ownership, write and execute permissions instead of relying on Host modes.

## instruction.md

`instruction.md` is the evaluated-agent instruction, used verbatim. Its input and output paths
refer to the declared initial environment and artifacts. Use literal sandbox paths:
`/home/user/...` on Linux, `/Users/user/...` on macOS and `C:\Users\user\...` on Windows.
ALE does not add or rewrite a workspace prefix.

## Skills and MCP servers

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server; never depend on ambient Host configuration. As in
Harbor, a Task's own are part of its image, so the image's identity covers them. Put their files
below `environment/` and have the Dockerfile, `build.ps1` or `build.sh` place them.

`[environment].skills_dir` is an absolute directory in the image holding one directory per
Skill. The Harness copies its contents to the agent's skills directory before the agent starts.

`[[environment.mcp_servers]]` uses Harbor's `name`, `transport`, `command`, `args`, `url` and
`headers`, with Harbor's default `transport`, `sse`; `http` means `streamable-http`, as in
Harbor. A stdio server is a program in the image; one that needs its own directory or variables
starts through a wrapper script there. An `sse` or `streamable-http` URL must be reachable under
`[agent]`'s network, which ALE checks before any Sandbox exists: under `allowlist` that means
`https`. Each Harness configures an SSE server as Harbor's adapter for its agent does, and one
whose agent has no SSE transport is refused before any Sandbox exists
([Harnesses](../../packages/ale-run/src/ale/run/harnesses/README.md#mcp-transports)).

`headers` belongs to an `sse` or `streamable-http` server. As in Harbor, and unlike an `env` value, a
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
directory as cwd, or with [Harbor's working directory](#tasktoml) when the Task names one. Stage code uses ordinary relative paths; solver outputs use literal
absolute paths. Before each tests entrypoint starts, ALE selects a Python 3.12+: the image's
own when it has one, since that is where the Task installs what `verify.py` imports, and
otherwise the framework's own. It stages `ale_verify` and its JSON Schema library into that
interpreter's site-packages, checks the import, then sets `ALE_VERIFY_PYTHON` in the
entrypoint's process environment.
It is available to the tests and their child processes, including during `calibrate` and
`reverify`; it is not initialized by the image build, setup stage, or SDK import.
ALE does not install Python into the image's environment or replace the solver's default
interpreter. The agent's model session is closed once its last turn ends, in every episode,
so a `tests/` that runs the agent's program cannot use the agent's model session
([Security](security.md#5-answers-are-absent-not-hidden)).

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

## validation/

Research-style Tasks split their data as machine learning does. What `environment/` gives
the agent is its training data, to use as it likes. `validation/` is the validation set:
its results reach the agent between turns. `tests/` is the test set, run once at the end
and never shown to the agent. An agent that sees the score it is finally ranked by can
overfit to it, round after round; one that sees only a validation set's cannot overfit to
the test set except through what the validation set shares with it.

`validation/` has the contract of [`tests/`](#tests): the entry `test.sh` (`test.ps1` on
Windows), conventionally `verify.py`, writes `reward.json` and optionally
`verification.json` and [`feedback.md`](verification.md#feedback), and may use `ale_verify`
and Judges. It is staged only when it runs, in its own root-only stage directory,
`/opt/ale/validation` (`C:\ProgramData\ALE\validation` on Windows), where its entry runs
with `ALE_STAGE_DIR` set, as `tests/`'s does. It is absent during every turn of the agent,
as `tests/` is. It runs where ALE's `[validation]` places it, by `[verifier]`'s
[verification topology](#verification-topology) rules, or else where `tests/` runs, with
`[verifier]`'s budget and variables either way. It has no Dockerfile or image of its own: a
separate verifier runs it on the image a separate verifier of `tests/` runs, or else the
solver's, so a `validation/Dockerfile` is only a file there and `[validation.environment]`
sets only the Sandbox's size and capabilities, and its working directory. Placed apart, a Task can, for example, score
each round in a separate verifier that mounts views of the agent's output and the final
state in the solver itself, where a running service can be checked; a validation score then
predicts the final one less well where the two see different things. Run shared, it runs in
a Sandbox booted from a snapshot of the agent's, with the agent's resources and
`[environment].env` and no sudo, since the agent goes on from that state. It receives no
solver trajectory, which is converted once, after the agent's last turn. A copy of `tests/`
therefore works unless it reads the trajectory: `judge(..., trajectory=True)` and the
`checks.trajectory_*` checks raise there. Nothing requires the two to differ.

With `[agent].validation_rounds = N`, the agent works in up to `N + 1` turns: its first turn
ends, `validation/` scores the state it left, the agent receives that result as its next
turn begins, and so on until `tests/` scores the state its last turn left, once. A turn ends
when the agent's program finishes normally, so the agent submits its work by ending its
turn; there is nothing else to call. A turn that a timeout, a limit or a failure ends is the
last. `[agent].timeout_sec` covers all turns together. Between turns the agent's Sandbox is
saved and a new one booted from what it saved, without setup: files on its disk survive the
end of a turn; files in memory, such as a tmpfs, and its desktop session do not, nor does
any process, the agent's or a service setup started. `validation/` therefore never sees a
process the agent or setup left running. How the rounds run is in the
[Episode protocol](episode-protocol.md#validation-rounds).

Rounds need a Harness that resumes its native session, since every turn after the first
resumes it: a Run that gives rounds to one that cannot, such as the `computer-use` policy
Harness, is refused before provisioning, and can set `validation_rounds = 0` instead. Each
round costs a save of the agent's Sandbox, a verifier run under the verify limit
(`[verifier].timeout_sec`, or the Run's `timeouts.verify`) that its Judge calls share, and a
boot; none of them spends the agent's time. Since every calibration case checks
`validation/` too, a Task with `validation/` cannot [calibrate](#calibration) on a Provider
without snapshots.

The agent's model session is closed between turns, and no credential file stays on the
disk a round saves. The round's verifier has verification's open egress and, under shared or
snapshot verification, runs on the agent's disk, so a `validation/` that runs the agent's
program runs it with that network; with `[agent].sudo`, what the agent left there can write
the round's feedback and reward. The round's reward, status and feedback text are all that
reaches the agent ([Security](security.md#5-answers-are-absent-not-hidden)).

What the feedback text reveals is the author's decision: a score alone, the cases that
fail, or a diagnosis. Whatever it says, the agent can use, so it must not hold what `tests/`
checks for if the test set is to stay unseen.

## calibration/

Calibration cases are known final states with the scores each must receive, as a grader
reads anchor essays before marking: when `tests/` changes, `ale calibrate` shows at once
whether every case still scores as declared, and reviewers can judge the cases and their
expected scores without running anything. An attempt to cheat is a case like any other,
expected to earn 0. `[[calibration]]` is a top-level table, laid out as Harbor's `[[steps]]`
with `steps/<name>/` is.

```toml
[[calibration]]
name = "missing-summary"                    # runs calibration/missing-summary/
expect = { reward = [0.4, 0.6] }            # a closed interval

[[calibration]]
name = "hardcoded-output"
expect = { reward = 0 }

[[calibration]]
name = "solution"                           # replaces the default { reward = 1 }
expect = { reward = [0.0, 0.05], tflops = [600, 680] }
```

Every Task has the case `solution`, its reference solution in `solution/`. It
expects `{ reward = 1 }`, and an entry of that name only replaces the expectation, so the
directory needs none. An open-ended Task whose reference solution is its baseline rather
than a full-marks answer declares what that baseline earns, as the last entry above does:
almost no reward, at a throughput between 600 and 680 TFLOPS. Any other `name`, a lowercase
slug unique among the entries, is the directory `calibration/<name>/`. To check that the
start state earns nothing, declare a case whose `solve.sh` does nothing and that expects
`{ reward = 0 }`.

In each case, its directory's `solve.sh` (`solve.ps1` on Windows) supplies the case's final
state: it may execute a solution normally or write that state directly from reference data
it carries. It runs as the oracle, with `[solution].env`, and like `tests/` it is absent
while an evaluated agent works. `calibration/` holds only the further case directories, and
`ale lint` refuses any other entry there, a link or a `solution/` included. A directory
there without an entry fails the Task as it loads, so a case cannot exist without its
expectation.

`expect` includes `reward`. Each value is a number the result must equal, or `[lo, hi]`, a
closed interval it must lie in, which needs lo ≤ hi; booleans, strings, NaN and infinity
fail. `reward` is compared with the stored reward, which lies from 0 through 1 with two
decimals ([Result](verification.md#result)), so an expected reward lies in that range, an
exact one has at most two decimals, and an interval holds at least one two-decimal value.
Any other key names a top-level number of the Episode's
[Verification Result](verification.md#result), compared as written, so a measured value
takes an interval; a missing key, or a value that is not a number, fails the case.

A Task with `validation/` checks it in every case too, with a validation check rather than a
round: `ale calibrate` runs `validation/` once on the case's final state, before `tests/`,
with no turn after it. `expect` applies to both, unless an entry's `expect_validation`, by
the same rules, says what `validation/`'s result must be; it is refused without
`validation/`. The check saves the oracle's Sandbox, as a round saves the agent's, so a Task
with `validation/` needs a Provider that saves snapshots for calibration too, and under
shared verification `tests/` then runs in a Sandbox booted from that snapshot: like
`validation/`, it sees no process the oracle left running.

How `ale calibrate` runs the cases is defined in
[the episode protocol](episode-protocol.md#calibration).
