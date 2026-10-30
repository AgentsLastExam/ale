# Container and VM sandbox image specification

Normative for standard container and VM Tasks. Every solver and dedicated verifier
declares `image.kind`. Linux uses a Dockerfile or `image.ref`; Windows uses `image.ref`
directly, or builds `image/run.ps1` on `image.base_ref`.

## What a Linux image needs

A Linux Task may build from any image that has:

- `python3` for the guest service (3.12+ in containers); Linux VMs may retain
  Python 3.10+ when `python3.12` is available for verification;
- the agent account the Task names with `image.user` (default `user`), with a home;
- for a container, a long-lived command that ALE does not replace;
- for a VM, a bootable systemd system (the VM base provides one);
- for GUI work, a running desktop session, such as an X server with a window manager.

Everything ALE itself needs it adds when it prepares the image, as one more layer on top
of the Task's own build or `image.ref` (the framework layer):

- guestd, the guest service every sandbox operation goes through, under root-only
  `/opt/ale`, so it always speaks the protocol of the engine driving it;
- Cua Driver at a pinned version, the one desktop capture and input implementation;
- for a VM, the boot-time unit that starts guestd: a systemd unit on Linux, a service on
  Windows that also starts Cua Driver in the agent's session;
- a check that the agent account exists, failing preparation if it does not;
- working sudo for that account when the Task sets `resources.sudo: true`, installing sudo
  with the image's package manager when the image lacks it;
- a marker file recording which framework the image carries: `/opt/ale/framework` on
  Linux, `C:\ProgramData\ALE\framework` on Windows.

A VM disk named by a qcow2 `ref` gets the same pieces, installed offline into a copy of
the disk by the VM builder's libguestfs, so nothing on the disk runs on the Host. On Linux
its small appliance VM runs the disk's own programs that the setup calls; on Windows the
install is file and registry edits only. Each copy is made once per disk and framework
and then used directly, and a disk that already carries this framework, such as one ALE
prepared, is used as it is.

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

A Windows Task builds `image/run.ps1` on an ALE Windows base. The base signs its agent
account in to a desktop automatically, with UAC on and Python 3.12 or newer installed for
all users, and carries none of the framework. ALE installs the framework offline into a
copy of the base before the build VM boots, so the build talks to the same guestd as every
later sandbox, and the Task's script cannot replace the framework's pieces:

- the current guestd under `C:\ProgramData\ALE\guestd`, and Cua Driver at the pinned
  version under `C:\ProgramData\ALE\cua-driver`;
- a boot service, running as the system, that starts both in the agent's session with the
  account's elevated token, so Cua Driver can reach elevated windows. The service limits
  those files to the system and Administrators;
- a check, before anything is written, that the disk has the agent account, signs exactly
  that account in, keeps UAC on, has the Python and was shut down completely (not left
  hibernated by Fast Startup), each failing with the one thing it lacks.

Elevation for `resources.sudo` is granted per boot by the Host, not built into the disk.
A Windows sandbox has a desktop for ALE to drive exactly when its image carries the
framework's Cua Driver, so the Provider waits for a screenshot in every sandbox it boots.
How the install works, and what it costs, is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#qemu).

## ALE base images

- `container-ubuntu22-base` provides an Ubuntu 22.04 GNOME desktop container and the
  `user` account.
