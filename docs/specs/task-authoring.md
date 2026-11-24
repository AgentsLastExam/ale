# Task authoring contract

A Task is one self-contained folder: nothing outside it affects it, so it can be reviewed,
moved, and exported on its own. This document owns its directory, configuration,
environment, data visibility, and stage interfaces. Design and acceptance standards belong to
the [Task quality standard](task-quality-standard.md); scoring and Judge APIs belong to
[Verification](verification.md).

The format is based on Harbor's task format (`task.toml`, `environment/`, `tests/`,
`solution/`). A field Orbit implements as Harbor does keeps Harbor's name, table and
meaning. A field Orbit adds sits in the Harbor table of what it configures, under a name
Harbor gives no other meaning: a separate namespace would not stop one name meaning two
things, and would have to mirror every table it extends. The exception is `[[calibration]]`,
which no Harbor table configures; `validation/` and `calibration/` are Orbit's directories.
`schema_version` is the Orbit version, major and minor, in which the format last changed,
and Orbit accepts exactly that value, so a file written for another format fails as it loads
instead of being read with a different meaning.

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
├── validation/                # Orbit; optional verifier whose results the agent receives
│   ├── test.sh                # as tests/test.sh
│   └── verify.py
├── solution/                  # the reference solution, as in Harbor
│   └── solve.sh               # solve.ps1 on Windows
└── calibration/               # Orbit; optional further known final states and their scores
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
directly is a VM when `image` is an absolute path to a qcow2 or names an Orbit VM disk, and
a container when it names a container image
([Sandbox image](sandbox-image.md#source-selection-and-identity)). Every image build has
`environment/` as its context, `setup/` excluded.

## task.toml

`task.toml` is strict, and small. Only `schema_version` and `[task].name` are required; every
other key has a default, so a Task states what makes it different. An unknown key fails, and a
Harbor key Orbit does not implement fails with a message saying so. The annotated shape
below describes every key of Orbit's format, with `Orbit` marking the ones Harbor does not
have; the Harbor keys Orbit also reads, so that a Harbor Task loads with few edits, are in
[Harbor compatibility](#harbor-compatibility). A Task Orbit can read but that departs from this
form loads with a warning, which every command that loads it prints and the RunLock records
in `task.warnings`; only an error fails `orbit lint`.

```toml
schema_version = "0.2"                      # required; exactly this
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
# image = "registry.example/image:tag"      # Orbit; used directly, or a build script's base
build_timeout_sec = 3600                    # seconds the Task's own build may run; default 3600
cpus = 2                                    # whole CPUs; default 2
memory_mb = 8192                            # default 8192
storage_mb = 8192                           # writable storage; default the Provider's
gpus = 0                                    # GPU count, not indices; default 0
# gpu_types = ["RTX-PRO-6000", "L40S"]      # models any of which will do; default any
# gpu_memory_mb = 8192                      # Orbit; MiB needed on each GPU; default whole GPUs
container_features = []                     # Orbit; such as ["perf", "mount"]; default none
# nodes = 2                                 # Orbit; machines of this size; default 1
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}", REGION = "eu" }  # see Variables
skills_dir = "/skills"                      # absolute; the Task's Skills in the image

[[environment.mcp_servers]]                 # one table per server
name = "search"
transport = "stdio"                         # stdio | streamable-http
command = "/opt/search-mcp/server"          # stdio: a program in the image
args = ["--index", "/srv/index"]            # stdio
# url = "https://mcp.example.org/mcp"       # streamable-http
# headers = { Authorization = "Bearer ${SEARCH_TOKEN}" }  # streamable-http

[agent]                                     # the evaluated agent and the oracle
user = "user"                               # account in the image; default user
timeout_sec = 1800                          # positive seconds; default 1800
network_mode = "allowlist"                  # no-network | allowlist | blocklist | public; default no-network
allowed_hosts = ["catalog.example.org"]     # required, and only, for allowlist
# blocked_hosts = ["github.com"]            # Orbit; required, and only, for blocklist
sudo = false                                # Orbit; sudo works exactly when true, always for root (Administrator on Windows)
# workdir = "project"                       # Orbit; where the agent starts; default its home
validation_rounds = 0                       # Orbit; validation results it gets; default 0

[verifier]
timeout_sec = 600                           # positive seconds; default 600
environment_mode = "separate"               # shared | separate | snapshot (Orbit)
env = { REFERENCE_DB = "${REFERENCE_DB}" }  # the verifier only
# network_mode = "no-network"               # default public
# allowed_hosts = ["pypi.org"]              # with allowlist, as in [agent]
# blocked_hosts = ["github.com"]            # Orbit; with blocklist, as in [agent]

[verifier.environment]                      # a separate or snapshot verifier's own sandbox
# image = "registry.example/verifier:tag"   # Orbit; separate only; not with tests/Dockerfile
build_timeout_sec = 3600                    # separate only; default 3600
cpus = 1                                    # default [environment].cpus
memory_mb = 4096                            # default [environment].memory_mb
storage_mb = 8192                           # default the Provider's
gpus = 0                                    # default 0
# gpu_types = ["H100"]                      # default any
# gpu_memory_mb = 4096                      # Orbit; default whole GPUs
container_features = []                     # Orbit; default none
# nodes = 1                                 # Orbit; default 1
env = {}                                    # replaces [environment].env

[verifier.validation]                       # Orbit; the rounds' verifier; default [verifier]
timeout_sec = 300                           # every [verifier] key, each over [verifier]'s
environment_mode = "snapshot"               # shared | separate | snapshot
env = { SPLIT = "validation" }              # over [verifier].env, by variable
# network_mode = "public"                   # with its hosts, replaces [verifier]'s whole
# user = "grader"                           # as [verifier].user
visibility = false                          # a read-only copy for the agent; default false

[verifier.validation.environment]           # over [verifier.environment], key by key
# image = "registry.example/val:tag"        # separate only; not with validation/Dockerfile
cpus = 2                                    # here [verifier.environment]'s 1 otherwise

[solution]
env = { CATALOG_TOKEN = "${CATALOG_TOKEN}" }  # the oracle only

[[calibration]]                             # Orbit; one per added case or changed default
name = "missing-summary"                    # lowercase slug: calibration/missing-summary/
expect = { reward = [0.4, 0.6] }            # reward and other numbers: exact or [lo, hi]
# expect_validation = { reward = 1 }        # validation/'s instead; default expect
```

`[agent].workdir` is below the agent's home when relative, so a Task need not spell its home
per OS. It must be a directory when the agent starts; stages keep their fixed directories.
`[agent].validation_rounds`, an integer from 0, is how many results of
[`validation/`](#validation), or of `tests/` for a Task without it, the agent receives. Run
policy may replace it with `validation_rounds`, a non-negative integer, recorded as the
timeouts' overrides are ([Episode protocol](episode-protocol.md#phase-order)).

When the instruction requires deliverable files, every instructed destination must equal or be
contained by a declared `artifacts` path, or matched by a declared glob. Commands and examples in `instruction.md` must write to
that same destination; do not declare one path while instructing the agent to use another.

### Artifact copying

`artifacts` accepts file paths, directory paths, and globs such as `*.sgf` or `**/*.json`. A
relative path is below the directory the agent starts in. A table adds `exclude`, patterns that
match as `tar --exclude` does, as in Harbor: `.venv` is that name at any depth and `*.pyc` every
such file. Orbit never walks what it leaves out, so a dependency tree costs nothing.
`destination` is where the Host keeps the copy: a relative path below the
episode's `artifacts/`, where a path lands as that name and a glob's matches below it. Without
one, each declaration gets a numbered directory there; those names and the inventory,
`snapshot.json`, are Orbit's, and destinations may not overlap. A verifier still receives every
copy at its source path.

Orbit copies each match according to its actual type and skips links, missing paths, and
copy failures. No link is followed anywhere on an artifact's path, a directory above it
included: one the agent made could point at a file only the framework may read. These are
fixed defaults; the verifier decides how to score the resulting output. A copy stops at
100,000 entries or 64 directories deep. An artifact cut off there fails a separate
verifier's Episode as a Task error, because scoring part of an output gives a wrong score
rather than a low one. Copying uses the framework's privileged identity on every supported
OS; see [Permissions](sandbox-image.md#permissions).

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
- `snapshot`, Orbit's: in a new Sandbox booted from a snapshot of the solver, with resources
  of its own. It sees the solver's whole disk but none of its processes, and nothing it does
  reaches the solver.
- `separate`: in a new Sandbox of the verifier's image, with resources of its own. It sees
  only the declared artifacts, some as [views](#volumes) of the solver's snapshot.

Verification is shared unless `environment_mode` or `[verifier.environment]` says otherwise,
as in Harbor: the table alone means `separate`, so `snapshot` is always written out. A
verifier of its own, separate or snapshot, has the `[verifier.environment]` resources, or
the solver's when that table is absent. A separate verifier's image is
`[verifier.environment].image`, a Linux container built from `tests/Dockerfile`, or else the
solver's, as [Verifier images](sandbox-image.md#verifier-images) says. The verifier of
[validation rounds](#validation) is placed by the same rules, from `[verifier.validation]`.

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
`[agent].network_mode` applies to the evaluated agent and the oracle: `no-network`, the
default, reaches only the Gateway, `allowlist` also the declared hosts, `blocklist` every
public host but the declared ones, `public` everything. An `allowed_hosts` or `blocked_hosts`
entry `pypi.org` is that host alone, `*.pypi.org` that host and every name below it at any
depth, which every Provider that offers the mode can enforce, and an address or CIDR range a
client connecting to that address. Entries reaching the Host itself (loopback, link-local, and
ranges holding them) fail, because the traffic of either list leaves from the Host.

Both lists go through the Host's egress proxy, so they carry TLS and need a client that
honors the proxy variables or, on a desktop, the agent account's system proxy (WinINet on
Windows, GNOME's settings on Linux, the network services' web proxies on macOS), both of
which the framework points at the proxy for whatever runs under either list. Plain HTTP, raw
TCP, UDP and name resolution inside the Sandbox stay blocked, so beyond its hosts a
blocklist is narrower than `public`: `pip`, `git` over HTTPS and a browser work, `apt` from an
`http://` mirror, `ssh` and a program that ignores the proxy settings do not. An allowlist
admits a name, never an address it resolves to. A blocklist also judges what a name
resolves to and what the client's TLS handshake names: it refuses a name any of whose
addresses is blocked, is not public or is the Host's own, and a tunnel whose handshake names
a blocked host, whatever address the client asked for, so `github.com` blocked stays blocked
when asked for by one of its addresses. A host on a private network therefore takes an
allowlist. A blocklist keeps the agent from the hosts it names, not from what they serve: a
mirror, a cache or another host with the same content stays reachable
([Security](security.md#2-egress-is-a-decision-made-in-our-code)).

`[verifier].network_mode` and its hosts are the verifier's network, and
`[verifier.validation]`'s that of [validation rounds](#validation), in every topology, with
the same modes and host rules. The default is `public`: a verifier runs the Task's trusted
code, which often needs what the agent may not reach. Under `no-network` a verifier still
reaches the Gateway, so its [Judges](verification.md#judge-tools) work. Setup, the Harness's installation and image builds
have the network, since they are the Task's trusted preparation and ours; enforcement is
specified in [Security](security.md). The quality standard owns resource feasibility and
network-policy selection. `orbit lint` requires `[metadata].network_justification` when the
agent's network is `public` or `blocklist`.

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
its own disk, as Windows and macOS do, keeps its paging. A Linux Sandbox's `/dev/shm` holds
half its `memory_mb`, as on a Linux machine of that memory, so that processes that share
memory through it, such as PyTorch's DataLoader workers, get the same room on every Provider
([Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#memory)).

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
[Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#gpu-models).

`gpu_memory_mb` is the device memory, in MiB, that the Sandbox needs on each of its GPUs,
and requires `gpus`. It counts everything its processes allocate there, each process's CUDA
context included, whose size
[Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#gpu-memory) gives.
Without it the Sandbox takes its GPUs whole. With it, a Provider that shares GPUs may give
each to several such Sandboxes, holding each to what it named, so a Task that uses a few GiB
does not take a whole 80 GB GPU; a Provider that does not gives whole GPUs, of a model with
at least that much memory where the leaf knows the model's memory. A Sandbox that goes past
it fails as on a full GPU, or loses the process that went past it, as on a memory limit
([Sandbox image](sandbox-image.md#gpu-memory)). Frameworks that size their memory pool from
what the GPU reports must be configured to stay within it, since a Sandbox still sees the
whole GPU.

### Nodes

`nodes` is how many machines the Sandbox spans, each with the CPUs, memory, storage and
GPUs above, for work that one machine cannot hold, such as training across sixteen GPUs.
The agent works on node 0, where its commands and files are; every node runs the same image
under the same account, sudo and network policy, and its disk is its own, so data one node
needs on another is copied there or fetched there. The nodes are named `node0` to
`node<N-1>`, which is also each one's hostname, and reach one another on a network of
their own that reaches nothing else, whatever the Task's network; a job's ranks rendezvous
and exchange data there, or over a faster fabric the Provider gives them, such as
InfiniBand. `ORBIT_NODES` lists the names and `ORBIT_NODE_RANK` is each node's number.

On every node, `orbit-exec NODE COMMAND [ARG...]` runs a command on another node as the
account that runs it, as `ssh` would: its arguments joined by spaces and run by `sh` there,
in the same directory, or its home where that is missing there, with its input, output and
exit status. Unlike `ssh`, the command also gets the variables its caller exported, the
Task's among them, apart from those that describe a node, such as `ORBIT_NODE_RANK` and
`HOME`, which are the other node's. A distributed launcher takes it as its remote shell, so
the agent starts `torchrun` on each node, or `mpirun` with
`--mca plm_rsh_agent orbit-exec`, without a scheduler or its credentials:

```bash
orbit-exec node1 torchrun --nnodes=2 --nproc-per-node=8 --rdzv-backend=c10d \
    --rdzv-endpoint=node0:29400 train.py &
torchrun --nnodes=2 --nproc-per-node=8 --rdzv-backend=c10d \
    --rdzv-endpoint=node0:29400 train.py
```

A verifier's `nodes` is its own, one unless its table names more; a verifier without a
table spans the agent's. A snapshot verifier boots each of its nodes from the agent's node
of the same number, so it spans no more nodes than the agent. Only some Providers join
machines into one Sandbox
([Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#provider-ids-and-profiles));
the others refuse a Task that names more than one node before anything starts.

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
[Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#container-features).

### Variables

Harbor's `env` tables set environment variables for the Task's commands:

| Table | Received by |
|---|---|
| `[environment].env` | setup, the agent and the oracle, and, under shared verification, the verifier of `tests/` and of validation rounds |
| `[verifier].env` | the verifier, of `tests/` and of validation rounds alike |
| `[verifier.environment].env` | a verifier of its own, instead of `[environment].env` |
| `[verifier.validation].env`, `[verifier.validation.environment].env` | the verifier of validation rounds, over the two above |
| `[solution].env` | the oracle |
| `[[environment.mcp_servers]].headers` | the agent's MCP client |

A separate or snapshot verifier without `[verifier.environment]` receives
`[environment].env`, because Harbor copies `[environment]` for it. `[verifier].env` and
`[solution].env` win over the table beneath them, and a variable the framework or the
Harness sets for a command wins over every table. Names beginning with `ORBIT_` are the
framework's.

A value is a literal or a whole-value reference, `${NAME}` or `${NAME:-default}`. References
resolve from the checkout's `.env` (`ORBIT_ENV_FILE` overrides) and nothing else: a variable
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
`TOKEN` (not `TOKENS` or `TOKENIZER`), `PASSWORD`, `CREDENTIAL` or `AUTH`. `orbit lint` fails a
credential written as a literal or with a non-empty default; it is written `${NAME}` and its
value lives in the `.env`. Credential values from the `.env` are redacted from every record of
the episode (see [Trace](trace.md)), except values that are only digits or a boolean word, which
cannot be told apart from ordinary output. Artifacts are the agent's output and are copied
exactly. The RunLock records each variable's table, name and source (literal, `.env` or
default), never its value.

These tables never carry the Host's own credentials. A Task cannot refer to an `ORBIT_`
variable, and a resolved value equal to a model, Judge or Provider key fails resolution:
those keys reach a model only through the Gateway
([Security](security.md#3-provider-credentials-stay-on-the-host)).

## environment/

The image is the reproducible initial computer state supplied to the agent. Start from the Orbit
GUI base matching the Task OS and sandbox (macOS has none: name a Tart image, as below),
then add every stable dependency, service, file, permission, and piece of initial state
required by the Task. The Linux bases are:

- container: `ghcr.io/agentslastexam/container-ubuntu22-base` (Ubuntu 22.04), for `FROM`;
- VM: `ghcr.io/agentslastexam/vm-ubuntu24-base` (Ubuntu 24.04), a VM disk for
  `[environment].image`.

Name an exact version, such as `:0.1.1`, never `latest`; `orbit new-task` writes the
container base's current version, and `orbit lint` warns about a registry `FROM` or `image`
with no tag or `latest`. A Task keeps building from the version it names until its author
moves it ([Sandbox image](sandbox-image.md#versions)).

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
image = "ghcr.io/agentslastexam/vm-ubuntu24-base:0.2.1"
```

```bash
#!/usr/bin/env bash
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends sqlite3
install -d -o user -g user "$ORBIT_HOME/output"
```

A Windows Task provides `environment/build.ps1`; its base includes WinGet and Chocolatey:

```toml
[environment]
os = "windows"
image = "ghcr.io/agentslastexam/vm-windows10-base:0.1.4"
```

```powershell
$ErrorActionPreference = 'Stop'
choco install -y 7zip --no-progress
if ($LASTEXITCODE -ne 0) { throw "7zip installation failed: $LASTEXITCODE" }
Copy-Item .\input.txt "$env:ORBIT_HOME\input.txt"
```

`orbit prepare TASK` boots a temporary VM of the base that already runs Orbit's current
guestd and Cua Driver, copies `environment/`, less `setup/`, into it and runs the script
under the framework's identity (root, Administrator on Windows) with that directory as cwd,
`ORBIT_HOME` set to the agent home and the network open. The copy's files are not
executable, whatever their mode in the folder, as every upload's are not
([Permissions](sandbox-image.md#permissions)), so the script runs a helper through its
interpreter, `bash install.sh` rather than `./install.sh`. It then removes the directory and
keeps the VM's disk as the Task's layer on the base. A Linux or Windows base must already
have the `[agent].user` account, because Orbit installs the framework for it before the
script runs. Scripts may use the system's package managers or local unattended installers;
pin package versions where supported and check native command exit codes. Scripts must
finish within `build_timeout_sec`; nonzero exits fail the build. Suppress installer reboots;
builds requiring a mid-script restart are unsupported. Orbit owns shutdown. Copy required
files out of the build directory to their final locations, and set the owner of what the
script writes for the agent. Only that context is staged, and the whole build is cached by
base, builder, and every file in it.

A macOS Task (`os = "macos"`) builds the same way on a Tart image named by `image`, with
`environment/build.sh`. `orbit prepare TASK` boots the base with Orbit's framework installed
for `admin`, the account Orbit controls the VM through, which the base must therefore sign
in, runs `bash build.sh` there as for Linux, and keeps the VM's disk as a local image on the
Mac. Orbit frames the result for `[agent].user` when a sandbox first uses it, so `build.sh`
may create that account; `ORBIT_HOME` is then `/Users/<user>`, the home the build must give
it, and the build fails if the account is missing or lives elsewhere. Such an account must
sign in automatically with its first login settings done, as the
[macOS guide](../guides/macos-orchard.md#choose-an-image) says.

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
Orbit does not add or rewrite a workspace prefix.

## Skills and MCP servers

Skills and MCP are optional. Add them only when the Task concept or expert feedback explicitly
requires a specific Skill or MCP server; never depend on ambient Host configuration. As in
Harbor, a Task's own are part of its image, so the image's identity covers them. Put their files
below `environment/` and have the Dockerfile, `build.ps1` or `build.sh` place them.

`[environment].skills_dir` is an absolute directory in the image holding one directory per
Skill. The Harness copies its contents to the agent's skills directory before the agent starts.

`[[environment.mcp_servers]]` uses Harbor's `name`, `transport`, `command`, `args`, `url` and
`headers`. `transport` is `stdio` or `streamable-http`. A stdio server is a program in the
image; one that needs its own directory or variables starts through a wrapper script there. A
`streamable-http` URL must be reachable under `[agent]`'s network, which Orbit checks before any
Sandbox exists: under `allowlist` or `blocklist` that means `https`.

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
whose root volume is read-only, `/opt/orbit/logs/verifier/reward.json`). Verifier
implementation conventionally lives in `tests/verify.py`. Orbit uploads the whole directory
to its root-only stage directory, not to Harbor's `/tests`, and runs the entry with that
directory as cwd. Stage code uses ordinary relative paths; solver outputs use literal
absolute paths. Before each tests entrypoint starts, Orbit selects a Python 3.12+: the
image's own when it has one, since that is where the Task installs what `verify.py` imports,
and otherwise the framework's own. It stages `orbit_verify` and its JSON Schema library into
that interpreter's site-packages, checks the import, then sets `ORBIT_VERIFY_PYTHON` in the
entrypoint's process environment.
It is available to the tests and their child processes, including during `calibrate` and
`reverify`; it is not initialized by the image build, setup stage, or SDK import.
Orbit does not install Python into the image's environment or replace the solver's default
interpreter. The agent's model session is closed once its last turn ends, in every episode,
so a `tests/` that runs the agent's program cannot use the agent's model session
([Security](security.md#5-answers-are-absent-not-hidden)).

The Linux and macOS entry is:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec "$ORBIT_VERIFY_PYTHON" verify.py
```

The Windows entry is:

```powershell
& $env:ORBIT_VERIFY_PYTHON .\verify.py
exit $LASTEXITCODE
```

APIs, scoring, and record behavior are defined in [Verification](verification.md).

## validation/

Research-style Tasks split their data as machine learning does. What `environment/` gives
the agent is its training data, to use as it likes. `validation/` is the validation set: its
results reach the agent between turns. `tests/` is the test set, run once at the end and,
beside a validation set, never shown to the agent. An agent that sees the score it is
finally ranked by can overfit to it, round after round; one that sees only a validation
set's cannot overfit to the test set except through what the validation set shares with it.

`validation/` has the contract of [`tests/`](#tests): the entry `test.sh` (`test.ps1` on
Windows), conventionally `verify.py`, writes `reward.json` and optionally
`verification.json` and [`feedback.md`](verification.md#feedback), and may use
`orbit_verify` and Judges. It is staged only when it runs, in its own root-only stage
directory, `/opt/orbit/validation` (`C:\ProgramData\Orbit\validation` on Windows), where its
entry runs with `ORBIT_STAGE_DIR` set, as `tests/`'s does. It is absent during every turn of
the agent, as `tests/` is.

Rounds run in `[verifier]`'s verifier unless Orbit's `[verifier.validation]` says otherwise,
since a round often needs less, or other things, than the final score: a shorter limit, a
smaller Sandbox, a sealed network. That table takes every key `[verifier]` takes, each over
`[verifier]`'s: a key it sets replaces the verifier's, a table it sets merges into the
verifier's key by key and its `env` variable by variable, and a network it names replaces the
inherited one whole. What its placement cannot use is not inherited, so a shared one takes no
environment and a snapshot one no image, and an environment it declares over a shared verifier
makes it separate, as Harbor resolves a verifier that declares one. With
`[verifier] network_mode = "public"` and `[verifier.validation] network_mode = "no-network"`,
for example, the rounds are sealed while `tests/` reaches what it needs. Its placement follows
the [verification topology](#verification-topology) rules; a separate one runs the image
`[verifier.validation.environment].image` names or `validation/Dockerfile` builds, which, as
`tests/Dockerfile` does, requires a separate verifier, or else the one it inherits: a separate
verifier's of `tests/`, else the solver's. Placed apart, a Task can, for example, score
each round in a separate verifier that mounts views of the agent's output and the final
state in the solver itself, where a running service can be checked; a validation score then
predicts the final one less well where the two see different things. Run shared, it runs in
a Sandbox booted from a snapshot of the agent's, with the agent's resources and
`[environment].env` and no sudo, since the agent goes on from that state. Each round
receives the agent's trajectory up to the turn that ended, converted as that turn ends just
as the final one is, so `judge(..., trajectory=True)` and the `checks.trajectory_*` checks
work in every round as they do in `tests/`.

With `[verifier.validation] visibility = true`, the agent works beside a read-only copy of
the stage that validates at `/opt/validation` (`C:\validation` on Windows), mounted from the
Task folder where the solver's Provider mounts Host directories and copied as setup ends
elsewhere, and a line below the instruction says where it is: a validation set the agent may
study, its data and how it is scored, as a researcher studies one. The default is `false`,
which keeps the stage out of every turn. A verifier always runs its own copy, so the agent's
reaches no score.

A Task without `validation/` validates with `tests/` instead: its rounds run `tests/` in the
verifier of validation rounds, so a Task whose data cannot be split still gives its agent
results between turns. The reward that agent sees is the score it is ranked by, so it can
overfit to the test set. It receives that reward, the round's status and the feedback
`tests/` writes, if any, as if `tests/` were copied into `validation/`, and its RunLock
records that `tests/` graded the rounds
([Trace](trace.md#verification-result)), setting such a score apart from one earned against
a held-out validation set. Run policy can give rounds to any Task, so a Task whose test set
must stay unseen has `validation/`. With `visibility = true` it is `tests/` that the agent
reads.

With `[agent].validation_rounds = N`, the agent works in up to `N + 1` turns: its first turn
ends, `validation/`, or else `tests/`, scores the state it left, the agent receives that
result as its next turn begins, and so on until `tests/` scores the state its last turn
left, once. A turn ends when the agent's program finishes normally, so the agent submits its
work by ending its turn; there is nothing else to call. A turn that a timeout, a limit or a
failure ends is the last. `[agent].timeout_sec` covers all turns together. Between turns the
agent's Sandbox is saved and a new one booted from what it saved, without setup: files on
its disk survive the end of a turn; files in memory, such as a tmpfs, and its desktop
session do not, nor does any process, the agent's or a service setup started. A round
therefore never sees a process the agent or setup left running. How the rounds run is in the
[Episode protocol](episode-protocol.md#validation-rounds).

Rounds need a Harness that resumes its native session, since every turn after the first
resumes it: a Run that gives rounds to one that cannot, such as the `computer-use` policy
Harness, is refused before provisioning, and can set `validation_rounds = 0` instead. Each
round costs a save of the agent's Sandbox, a verifier run under the verify limit
(`[verifier].timeout_sec`, or the Run's `timeouts.verify`) that its Judge calls share, and a
boot; none of them spends the agent's time. Since every calibration case checks the stage
that validates too, a Task with `validation/`, or with `[agent].validation_rounds` above 0,
cannot [calibrate](#calibration) on a Provider without snapshots.

The agent's model session is closed between turns, and no credential file stays on the
disk a round saves. The round's verifier has verification's open egress and, under shared or
snapshot verification, runs on the agent's disk, so a `validation/` that runs the agent's
program runs it with that network; with `[agent].sudo`, what the agent left there can write
the round's feedback and reward. The round's reward and status, and the feedback text of
the stage that validated, are all that reaches the agent
([Security](security.md#5-answers-are-absent-not-hidden)).

What the feedback text reveals is the author's decision: a score alone, the cases that
fail, or a diagnosis. Whatever it says, the agent can use, so it must not hold what `tests/`
checks for if the test set is to stay unseen.

## calibration/

Calibration cases are known final states with the scores each must receive, as a grader
reads anchor essays before marking: when `tests/` changes, `orbit calibrate` shows at once
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
`orbit lint` refuses any other entry there, a link or a `solution/` included. A directory
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

A Task with `validation/` checks it in every case too, and a Task without it whose
`[agent].validation_rounds` is above 0 checks `tests/` as a round runs it, with a validation
check rather than a round: `orbit calibrate` runs the stage that validates, `validation/` or
else `tests/`, once on the case's final state, before `tests/` scores it, with no turn after
it. `expect` applies to both, unless an entry's `expect_validation`, by the same rules, says
what `validation/`'s result must be; it is refused without `validation/`, where `expect`
already says what `tests/` gives. The check saves the oracle's Sandbox, as a round saves the
agent's, so such a Task needs a Provider that saves snapshots for calibration too, and under
shared verification `tests/` then runs in a Sandbox booted from that snapshot: like a round,
it sees no process the oracle left running.

How `orbit calibrate` runs the cases is defined in
[the episode protocol](episode-protocol.md#calibration).

## Harbor compatibility

Orbit also reads these Harbor keys and files, with Harbor's meaning, so that a Harbor Task loads
with few edits. They are not part of Orbit's format: a new Task uses the form the sections above
describe.

- `schema_version` may be left out, or name Harbor's `1.<n>`, or be Harbor's root-level
  `version`: Orbit reads the Task as a Harbor Task, in its own format with the defaults
  below, with a warning and `task.harbor` in the RunLock. Any other version fails.
- Without `[task].name`, a Task is named `<repository>/<folder>`; a name may use Harbor's
  characters, both cases and `.`.
- A Harbor Task's agent runs as `root` on Linux unless `[agent].user` names another
  account.
- A Harbor Task's containers run `sh -c "sleep infinity"` in place of their image's
  command, as Harbor's do.
- `docker_image`, in `[environment]` and `[verifier.environment]`, is `image`: one field, so a
  table holds either, never both.
- `[environment].workdir` is where the agent starts, unless `[agent].workdir` names another
  start, and where `tests/` and `validation/` run, their entries named by full path, so Harbor's
  verifiers find what they expect. `[verifier.environment].workdir`, and
  `[verifier.validation.environment].workdir` for validation rounds, replace it for a verifier
  with a Sandbox of its own. Setup and the oracle keep their stage directories.
- `[verifier].user` is the account, by name or uid, that `tests/` runs as on Linux and macOS,
  and `[verifier.validation].user` that of validation rounds; Windows refuses it, since verification there runs as Administrator. Orbit then
  gives that one account the stage directory and the verifier directory, which are otherwise
  the framework's alone, so a verifier running as an account the agent controls, such as
  `[agent].user`, can be changed by what the agent left: that is the Task's choice.
- `[environment.healthcheck]` runs its `command` right after setup, in the directory the agent
  starts in, under the framework's identity and setup's time limit, in `bash` (`cmd.exe` on
  Windows) with `[environment].env`. As in Docker's `HEALTHCHECK`, its first success passes, a
  failure counts only once `start_period_sec` (default 0) has passed, and `retries` (default 3)
  failures in a row fail the Episode as a Task error; attempts are `interval_sec` (default 5)
  apart, `start_interval_sec` (default 5) during the start period, and each may take
  `timeout_sec` (default 30). Orbit's form is a setup that starts the service and waits until it
  is ready.
- `network_mode` in `[environment]`, with its `allowed_hosts` or `blocked_hosts`, is the default
  of both the agent's and the verifier's network, and in `[verifier.environment]` that of a
  verifier with a Sandbox of its own, which is `public` when that table names no mode, as
  Harbor's separate environment is.
  `allow_internet`, Harbor's deprecated switch, stands for `public` when true and `no-network`
  when false, read only where its table names no mode. A phase's own `network_mode` always wins.
- `reward.txt`, where `reward.json` would be, holding one number and nothing else, is read as a
  `reward.json` of `reward` alone when there is no `reward.json`; `reward.json` wins when both
  exist.
- An MCP server's `transport` may be `sse`, and is `sse` when omitted; `http` means
  `streamable-http`. An `sse` server follows a `streamable-http` server's rules for its URL,
  network and `headers`. Each Harness configures it as Harbor's adapter for its agent does, and
  one whose agent has no SSE transport is refused before any Sandbox exists
  ([Harnesses](../../packages/orbit-run/src/orbit/run/harnesses/README.md#mcp-transports)).

Nothing in Orbit replaces Harbor's multi-step Tasks (`steps`, `multi_step_reward_strategy`),
compose sidecars (`docker-compose.yaml`, which `orbit lint` refuses, an artifact's
`service`, `[[verifier.collect]]`) or TPU selection (`tpu`), and a Task that sets one fails
to load. An origin belongs in `[metadata]`, so there is no root-level `source`.

These values differ from Harbor's:

- `validation/` and `[agent].validation_rounds` are Orbit's: a verifier whose results the
  agent receives between its turns; see [validation/](#validation).
- A Sandbox always has a size and a time limit, the same on every Provider so that scores
  compare, and its network stays closed until a Task opens it, so some defaults differ: `cpus`
  is 2, `memory_mb` 8192 and `[agent].timeout_sec` 1800, where Harbor leaves the size to its
  backend and sets no time limit; `[agent].user` is `user`, not the image's `USER`;
  `[agent].network_mode` is `no-network`, not `public`; `build_timeout_sec` is 3600, not 600.
  The agent runs in its Sandbox, so the default size leaves it a CPU beside the Task's work.
- `os = "windows"` is a VM with `.ps1` stage entries, because Orbit runs Windows in VMs,
  where PowerShell is always available; Harbor's is a Windows container with `.bat` entries.
- `os = "macos"` is Orbit's: a macOS VM with `.sh` stage entries, which Harbor does not run.
- `artifacts` may be globs, and nothing is collected undeclared, where Harbor always collects
  `/logs/artifacts`.
- `${NAME}` in an `env` table resolves from the checkout's `.env`, not the process environment,
  and an empty value counts as unset; see [Variables](#variables).
- A `skills_dir` that does not exist fails the Episode.
- `tests/Dockerfile` without separate verification fails, where Harbor ignores it.
- `environment_mode` also takes `snapshot`, Orbit's: a verifier booted from a snapshot of
  the solver; see [Verification topology](#verification-topology).
- The reference solution, `solution/` as in Harbor, is also the [calibration case](#calibration)
  `solution`; further cases live in `calibration/<name>/`, and a `calibration/solution/`
  fails as the Task loads.
- `gpu_types` holds at least one name and requires `gpus`, and every Provider honors it,
  where Harbor's Docker environment ignores it; see [GPUs](#gpus).
- `network_mode = "blocklist"` with `blocked_hosts` is Orbit's: every public host but those
  listed; see [Resources and network](#resources-and-network).
- `allowed_hosts` and `blocked_hosts` refuse entries that reach the Host; see
  [Resources and network](#resources-and-network).
- Only `reward`, a number from 0 through 1, is scored; see [Result](verification.md#result).
