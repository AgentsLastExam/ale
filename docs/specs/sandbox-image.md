# Container and VM sandbox image specification

Normative for standard container and VM Tasks. Every solver and dedicated verifier runs
in a container or a VM. A Linux container builds `environment/Dockerfile`; a VM builds
`environment/build.sh` (`build.ps1` on Windows) on the VM image `[environment].image` names;
an image named alone is used directly ([Source selection](#source-selection-and-identity)).

## What a Linux image needs

A Linux Task may build from any x86-64 image with glibc that has:

- the agent account the Task names with `[agent].user`, with a home; a VM build's base must
  have it already, since ALE installs the framework for it before the build runs;
- for a container, a long-lived command that ALE does not replace;
- for a VM, a qcow2 disk of a bootable systemd system (the VM base is one);
- for GUI work, a running desktop session, such as an X server with a window manager,
  that starts at the [desktop resolution](#desktop-resolution) it is given.

Everything ALE itself needs it adds when it prepares the image, as one more layer on top
of the Task's own build or the image it names (the framework layer):

- guestd, the guest service every sandbox operation goes through, under root-only
  `/opt/ale`, so it always speaks the protocol of the engine driving it;
- the framework's own Python at a pinned version, which guestd runs on whatever Python the
  image has (or lacks): `/opt/ale/python` on Linux and macOS, `C:\ProgramData\ALE\python` on
  Windows. guestd runs it isolated (`-I`), in a directory only the framework writes, and so
  does every root process guestd starts on it, so neither the image's `PYTHON*` variables,
  which a container's exec inherits, nor the agent's files change their modules or the
  buffering of guestd's replies. Only the framework's identity can run it, except on macOS,
  where the agent's desktop tool and Harness helpers run it too; the agent's PATH never
  includes it;
- Cua Driver at a pinned version, the one desktop capture and input implementation, which
  every account may run, because the agent's `cua-desktop` MCP server runs it too;
- for a VM, the boot-time unit that starts guestd: a systemd unit on Linux, a service on
  Windows that also starts Cua Driver in the agent's session; a macOS VM has none, since
  the Orchard Provider starts guestd through Orchard's exec for each connection;
- a check that the agent account exists, failing preparation if it does not;
- sudo, installed with the image's package manager when the image lacks it, so that any
  sandbox can be granted elevation; the grant itself is never part of the image
  ([Permissions](#permissions));
- a marker file recording which framework the image carries: `/opt/ale/framework` on
  Linux and macOS, `C:\ProgramData\ALE\framework` on Windows.

A Linux or Windows VM disk, used directly or as a build's base, gets the same pieces,
installed offline into a layer on the disk by the VM builder's libguestfs, so nothing on the
disk runs on the Host. On Linux its small appliance VM runs the disk's own programs that the
setup calls; on Windows the install is file and registry edits only. Each layer is made once
per disk and framework and then used directly, and a disk that already carries this
framework is used as it is.

An image or disk handed straight to a Provider, rather than prepared from a Task, gets the
same layer from the Provider when it does not carry this framework, so every sandbox runs
the guestd of the engine driving it however it was made.

There are no image labels to declare. Whether a sandbox has a desktop is decided from the
image's own configuration before anything starts: a VM has one when its systemd enables a
display manager (`display-manager.service`), and a container has one when its image sets
`DISPLAY` or `WAYLAND_DISPLAY`, which is how a desktop image tells its processes where to
draw. An image that ships X only as a tool, such as Xvfb for a headless browser, sets
neither and is headless; a desktop base can be made headless with `ENV DISPLAY=`. With a
desktop, the Provider waits until a screenshot succeeds and fails the sandbox if the
desktop never becomes ready; guestd starts Cua Driver inside the agent's desktop session
once its window manager or shell runs.

## What a Windows image needs

A Windows Task builds `environment/build.ps1` on an ALE Windows base. The base signs its agent
account in to a desktop automatically, with UAC on, and carries none of the framework; the
Python it installs for all users is for Tasks and agents. ALE installs the framework offline
into the build VM's disk, a layer on the base, before it boots, so the build talks to the
same guestd as every later sandbox, and the Task's script cannot replace the framework's
pieces:

- the current guestd under `C:\ProgramData\ALE\guestd`, Cua Driver at the pinned version
  under `C:\ProgramData\ALE\cua-driver`, and the framework's own Python;
- a boot service, running as the system on that Python, that starts guestd and Cua
  Driver in the agent's session with the account's elevated token, so Cua Driver can reach
  elevated windows. The service limits those files to the system and Administrators,
  except that every account may run Cua Driver, as on Linux;
- a check, before anything is written, that the disk has the agent account, signs exactly
  that account in, keeps UAC on and was shut down completely (not left hibernated by Fast
  Startup), each failing with the one thing it lacks.

Elevation for `[agent].sudo` is granted per boot by the Host, not built into the disk, as
on Linux ([Permissions](#permissions)). A Windows sandbox has a desktop for ALE to drive
exactly when its image carries the framework's Cua Driver, so the Provider waits for a
screenshot in every sandbox it boots.
How the install works, and what it costs, is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#qemu).

## What a macOS image needs

A macOS Task may use any Tart image that signs the Task's account in to its desktop
automatically. ALE adds the framework as it does to a Linux or Windows disk, offline into
a copy of the image, once per image, framework and account: guestd, a Python of the
framework's own, since a stock macOS has none recent enough, and Cua Driver with the
privacy grants macOS otherwise takes only through its settings. The copy is booted once so
macOS finishes the account's first login, whose one-time notices would otherwise appear in
every sandbox, and kept as the local image every sandbox starts from. A Task's `build.sh`
runs as root in a VM of its base framed for `admin`, the account ALE controls the VM
through, and the image it leaves is framed in turn for the Task's account, which the build
may create. Every macOS sandbox the agent works in shows that account's desktop.
Elevation for `[agent].sudo` is granted or taken away at each boot, as on Linux
([Permissions](#permissions)). What else the image must allow is in the
[Orchard Provider](../../packages/ale-run/src/ale/run/providers/README.md#orchard).

## Desktop resolution

The Run's `desktop_resolution` (`WIDTHxHEIGHT` from 1024x480 to 3200x1800, 1024x768 unless
set) is the size of every Sandbox's desktop in logical pixels, which are points on macOS, so
one value fits as much of a window on every system. It is never part of a prepared image, so
one prepared image serves every resolution: each Provider applies it as a Sandbox starts,
refuses a desktop that reports another size, and records it with the allocation in the
RunLock. A container's desktop is its image's own X server, so the image must start it at
the size `VNC_RESOLUTION` names, a name VNC desktop images share that asks for no VNC; a VM's
desktop starts at its display's preferred mode. How each Provider sets them is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#desktop-resolution).

## ALE base images

- `container-ubuntu22-base` provides an Ubuntu 22.04 GNOME desktop container and the
  `user` account.
- `vm-ubuntu24-base` is a published VM disk of Ubuntu 24.04 with systemd, full GNOME/GDM and
  the `user` account, for QEMU. It has no firewall of its own: network policy is enforced by
  the QEMU runner outside the guest
  ([security.md](security.md#1-a-sandbox-has-only-declared-ways-out)).
- The Windows 10 base provides a logged-in desktop, system Python, WinGet, Chocolatey, and
  the runtimes required by autonomous agents.

No base carries guestd or Cua Driver; ALE installs both when it prepares a Task image or
boots a disk. The bases do not contain domain stacks, benchmark data, databases, robotics
suites, or Task-specific tools.

Repository sources are separated by responsibility:

- `images/base/` contains the three Task foundations;
- `images/builders/vm-materializer/` is the VM builder. It makes the Linux base's disk from
  its root filesystem once per version, so initramfs, boot and partition assembly are never
  a Task's code; it also installs the framework into Linux and Windows disks and discards a
  VM build's free space;
- `images/runtimes/qemu-runner/` hosts prepared qcow2 disks under QEMU/KVM.

## Versions

Every ALE image is published once per version and never overwritten: the publishing
workflow skips a version the registry already has, and no `latest` tag is published, since
a tag that every change overwrote would change every checkout's next build behind its back.

The VM builder and the QEMU runner run the engine's own logic, so they are published under
the engine's version, such as `0.2.0`; an engine runs exactly those of its own version, and
a changed one ships with the next engine version. A base carries no part of the framework,
so nothing in it depends on the engine: each base has its own version, raised when the base
itself changes, and any engine runs any version of it. A container runtime that ALE builds
for Hosts to install depends on no engine either and has a version of its own.

A Task may build from any image. Task repositories maintained with ALE name an exact
version of an ALE base, such as `container-ubuntu22-base:0.1.1`, so that a Task keeps
building from the same base until its author moves it; `ale new-task` writes the base's
current version.

## Task image

A container image builds `environment/Dockerfile` with `environment/` as the sole ordinary
Docker build context, less `setup/`:

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:0.1.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY input.db /home/user/input/input.db
RUN mkdir -p /home/user/output \
    && chown -R user:user /home/user/input /home/user/output
```

The final stage starts directly from an ALE base. General third-party builder stages are
allowed, but a Task never inherits another Task's final image or an ALE domain image.
There is no Image Tree. Docker layer caching handles repeated installation without adding
a cross-Task contract.

`COPY .` cannot expose `setup/`, `tests/`, `solution/` or `calibration/`: the last three are outside the
build context, and ALE excludes `setup/` after the Task's own `.dockerignore`
rules, so a Task cannot bring it back. Files in the context, its asset root included, use
ordinary relative `COPY`; ALE creates no generated view or named BuildKit context.

## Source selection and identity

Before any solver model call, ALE prepares the Task's image; failure is terminal. The Task
folder decides how:

- `environment/Dockerfile` builds a Linux container. Its `FROM` names its base, so
  `[environment].image` beside it fails; on Windows and macOS a Dockerfile fails.
- A build script, `environment/build.sh` on Linux and macOS or `environment/build.ps1` on
  Windows, builds a VM on the VM image `[environment].image` names, which it requires.
- `[environment].image` alone is used directly, resolved by the Provider selected for it.

The Task folder decides every image's kind as the Task loads, so loading reads nothing
else: Windows and macOS images are VMs, a Linux absolute path is a VM disk, and only a Linux
image named alone by a registry reference may be either. Its manifest decides: an OCI
manifest whose `artifactType` is `application/vnd.ale.vm-disk.v1` is an ALE VM disk, a
container image's manifest or index (OCI or Docker) is a container, and anything else is
refused, naming its type. The Run reads each such manifest once, with the Host's Docker
credentials, before it selects a Provider, places a Sandbox or records anything about the
image, since each of those depends on the kind; [Fleet](fleet.md) Nodes receive the kind
decided. A digest-pinned reference the Host already holds, as a pulled disk or a Docker
image, is answered without the registry. An image only the local Docker daemon holds cannot
be named: nothing could pin it and no other Host could run it. Container and VM images are
both recorded immutably.

On Linux and Windows, `[environment].image` may also be an absolute host path to a
standalone qcow2; a macOS image is always a Tart OCI reference. Local disks are structurally
checked and their actual file bytes are SHA-256 hashed at preparation, without contacting a
registry. Disks with backing or external data files are rejected: ALE knows a layer's parent
only for the layers it makes, so a disk it is handed must hold its whole content. The
operator must keep prepared local disks unchanged while they are in use.

Any other Linux or Windows VM image names a VM disk published to an OCI registry with
`ale vm-image push`, and its identity is the manifest digest. The standalone qcow2 travels
as fixed-size chunks, each compressed alone as one layer, because a whole disk in one layer
approaches registries' documented limits on a layer and an upload, and downloads over a
single connection, which reaches about half the throughput of several. Every chunk is
checked against the digest of its uncompressed bytes, and a downloaded one also against its
layer digest, before the disk is used. The published qcow2 is itself uncompressed, so the
one copy a Host pulls, once per manifest digest, is also the base of the layers every Task
builds on it, read without decompressing. Its format is in the
[Providers README](../../packages/ale-run/src/ale/run/providers/README.md#registry-disks).

A Provider may keep a prepared VM disk as a chain of layers, each holding only what changed
on the one below, rather than flattening it, so a Host stores a base once under all the
Tasks built on it; the QEMU Provider keeps a Linux or Windows Task image and a snapshot
that way ([Providers](../../packages/ale-run/src/ale/run/providers/README.md#disk-layers)).
A layer's identity is the hash of its parent's identity and the SHA-256 of its own bytes, as
an OCI ChainID is; the bottom disk's is the identity it was resolved under, whatever form a
Host stores it in. Each layer is hashed once, when it is made, and a chain in which a layer
sits on another parent than the one it recorded is refused, so the top layer's identity
covers the whole disk without hashing it whole.

The authored image source digest covers the build context except its asset root ([Task
assets](task-assets.md)) and `setup/`, so normal Task identity and asset provenance remain
separate and a setup change never rebuilds the image. A prepared container image is recorded
under the same context's file contents, its asset inventory (path, size, modification time
and mode, as for asset dirtiness) and the framework identity. An unchanged Task finds its
image there and uses it directly, without asking Docker to build or check anything, so a
base tag that moves is not followed: name exact base versions. VM build cache keys include
the actual image asset bytes. Dirty or unsynchronized assets disable resume and reporting.
RunLock schema 3 records declaration, preparation, Provider, and exact observed identity; it
does not invent a whole-disk content digest. A VM built from the Task folder records its
builder identity and resolved base materials, and a layered disk's prepared identity is its
top layer's.

## Reverification

Image source identity describes the files of the build context, separately from the exact
built image recorded in RunLock. It is not the decision to reuse a solver's answer.
A Task run records `task.non_verifier_digest`: a content hash of every Task file outside
`tests/`, `solution/` and `calibration/` (paths, executable bits and link targets), with `task.toml` read
without its `[solution]` and `[[calibration]]` tables, computed once per Task folder. None
of these reaches an evaluated agent, so changing verification, the reference solution or
another calibration case keeps a retained solver reverifiable. `validation/` is part of the
digest, because the agent receives its results. The asset roots and engine-owned
`.ale-cache` are excluded, so loading a Task never reads asset bytes. File timestamps do not
enter this hash. Reverification requires this digest to match the source episode and the
Task's asset observation to equal the source RunLock's: the same repository, Task path and
commit, clean on both sides. Any outside change, any asset change (asset commits do not
distinguish `tests/assets` from the other roots), or missing source evidence requires a
fresh solver run. This also protects direct CLI use.

When only verification changed, ALE does not build or resolve the solver image again.
It attaches the retained solver using the source RunLock's exact image identity, takes the
image's kind from that RunLock rather than the registry, and prepares only a separate
verifier image when needed. A solver image declared otherwise than in the source RunLock
requires a fresh solver run. Provider attachment remains strict;
missing retained resources are not replaced with a different sandbox. The new RunLock
keeps the actual solver image and records the new verifier. No image-config or layer
comparison is needed. New builds resolve immutable output digests from Buildx metadata,
not mutable tags that concurrent builds can overwrite.

## VM preparation

A Linux or Windows Task image is built in a temporary VM whose disk is a framed layer on
the resolved base. ALE stages only `environment/` less `setup/` and executes `build.sh` or
`build.ps1` there under the framework's identity, with network access for unattended
installation. The script has `build_timeout_sec`, one hour by default, for installation,
integrity checks and extraction; Host-to-VM staging happens before that timer starts. ALE
removes the build materials, shuts down the VM, and keeps its disk as the Task's layer on
the base. Linux and Windows share this one path, its cache and its layers, and a Task never
assembles boot or a disk itself. Reuse covers the whole build and is keyed by base,
framework, builder, and every file staged; there is no per-command cache or new recipe
language. Authoring details belong to [Task authoring](task-authoring.md#environment).

Windows 10 has no public ISO build path in ALE. The current Windows base starts from the GCP
image `agenthle-win10-base-0210`; the maintained `images/base/vm-windows10/prepare.ps1`
recipe installs and pins ALE's cross-platform components, and `compact.sh` checks the
resulting qcow2 and rewrites it uncompressed. Episodes always cold-boot a fresh overlay,
over a retention snapshot when they continue one. ALE creates no ready snapshot or warm
pool; the [Fleet](fleet.md) places Sandboxes on other machines but starts none before a
request.

A macOS Task image builds the same way on the local Orchard: `build.sh` runs as root in a
VM of the base framed for `admin`, and the stopped VM is kept as a local Tart image on the
Mac ([What a macOS image needs](#what-a-macos-image-needs)).

Software assets are deployed during image construction and saved into the Task image.
An episode overlay isolates disk changes while preserving Windows permissions and package
execution rules. Use each asset's qualified deployment recipe; see the
[Windows Task image guide](../guides/windows-task-images.md).

For assets downloaded by the image script, ALE hashes the authored reference, not unseen remote
bytes. Pin and verify the expected digest before deployment; changing that pin invalidates reuse.

GUI image qualification includes a real visual agent completing a task whose decisive
input exists only on screen, followed by inspection of its canonical trajectory,
screenshots, desktop actions, artifact, and reward. Windows base qualification also checks
autonomous agent installation, execution, and Gateway access. An oracle-only run checks
plumbing but does not establish agent readiness.

The QEMU Provider sizes a fresh overlay from `storage_mb`, boots it over the prepared
qcow2, grows the guest root filesystem, waits for guestd and the declared desktop,
and observes usable root capacity. The guest health contract reports its OS, agent user,
agent home, and whether it has a desktop; a Task whose `os` disagrees with the image is
rejected.
Every TCP guest connection authenticates with the sandbox token claimed by the framework's
first connection ([security](security.md)); an image whose guest
service predates authentication fails before provisioning completes and must be rebuilt.
Tasks may install ordinary OS packages and services, including Docker; VM boot machinery
stays engine-owned.

## Stable state versus setup

Put these in the image:

- fixed package installation and compilation;
- stable service installation and configuration;
- static solver-visible input;
- filesystem layout and permissions;
- normal image-started services.

Use `environment/setup/` only for per-episode work that cannot be frozen: mutable
reset/seeding, generated values, writable copies, dynamic service start/reset, and readiness
checks. Setup is not a second image builder.

Small static data may be committed anywhere in the build context. Large data is a
[Task asset](task-assets.md) restored by `ale assets pull`, then the same image build path.
Runtime never downloads it.

## Verifier images

Shared verification uses the solver image, and a snapshot verifier boots from a snapshot of
the solver, so it has the solver's image too. Separate verification uses one of:

1. `[verifier.environment].image`, used directly and resolved through the verifier's
   Provider; its kind follows the [solver's rules](#source-selection-and-identity) under
   the Task's `os`;
2. on Linux, a container built from `tests/Dockerfile` with `tests/` as context, which
   requires separate verification and fails a Windows or macOS Task;
3. otherwise the very image the solver runs, checked by the verifier's Provider when that
   is not the solver's, so the two never resolve a tag apart. Where either is the Fleet,
   which prepares images on its Nodes, the verifier's Provider prepares it again from the
   solver's declaration: a named image at the digest the solver's resolved to, unless the
   solver is on the Fleet, whose Node resolves the tag only after the Run has prepared its
   images, so the verifier resolves it again; a built image is built again, so a build that
   is not reproducible can give the verifier another image.

Naming an image beside `tests/Dockerfile` fails, as the Dockerfile names its base in `FROM`,
and a verifier never builds on a base. The local verifier context is fixed at `tests/`; its
Dockerfile is the declaration. Named images are pulled and resolved to an immutable
repository/content identity before solver execution.

Any verifier image must satisfy the same guest contract and permit ALE to stage
`ale_verify`. Verification runs as framework-controlled Task code.

## Runtime behavior

Each Sandbox owns two fixed guest connections: one for commands and one for file
discovery and reads. A long-running command must not block periodic log collection.
Docker uses two stdio guest sessions, Daytona two over SSH, Orchard two over its exec
WebSocket; QEMU and Firecracker use two authenticated connections to the same guest server
and port.
Providers close both on startup failure, retention, destruction, and image save.

`Sandbox.list_files(path)` returns the regular files at or below an absolute path, sorted,
with byte sizes; a missing path returns none. `Sandbox.read_file` reads a regular file or
a byte range of one, and `Sandbox.file_digest(path, length=n)` returns the SHA-256 of a
file's first `n` bytes, computed in the guest, so a caller can check that a copy it holds
is still a prefix without transferring it again. None of the three passes through a
symbolic link or Windows reparse point anywhere on its path, and a listing skips those
below it: the agent may replace any directory below its home with a link, and one could
point at a file only the framework may read. On Windows a file cannot be deleted or
renamed over while the service holds it open; reads are short, and the shipped CLIs
append to their session files rather than replacing them.

Given a conforming image, the Docker Provider:

- starts the exact prepared content without replacing its command, adding the framework
  layer first to an image that was not prepared with it;
- uses the Task's agent account and, when the image has a desktop, starts it at the Run's
  [resolution](#desktop-resolution) and waits for it;
- applies CPU, memory, writable storage, network, sudo, and GPU requests or rejects them;
- injects only selected NVIDIA devices through the configured runtime/CDI path;
- observes the resulting image, runtime and resource allocation, and refuses a container
  that ran otherwise than its leaf asked, so a Host's defaults never stand in for what the
  RunLock records
  ([Providers](../../packages/ale-run/src/ale/run/providers/README.md#docker-runtimes));
- labels every ALE container with episode, physical role, requested retention, GPU
  identity and, on a GPU it shares, the memory it reserved there;
- destroys by default, or after framework sanitation returns a retained handle or saves a
  snapshot.

The QEMU Provider applies the same retention contract to its runner container and the
episode qcow2 overlay mounted into it. `keep` leaves both alive and discoverable;
`ale sandbox destroy HANDLE` removes both. `snapshot` keeps the guest's disk as a layer on
its Task's disk. Provider-qualified QEMU handles prevent the runner from being mistaken
for a container sandbox.

QEMU and Firecracker enforce Task egress in the runner network namespace, outside the
guest. The guest does not need an OS-specific firewall implementation for ALE policy.

The Firecracker Provider runs a container Task's prepared image as a microVM with its own
kernel, under the same contract as the Docker Provider; how is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#firecracker).

The Daytona Provider runs Linux container images, with or without GPUs, on Daytona's
cloud under the same contract; how it meets it there is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#daytona). Two of its
differences reach other modules: the RunLock's observed image is the digest pushed to
Daytona's registry, and memory and writable storage must be whole GiB, refused rather
than rounded.

Retained handles are always provider-qualified: `docker:<runtime-id>` for containers,
`qemu:<runtime-id>` for QEMU VMs, `firecracker:<runtime-id>` for Firecracker microVMs,
`daytona:<sandbox-id>` for Daytona, `orchard:<vm>:<uid>` for Orchard VMs and `fleet:<sandbox>`
for a Sandbox kept on the [Fleet](fleet.md#lifecycle), whichever leaf its Node used.
Unqualified handles are rejected. Listing, destroying and attaching are Provider
operations, routed by the Provider a handle or RunLock names, so a new backend needs no CLI
change, apart from the Fleet, which `ale sandbox` reaches at `$ALE_FLEET_URL` and
`--continue-from` through the Run's own Fleet connection.

GPU workload libraries such as CUDA, PyTorch, and simulators, and the Vulkan ICD and GLVND
EGL vendor files a renderer loads, remain the Task image's responsibility: not every
injection path mounts those files. Tasks request a count and the models they accept
([GPUs](task-authoring.md#gpus)). Provider configuration may narrow the devices a Provider
leases, such as Docker host indices, but never widens the models a Task accepts; ALE
records actual UUIDs and models. QEMU GPU passthrough remains a Provider capability, not a
Task image declaration.

On every Provider a GPU sandbox gets the Host's whole NVIDIA driver, as far as the driver
has each part (CUDA, Vulkan, EGL, the video codecs), registered with the sandbox's linker,
so a Task finds the same driver whichever backend runs it. The Provider, not the image,
decides which Host driver files enter the sandbox, because an image would otherwise choose
what of the Host reaches it. How each Provider does it is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#gpu-driver-capabilities).

### GPU memory

A Sandbox whose Task names `gpu_memory_mb` ([GPUs](task-authoring.md#gpus)) may share each
of its GPUs with other such Sandboxes; one that names none takes its GPUs whole. A
capability profile says how a leaf holds such a Sandbox: shared under a limit, shared and
watched, or whole, and gives each GPU model's memory where the leaf knows it: for a leaf
that shares GPUs, the most it admits a Sandbox with on an empty GPU of that model, so that
selection never chooses a leaf whose admission could not. A leaf serves such a Sandbox only
with a model whose memory, where it knows it, is at least the need; a leaf that gives it
whole GPUs gives it only such a model. A leaf that shares a GPU:

- admits a Sandbox to it only while no Sandbox holds it whole and its headroom, the memory
  free there less what the Sandboxes sharing it reserved and do not use yet, leaves the
  need and a margin, and decides under one lock, which every process admitting through the
  same container daemon takes, until the Sandbox has started, so that two never take the
  same memory. The margin is for other tenants' jobs,
  which grow after they start; a refusal names each GPU it considered and why;
- holds a Sandbox to its share with a limit where its runtime has one, under which an
  allocation past it fails as on a full GPU and the Sandbox goes on. Where none does, it
  watches what the Sandbox uses while a Run holds it and kills the Sandbox's largest process
  on a GPU past its share, as the OOM killer kills a process past a memory limit: memory
  past a share is memory another Sandbox was admitted with, and going past it is the
  Sandbox's own doing, so the episode goes on with what the agent makes of it rather than
  ending in an environment error that would rerun or unscore it. Where a limit's
  accounting fails, which is ALE's failure, it stops the Sandbox and the episode ends in an
  environment error that says what used how much. Every selection says which leaves only
  watch, and for how many Sandboxes;
- records in the RunLock what it reserved on each GPU, how the Sandbox is held to it, and
  each GPU's total and free memory when it admitted the Sandbox.

How each leaf does it is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#gpu-memory).

## Permissions

Setup, artifact collection/restoration, and verification use the framework's elevated
identity: root on Linux and macOS, Administrator on Windows. This applies in every
verification mode, independently of the agent's `[agent].sudo` setting.

Directory uploads ignore Host permissions: POSIX files use `0644`, directories `0755`,
owned by the selected framework or agent identity. Windows resets ACLs to grant
SYSTEM/Administrators full control, Users read access, and the selected agent modify
access for agent uploads. Task entry scripts run through their interpreter, without
requiring executable bits. Artifact collection/restoration does not use these defaults.
Every Provider streams the tree through guestd's `put_tree`, which applies these rules as
it writes, so they have one implementation rather than one per backend. Links arrive as
links; a link or hard link already at a destination is replaced, never written through,
because the writer is root. Single-file writes, `mkdirs` and `remove` treat a link at
their path the same way and leave its target alone; their parents, like an upload's,
are framework-chosen paths and resolve normally.

Solver and oracle use the image's agent account. Windows guestd runs elevated in that
account's interactive session and launches agent commands with the desktop's limited
token, unless the Host supplies the existing SMBIOS `ale-sudo` grant. UAC and a limited
desktop session are required; the Provider checks both framework and agent privileges
before Task execution. A Windows Task image is rebuilt when the framework changes, and a
disk named directly by `[environment].image` is framed like any other.

Elevation is never part of an image. On every system the Provider sets it as every sandbox
the agent works in starts, before Task code runs, so one prepared image serves a Task with
and without `[agent].sudo`, its solver and its verifier alike, and a Sandbox continued from
a snapshot gets what its own request asks for. On Linux and macOS the root guestd grants the
agent's account elevation, or revokes every way up through sudo and the groups that make
administrators; how is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#agent-elevation-on-linux-and-macos).
Nothing can keep a root account from elevating, so a root agent is elevated: its
`[agent].sudo` is true unless written, a Task writing it false fails to load, and the agent
needs no sudo program. Grants, and their absence, are verified and recorded: a
Linux or macOS sandbox the agent works in is refused when its guest service is not root,
when a requested grant does not work, or when an agent that did not request one can still
use sudo.

ALE does not pre-create artifacts or repair their ownership: image/setup/agent code owns
their type and existence.

## Provider selection

A Task states what its Sandboxes need and never names a Provider or runtime, so it runs
unchanged wherever a Provider serves it. A Provider is one of two classes. A
[leaf](lexicon.md) starts and drives the Sandbox itself, locally or in an allocation, and
declares each real combination it can start as a [capability profile](lexicon.md): kind,
operating system, architecture, desktop, network modes, snapshots and how many it saves in a
row, GPUs, their models and memory and how it shares them, CPU and memory limits, whether it
holds a Sandbox to its `storage_mb`, container features, and whether it mounts
[volumes](task-authoring.md#volumes). A scheduler places Sandboxes on machines it manages,
which start them with the leaves there, and offers what those leaves do: the Fleet those of
the leaves its registered Nodes lend, Daytona the one of its own Sandboxes. Which leaves a
scheduler uses, and in which order, is its own configuration, never a Run's.

A Sandbox needs its image's kind and operating system, the architecture that system implies
(arm64 for macOS, x86-64 otherwise), its network mode (a verifier's is the verifier's, and a
solver that serves a shared verifier needs the least open of the agent's and the verifier's,
since it applies both), its resources (CPUs, memory, the
storage it may write, GPUs, the models it accepts and the memory it needs on each, container
features), snapshots, and how many in a row, when it saves or boots from one, and volume
artifacts, when its Task has any. One rule compares needs with a profile, naming each need
the profile misses, wherever a Provider or Node is chosen: in a Run's selection, in every
leaf's `create`, and in the Fleet's placement, so a Sandbox is accepted or refused the same
way wherever it is asked for. A leaf may also refuse by rules no profile states
([Providers](../../packages/ale-run/src/ale/run/providers/README.md#provider-ids-and-profiles)
lists them); they hold wherever it runs, so selection asks them of a leaf, and of the leaves
a scheduler would use, by the method `create` asks. A request that `create` would refuse is
thus refused before the Run starts.

The Run's `sandbox.providers` lists provider ids in order of preference. Each Sandbox, a
Task's solver and a separate verifier by its own needs, goes to the first Provider with a
profile that serves it and no rule of its own against it. A snapshot verifier and its solver
go together to the first with one profile that serves both, since only the leaf that saved
a snapshot boots from it: the solver's needs carry its verifier's, and every check of them,
a leaf's own and the [Fleet](fleet.md)'s placement included, holds one leaf to both. The
choice is by capability alone, never by load, so one configuration always chooses the same
way. Every Task is selected once the kinds of its images are decided and before any image
is prepared or Sandbox started. A Run with a Sandbox that no listed Provider serves is
refused whole, naming each such Task and role and why each Provider does not serve it,
rather than failing its episodes one by one.

Each [validation round](episode-protocol.md#validation-rounds) saves the solver and boots
the next from the snapshot, on the same leaf, so a solver plans its saves: one for each
round still to come, or for calibration's validation check, and one at its end only when
it is kept as a snapshot or saved for a snapshot verifier. A shared solver is never saved at
its end for its verifier. Selection compares that count with how many snapshots in a row a
profile saves (`max_saves`), and a leaf that saves fewer in a row is not chosen. Under
shared verification a solver with rounds also carries the needs of the round's verifier,
which boots from its snapshot with the solver's resources and no sudo, and of
`[validation]`'s snapshot verifier when that places one. Volumes ask nothing of selection:
a separate verifier mounts views of its solver's volumes only where, as the episode finds,
the solver's leaf serves it and saves the solver once more for it, and a Sandbox's
volumes are copied otherwise.

Before any episode starts, the Run has each selected leaf run its preflight with the needs
of the Sandboxes selected for it, which finds what the leaf runs those with, such as its
container runtimes, and again only for needs a later selection adds. Then, for each set of
container features its selected Sandboxes ask for, the leaf starts one throwaway Sandbox as
it would the first that asks for that set, and the Run probes each feature in it as root;
one it lacks refuses the Run, naming the leaf and the feature. What only a running Sandbox
shows of the Host, such as that it runs on the runtime the leaf names, the preflight leaves
as a check, which runs once, in the first such Sandbox, or in one started for it alone when
no Sandbox asks for a feature. A profile says what a leaf gives; only the Host shows that it
gives it here, where a kernel setting or a missing device can take a feature away. Each set
is probed on its own because a grant one feature brings can make another pass. A scheduler's
leaves run on machines the Run does not reach, and the Run checks none of them.

A prepared image belongs to the Provider selected for it, so one image is prepared once for
each Provider that runs it. A Sandbox kept or snapshotted is reached only through the
Provider that kept it, so a continuation's solver needs what booting it from a snapshot
needs, and is refused before any image is prepared unless the Run selects that Provider
for it. The RunLock records, for the solver and a separate or snapshot verifier, the
Provider selected and the leaf that started the Sandbox
([Security](security.md#7-a-result-carries-what-produced-it)).

## Provider contract

A Provider turns a SandboxRequest into a running Sandbox that holds the guarantees below,
or rejects the request before anything is created; it never degrades one silently. It owns
isolation (resources, network, identity, enforced where the agent cannot reach), the
channels (Host to guestd, sandbox to Gateway), the lifecycle (create, destroy, retain,
snapshot, attach) and truthful observation. It does not own phase timing (the episode
protocol), command and file semantics (guestd), credentials (the Gateway) or image and
Task content (the engine).

| Guarantee | Enforced by | Conformance test |
|---|---|---|
| It declares its capability profiles, and refuses, before creating anything, a request that they or its own rules do not serve, by the check the Run's selection made | Provider: `profiles` and `refusals`; a leaf's `accepts` in `create`, the Fleet's placement | `test_declares_profiles`, `test_refuses_up_front_a_container_feature_none_of_its_profiles_gives`, `test_rejects_a_request_it_cannot_serve` |
| Every sandbox runs the framework of the engine driving it, whatever image or disk it was handed | Provider in `create` | `test_a_sandbox_carries_the_engines_framework` |
| Agent work runs as `[agent].user`, framework work as root or Administrator; the agent elevates exactly when `[agent].sudo` asks, whatever image or snapshot it starts from | Provider in `create`: set at every start of a Sandbox an agent runs in, then checked both directions | `test_the_agent_and_the_framework_are_different_accounts`, `test_storage_and_sudo_are_enforced_or_rejected`, `test_one_image_serves_an_agent_with_and_without_elevation`, `test_a_snapshot_boots_new_sandboxes_with_its_files` |
| Uploads land with the fixed modes and owner of their identity | guestd `put_tree` (Windows: Provider ACL reset) | `test_uploads_ignore_host_modes_and_belong_to_their_identity` |
| File reads, listings and digests proceed while a command runs and never pass through a link | a second guest connection from the Provider; guestd | `test_file_reads_progress_while_a_command_is_running` |
| Replies of any size arrive whatever Python variables the image sets | the Provider starting guestd isolated; guestd writing each reply whole | `test_large_replies_arrive_whatever_python_the_image_configures` |
| After `open_egress` a Sandbox has the network; after `close_egress` it reaches its Gateway (and, for `allowlist` or `blocklist`, the hosts the episode's egress session admits) and nothing else, whichever phase sealed it; sealing a sealed Sandbox and opening an open one leave it so; an `open` request is never sealed | Provider, outside the agent's reach | `test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else`, `test_a_task_that_declared_open_is_never_sealed` |
| Every Sandbox reaches its Gateway at the same fixed port, whatever port the Host serves it on, and a reattached Sandbox reaches a later run's Gateway there: an agent may keep the address in its native session | Provider, outside the agent's reach | `test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else`, `test_a_reattached_sandbox_reaches_a_later_gateway_at_the_same_address` |
| Resources are applied or rejected; the RunLock records the observed image and allocation, with the Provider selected for the Sandbox and the leaf whose profile served it | Provider | `test_ready_sandbox_reports_observed_image_and_resources`, `test_rejects_a_request_it_cannot_serve` |
| A container Sandbox that asks for the container features a profile declares has each of them, as root | Provider in `create`; the Run's preflight on the Host | `test_gives_every_container_feature_it_declares` |
| A process past the memory the Task declares is ended alone where the kernel can, and the Sandbox goes on; a Sandbox, or its guest service, lost after an OOM kill in it since it started raises `MemoryExhaustedError`, whether the kill was for the Sandbox's memory or the Host's, with what showed it; where nothing records the kill, the loss is a lost guest ([how each Provider tells](../../packages/ale-run/src/ale/run/providers/README.md#memory)) | Provider, explaining a lost guest connection | `test_one_process_past_the_sandboxs_memory_dies_alone`, `test_a_sandbox_lost_to_its_own_memory_says_so` |
| A Sandbox that names `storage_mb` is held to it, or refused: by writable storage of its own, at least that size, where a write past it fails as on a full disk and the Sandbox goes on; or by measuring from outside the Sandbox, never by asking it, all that has been written over its image, and ending it once that passes `storage_mb`, after which its calls raise `StorageExhaustedError` ([how each Provider does](../../packages/ale-run/src/ale/run/providers/README.md#provider-ids-and-profiles)) | Provider, outside the agent's reach | `test_a_sandbox_is_held_to_its_storage` |
| Destroy is idempotent; retained sandboxes can be listed and destroyed through their Provider | Provider | `test_destroy_is_idempotent`, `test_a_retained_sandbox_can_be_listed_and_destroyed` |
| A Provider that can names each Sandbox by a `handle`, by which another process on the same machine drives it again (`reattach`) or destroys it (`discard`) after the creating process is gone, whatever its retention ([which Providers do, and how](../../packages/ale-run/src/ale/run/providers/README.md#reattaching-by-handle)) | Provider | `test_a_sandbox_is_driven_again_by_its_handle` |
| A snapshot holds the disk, any number of Sandboxes boot from it, each with its own request's role and resources, its `storage_mb` counting what the snapshot holds, and it is listed and destroyed through its Provider, never under a Sandbox booted from it; a Provider without snapshots refuses them before provisioning | Provider | `test_a_snapshot_boots_new_sandboxes_with_its_files` |
| A declared desktop can be captured and driven | Provider and image | `test_a_declared_desktop_can_actually_be_used`, `test_a_declared_desktop_accepts_input` |
| A desktop has the Run's resolution, whatever the image would use, and the RunLock records it | Provider in `create`: set at every start, then checked | `test_a_declared_desktop_can_actually_be_used`, `test_a_desktop_takes_the_runs_resolution` |
| No platform service inside the sandbox gives the agent more than its account | Provider check in `create` | `test_the_agent_cannot_reach_the_platform_daemon` |

`open_egress` and `close_egress` are abstract: a default that did nothing would let a new
backend run the agent, or a verifier, with the network while its RunLock said `block`. A
backend that cannot change policy at runtime raises there, and one that cannot apply a policy
leaves its mode out of its profiles. A Provider is complete when it passes
`ale.core.testkit.ProviderConformance`; an escape the suite cannot observe, such as a
platform's own in-sandbox service, is that Provider's to close and to check in `create`.

## Base-image changes

Base build sources live in the ALE engine repository and are Provider-conformance tested.
Changing them changes every Task foundation and requires image contract tests. Task
contributors add dependencies to their own `environment/` rather than adding domain bases to ALE.
