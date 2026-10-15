# Container and VM sandbox image specification

Normative for standard container and VM Tasks. Every solver and dedicated verifier
declares `image.kind`. Linux uses a Dockerfile or `image.ref`; Windows uses `image.ref`
directly, or builds `image/run.ps1` on `image.base_ref`.

## What a Linux image needs

A Linux Task may build from any image that has:

- system Python 3.12 or newer as `python3`, which the guest service and `ale_verify` run on;
- the agent account the Task names with `image.user` (default `user`), with a home;
- for a container, a long-lived command that ALE does not replace;
- for a VM, a bootable systemd system (the VM base provides one);
- for GUI work, a running desktop session, such as an X server with a window manager.

Everything ALE itself needs it adds when it prepares the image, as one more layer on top
of the Task's own build or `image.ref` (the framework layer):

- guestd, the guest service every sandbox operation goes through, under root-only
  `/opt/ale`, so it always speaks the protocol of the engine driving it;
- Cua Driver at a pinned version, the one desktop capture and input implementation;
- for a VM, the systemd unit that starts guestd at boot;
- a check that the agent account exists, failing preparation if it does not;
- working sudo for that account when the Task sets `resources.sudo: true`, installing sudo
  with the image's package manager when the image lacks it.

There are no image labels to declare. Whether a sandbox has a desktop is decided from the
image's own configuration before anything starts: a VM has one when its systemd enables a
display manager (`display-manager.service`), and a container has one when its image sets
`DISPLAY` or `WAYLAND_DISPLAY`, which is how a desktop image tells its processes where to
draw. An image that ships X only as a tool, such as Xvfb for a headless browser, sets
neither and is headless; a desktop base can be made headless with `ENV DISPLAY=`. With a
desktop, the Provider waits until a screenshot succeeds and fails the sandbox if the
desktop never becomes ready; guestd starts Cua Driver inside the agent's desktop session
once its window manager or shell runs. A VM disk given directly as a qcow2 `ref` is not
prepared by ALE and must carry a current guestd itself.

## What a Windows image needs

A Windows Task builds `image/run.ps1` on an ALE Windows base. The base signs its agent
account in to a desktop and starts the guestd it carries at that logon; this bootstrap
guestd is how the build reaches the VM. After the Task's script, ALE installs its own
pieces through it:

- the current guestd replaces the base's under `C:\ProgramData\ALE\guestd`;
- Cua Driver at the pinned version under `C:\ProgramData\ALE\cua-driver`, started
  elevated at the agent's logon so it can reach elevated windows;
- a check that `image.user` names the account the base signs in.

Elevation for `resources.sudo` is granted per boot by the Host, not built into the disk.
A Windows sandbox has a desktop for ALE to drive exactly when its image carries the
framework's Cua Driver, so the Provider waits for a screenshot in every Task image and
not in the bare base booted to build one. A disk used directly as `image.ref` is not
prepared by ALE and must carry a current guestd and Cua Driver itself.

## ALE base images

- `container-ubuntu22-base` provides an Ubuntu 22.04 GNOME desktop container and the
  `user` account.
- `vm-ubuntu24-base` provides Ubuntu 24.04, systemd, full GNOME/GDM and the `user`
  account for QEMU. It has no firewall of its own: network policy is enforced by the QEMU
  runner outside the guest ([security.md](security.md#1-a-sandbox-has-only-declared-ways-out)).
- The private Windows 10 BYOL base provides a logged-in desktop, system Python, a bootstrap
  guestd, WinGet, Chocolatey, and the runtimes required by autonomous agents.
  Its licensed disk is not a public ALE artifact.

No base carries Cua Driver, and only the Windows base carries guestd, as the bootstrap
its Task builds talk to; ALE installs both when it prepares a Task image. The bases do not
contain domain stacks, benchmark data, databases, robotics suites, or Task-specific tools.

Repository sources are separated by responsibility: `images/base/` contains the three
Task foundations, `images/builders/vm-materializer/` turns Linux VM OCI content into
qcow2, and `images/runtimes/qemu-runner/` hosts prepared qcow2 disks under QEMU/KVM.

## Versions

Every ALE image is published under the engine's version, such as `0.1.1`, once and never
overwritten: the publishing workflow skips a version the registry already has, and no
`latest` tag is published. An engine runs exactly the images of its own version, so a
checkout that stays on one engine version keeps the same images whatever later versions
publish. Publishing a changed image therefore means releasing a new engine version.

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
asset provenance remain separate. Docker still consumes current asset bytes natively and
BuildKit decides Linux cache reuse; Windows cache keys include the actual image asset
bytes. Dirty or unsynchronized assets disable resume and reporting.
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

Windows Task preparation boots a temporary VM from the resolved base, stages only
`image/`, and executes `image/run.ps1` with network access for unattended installation.
The image script has a one-hour budget for installation, integrity checks and extraction;
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
agent home, and GUI readiness; a Task whose `os` disagrees with the image is rejected.
Every TCP guest connection authenticates with the sandbox token claimed by the framework's
first connection ([ADR 0008](../adr/0008-claimed-guest-service.md)); an image whose guest
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

Retained handles are always provider-qualified: `docker:<runtime-id>` for containers and
`qemu:<runtime-id>` for VMs. Unqualified handles are rejected.

GPU workload libraries such as CUDA, PyTorch, and simulators remain Task Dockerfile
responsibility. Tasks request a count only; Provider configuration selects eligible host
indices and ALE records actual UUIDs. QEMU GPU passthrough remains a Provider capability,
not a Task image declaration.

## Permissions

Solver and oracle run as the image user. Linux setup and verification run as root. The
current Windows guestd runs in the interactive agent session so Cua Driver and process
execution share one desktop; stage directories are withheld by lifecycle rather than a
second Windows account. The Windows base starts with a limited token by default; only
Host-provided SMBIOS `ale-sudo` for `request.sudo` selects an administrator session, and
image builds explicitly request it. On Linux the framework layer configures sudo when the
image is prepared. A requested solver sudo/admin grant is verified before use and recorded. ALE does not pre-create declared artifacts or silently repair Task filesystem
ownership: the image/setup/agent owns the type and existence of each output.

## Base-image changes

Base build sources live in the ALE engine repository and are Provider-conformance tested.
Changing them changes every Task foundation and requires image contract tests. Task
contributors add dependencies to their own `image/` rather than adding domain bases to ALE.