- `vm-ubuntu24-base` provides Ubuntu 24.04, systemd, full GNOME/GDM and the `user`
  account for QEMU. It has no firewall of its own: network policy is enforced by the QEMU
  runner outside the guest ([security.md](security.md#1-a-sandbox-has-only-declared-ways-out)).
- The private Windows 10 BYOL base provides a logged-in desktop, system Python, WinGet,
  Chocolatey, and the runtimes required by autonomous agents.
  Its licensed disk is not a public ALE artifact.

No base carries guestd or Cua Driver; ALE installs both when it prepares a Task image or
boots a disk. The bases do not contain domain stacks, benchmark data, databases, robotics
suites, or Task-specific tools.

Repository sources are separated by responsibility: `images/base/` contains the three
Task foundations, `images/builders/vm-materializer/` turns Linux VM OCI content into
qcow2 and installs the framework into a Linux or Windows qcow2 named by reference, and
`images/runtimes/qemu-runner/` hosts prepared qcow2 disks under QEMU/KVM.

## Versions

Every ALE image is published under the engine's version, such as `0.1.1`, once and never
overwritten: the publishing workflow skips a version the registry already has, and no
`latest` tag is published. An engine runs exactly the images of its own version, so a
checkout that stays on one engine version keeps the same images whatever later versions
publish. Publishing a changed image therefore means releasing a new engine version.
A tag that every change overwrote would carry one engine's change into every checkout's
next build, including checkouts whose engine does not speak the new guest protocol.

A Task may build from any image. Task repositories maintained with ALE name an exact
version of an ALE base, such as `container-ubuntu22-base:0.1.1`, so that a Task keeps
building from the same base until its author moves it; `ale new-task` writes the
engine's current version.

## Task image

For a local Linux image, `image/` is the sole ordinary Docker build context:

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:0.1.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY assets/input.db /home/user/input/input.db
RUN mkdir -p /home/user/output \
    && chown -R user:user /home/user/input /home/user/output
```

The final stage starts directly from an ALE base. General third-party builder stages are
allowed, but a Task never inherits another Task's final image or an ALE domain image.
There is no Image Tree. Docker layer caching handles repeated installation without adding
a cross-Task contract.

`COPY .` cannot expose `setup/`, `verify/`, `oracle/`, or `tools/` because none are in the
build context. `image/assets` is already inside that context and uses ordinary relative
`COPY`; ALE creates no generated view or named BuildKit context.

## Source selection and identity

Before any solver model call, ALE builds an existing Linux Dockerfile or Windows
`image/run.ps1`; failure is terminal. Windows scripts require `image.kind: vm` and use
`image.base_ref` as the base. `image.ref` always names an image for direct use and is
mutually exclusive with `base_ref`. A base ref requires the Windows solver image script;
it is unsupported for Linux and separate verifier declarations. Without local build
instructions, the matching Provider resolves the declared ref. Container and VM refs are
both supported and recorded immutably.

A VM `ref` or Windows `base_ref` may also be an absolute host path to a standalone
qcow2. Local disks are structurally checked and their actual file bytes are SHA-256
hashed at preparation, without contacting a registry. Disks with backing or external data
files are rejected; convert them to a standalone qcow2 so its identity covers the complete disk.
The operator must keep prepared local disks unchanged while they are in use.

The authored image source digest excludes `image/assets` so normal Task identity and
asset provenance remain separate. A prepared Linux image is recorded under its build
context's file contents, its asset inventory (path, size, modification time and mode, as
for asset dirtiness) and the framework identity. An unchanged Task finds its image there
and uses it directly, without asking Docker to build or check anything, so a base tag that
moves is not followed: name exact base versions. Windows cache keys include the actual
image asset bytes. Dirty or unsynchronized assets disable resume and reporting.
RunLock schema 2 records declaration, preparation, Provider, and exact observed identity;
it does not invent a whole-disk content digest. Local Linux VMs record OCI and materializer
identities; local Windows VMs record builder identity and resolved base materials.

## Reverification

Image source identity describes the files under `image/`, separately from the exact
built image recorded in RunLock. It is not the decision to reuse a solver's answer.
A Task run records `task.non_verifier_digest`: a content hash of every Task file outside
`verify/` (paths, executable bits and link targets), computed once per Task folder. The four
asset roots and engine-owned `.ale-cache` are excluded, so loading a Task never reads asset
bytes. File timestamps do not enter this hash. Reverification requires this digest to match
the source episode and the Task's asset observation to equal the source RunLock's: the same
repository, Task path and commit, clean on both sides. Any outside change, any asset change
(asset commits do not distinguish `verify/assets` from the other roots), or missing source
evidence requires a fresh solver run. This also protects direct CLI use.

When only verification changed, ALE does not build or resolve the solver image again.
It attaches the retained solver using the source RunLock's exact image identity, and
prepares only a separate verifier image when needed. Provider attachment remains strict;
missing retained resources are not replaced with a different sandbox. The new RunLock
keeps the actual solver image and records the new verifier. No image-config or layer
comparison is needed. New builds resolve immutable output digests from Buildx metadata,
not mutable tags that concurrent builds can overwrite.

## VM preparation

A Linux VM Task Dockerfile usually ends in `vm-ubuntu24-base`. ALE adds the framework
layer, exports the resulting OCI rootfs and runs its pinned materializer, which owns
initramfs, boot, partition assembly, and the minimal bootable qcow2 template. Reuse is
keyed by the layered OCI and materializer identities and validated structurally without
hashing the whole disk.

Windows 10 has no public ISO build path in ALE. The current private BYOL base starts from
the GCP image `agenthle-win10-base-0210`; the maintained `images/base/vm-windows10/prepare.ps1`
recipe installs and pins ALE's cross-platform components, and `compact.sh` validates and
compresses the resulting qcow2. Episodes always cold-boot a fresh overlay. ALE creates no
ready snapshot, warm pool, or environment server.

Windows Task preparation boots a temporary VM from a framed copy of the resolved base,
stages only `image/`, and executes `image/run.ps1` with network access for unattended
installation. The image script has a one-hour budget for installation, integrity checks and extraction;
Host-to-VM staging happens before that timer starts.
ALE removes the build materials, shuts down the VM, and saves its configured qcow2.
Reuse covers the whole build and is keyed by base, builder, scripts, and asset bytes;
there is no per-command cache or new recipe language. Authoring details belong to
[Task authoring](task-authoring.md#image).

Software assets are deployed during image construction and saved into the Task qcow2.
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

Use `setup/` only for per-episode work that cannot be frozen: mutable reset/seeding,
generated values, writable copies, dynamic service start/reset, and readiness checks.
Setup is not a second image builder.

Small static data may be committed anywhere under `image/`. Large data uses the ignored
`image/assets/` directory restored by `ale assets pull`, then the same image build path.
Runtime never downloads it.

## Verifier images

Shared verification uses the solver image. Separate verification may:

1. reuse the prepared solver image when neither local nor external image is supplied;
2. on Linux, build `verify/Dockerfile` with `verify/` as context and explicit nested kind;
3. resolve a container or VM `verify.image.ref` through the matching Provider.

The local verifier context is fixed at `verify/`; its Dockerfile is the declaration. External
references are pulled and resolved to an immutable repository/content identity before
solver execution. They are verifier-only; the standard solver image remains locally
built from `image/`.

Any verifier image must satisfy the same guest contract, expose system Python 3.12 or
newer, and permit ALE to stage `ale_verify`. Verification runs as framework-controlled
Task code.

## Runtime behavior

Each Sandbox owns two fixed guest connections: one for commands and one for file
discovery and reads. A long-running command must not block periodic log collection.
Docker uses two stdio guest sessions, Daytona two over SSH; QEMU uses two authenticated
connections to the same guest server and port. Providers close both on startup failure,
retention, destruction, and image export.

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
- uses the Task's agent account and waits for the desktop when the image has one;
- applies CPU, memory, writable storage, network, sudo, and GPU requests or rejects them;
- injects only selected NVIDIA devices through the configured runtime/CDI path;
- observes the resulting image and resource allocation;
- labels every ALE container with episode, physical role, requested retention, and GPU
  identity;
- destroys by default or returns a retained handle after framework sanitation.

The QEMU Provider applies the same retention contract to its runner container and the
episode qcow2 overlay mounted into it. `keep` leaves both alive and discoverable;
`ale sandbox destroy HANDLE` removes both. Provider-qualified QEMU handles prevent the
runner from being mistaken for a container sandbox.

QEMU enforces Task egress in the runner network namespace, outside both Linux and Windows
guests. The guest does not need an OS-specific firewall implementation for ALE policy.

The Daytona Provider runs Linux container images, with or without GPUs, on Daytona's
cloud under the same contract; how it meets it there is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#daytona). Two of its
differences reach other modules: the RunLock's observed image is the digest pushed to
Daytona's registry, and memory and writable storage must be whole GiB, refused rather
than rounded.

Retained handles are always provider-qualified: `docker:<runtime-id>` for containers,
`qemu:<runtime-id>` for VMs and `daytona:<sandbox-id>` for Daytona. Unqualified handles
are rejected. Listing, destroying and attaching are Provider operations, routed by the
Provider a handle or RunLock names, so a new backend needs no CLI change.

GPU workload libraries such as CUDA, PyTorch, and simulators remain Task Dockerfile
responsibility. Tasks request a count only; Provider configuration selects eligible
devices (host indices, or models on Daytona) and ALE records actual UUIDs. QEMU GPU
passthrough remains a Provider capability, not a Task image declaration.

## Permissions

Setup, artifact collection/restoration, and verification use the framework's elevated
identity: root on Linux, Administrator on Windows. This applies to shared and separate
verification independently of the agent's `resources.sudo` setting.

Directory uploads ignore Host permissions: Linux files use `0644`, directories `0755`,
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
direct `image.ref` disk is framed like any other. Linux configures requested
sudo during image preparation. Grants, and their absence, are verified and recorded: a
Linux sandbox is refused when its guest service is not root, when a requested grant does
not work, or when an agent that did not request one is root or can use sudo. ALE does not pre-create
artifacts or repair their ownership: image/setup/agent code owns their type and existence.

## Provider contract

A Provider turns a SandboxRequest into a running Sandbox that holds the guarantees below,
or rejects the request before anything is created; it never degrades one silently. It owns
isolation (resources, network, identity, enforced where the agent cannot reach), the
channels (Host to guestd, sandbox to Gateway), the lifecycle (create, destroy, retain,
attach) and truthful observation. It does not own phase timing (the Environment), command
and file semantics (guestd), credentials (the Gateway) or image and Task content (the
engine).

| Guarantee | Enforced by | Conformance test |
|---|---|---|
| Every sandbox runs the framework of the engine driving it, whatever image or disk it was handed | Provider in `create` | `test_a_sandbox_carries_the_engines_framework` |
| Agent work runs as `image.user`, framework work as root or Administrator; the agent elevates exactly when `resources.sudo` asks | Provider check in `create`, both directions | `test_the_agent_and_the_framework_are_different_accounts`, `test_storage_and_sudo_are_enforced_or_rejected` |
| Uploads land with the fixed modes and owner of their identity | guestd `put_tree` (Windows: Provider ACL reset) | `test_uploads_ignore_host_modes_and_belong_to_their_identity` |
| File reads, listings and digests proceed while a command runs and never pass through a link | a second guest connection from the Provider; guestd | `test_file_reads_progress_while_a_command_is_running` |
| Framework phases have the network; after `close_egress` the agent reaches its Gateway (and, for `allowlist`, the declared hosts) and nothing else; `open` Tasks are never sealed; the Gateway address never moves | Provider, outside the agent's reach | `test_a_sealed_sandbox_reaches_its_gateway_and_nothing_else`, `test_a_task_that_declared_open_is_never_sealed` |
| Resources are applied or rejected; the RunLock records the observed image and allocation | Provider | `test_ready_sandbox_reports_observed_image_and_resources`, `test_rejects_a_request_it_cannot_serve` |
| Destroy is idempotent; retained sandboxes can be listed and destroyed through their Provider | Provider | `test_destroy_is_idempotent`, `test_a_retained_sandbox_can_be_listed_and_destroyed` |
| A declared desktop can be captured and driven | Provider and image | `test_a_declared_desktop_can_actually_be_used`, `test_a_declared_desktop_accepts_input` |
| No platform service inside the sandbox gives the agent more than its account | Provider check in `create` | `test_the_agent_cannot_reach_the_platform_daemon` |

`open_egress` and `close_egress` are abstract: a default that did nothing would let a new
backend run the agent with the network while its RunLock said `block`. A backend that
cannot change policy at runtime raises there, and one that cannot apply a policy rejects it
in `accepts`. A Provider is complete when it passes `ale.core.testkit.ProviderConformance`;
an escape the suite cannot observe, such as a platform's own in-sandbox service, is that
Provider's to close and to check in `create`.

## Base-image changes

Base build sources live in the ALE engine repository and are Provider-conformance tested.
Changing them changes every Task foundation and requires image contract tests. Task
contributors add dependencies to their own `image/` rather than adding domain bases to ALE.
