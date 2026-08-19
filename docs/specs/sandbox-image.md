# Container and VM sandbox image specification

Normative for standard container and VM Tasks. Every solver and dedicated verifier
declares `image.kind`; a fixed Dockerfile selects local preparation, otherwise `image.ref`
is required.

## ALE base images

- `container-ubuntu22-base` provides an Ubuntu 22.04 desktop container.
- `vm-ubuntu24-base` provides Ubuntu 24.04, systemd, full GNOME/GDM, the declared
  unprivileged user, and ALE guest integration for QEMU.
- The private Windows 10 BYOL base provides a logged-in desktop, system Python, guestd,
  and Cua Driver for QEMU. Its licensed disk is not a public ALE artifact.

The bases provide framework integration that should not be recreated per Task:

- system Python 3.12 or newer (`python3` on Linux, `python.exe` on Windows);
- an unprivileged account with a real home, exposed by the Provider contract;
- the guest service and dependencies needed by the Providers;
- a long-lived image command that ALE does not replace;
- `sudo` where Tasks requesting solver elevation are supported;
- Cua Driver and a ready graphical session for GUI bases. All guestd screenshot and input
  operations go through Cua Driver on both Linux and Windows.

They do not contain domain stacks, benchmark data, databases, robotics suites, or
Task-specific tools.

## Task image

For a local image, `image/` is the sole ordinary Docker build context:

```dockerfile
FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest

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

Before any solver model call, ALE applies one rule: an existing fixed Dockerfile is built
locally and failure is terminal; with no Dockerfile, the matching Provider resolves the
declared ref. Container and VM refs are both supported and recorded immutably.

The authored image source digest excludes `image/assets` so normal Task identity and
asset provenance remain separate. Docker still consumes current asset bytes natively and
BuildKit decides cache reuse. Dirty or unsynchronized assets disable resume and reporting.
RunLock schema 2 records declaration, preparation, Provider, and exact observed identity;
it does not invent a whole-disk content digest.

## VM preparation

A VM Task Dockerfile ends in `vm-ubuntu24-base`. ALE exports the final OCI rootfs and
runs its pinned materializer, which owns initramfs, boot, partition assembly, and the
minimal bootable qcow2 template. Reuse is keyed by final OCI plus materializer identities
and validated structurally without hashing the whole disk.

Windows 10 has no public ISO build path in ALE. The current private BYOL base starts from
the GCP image `agenthle-win10-base-0210`; the maintained `images/base/windows/prepare.ps1`
recipe installs and pins ALE's cross-platform components, and `compact.sh` validates and
compresses the resulting qcow2. Episodes always cold-boot a fresh overlay. ALE creates no
ready snapshot, warm pool, or environment server.

GUI image qualification includes a real visual agent completing a task whose decisive
input exists only on screen, followed by inspection of its canonical trajectory,
screenshots, desktop actions, artifact, and reward. An oracle-only run checks plumbing but
does not establish GUI readiness.

The QEMU Provider sizes a fresh overlay from `storage_mb`, boots it over the prepared
qcow2, grows the guest root filesystem, waits for guestd and the declared desktop,
and observes usable root capacity. The guest health contract reports its OS, agent user,
agent home, and GUI readiness; a Task whose `os` disagrees with the image is rejected.
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
`image/assets/` directory restored by `ale assets pull`, then the same normal Dockerfile
path. Runtime never downloads it.

## Verifier images

Shared verification uses the solver image. Separate verification may:

1. reuse the prepared solver image when neither local nor external image is supplied;
2. build `verify/Dockerfile` with `verify/` as context and explicit nested kind;
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

- starts the exact prepared content without replacing its command;
- verifies the declared agent user and optional desktop readiness;
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
second Windows account. A requested solver sudo/admin grant is verified before use and
recorded. ALE does not pre-create declared artifacts or silently repair Task filesystem
ownership: the image/setup/agent owns the type and existence of each output.

## Base-image changes

Base Dockerfiles live in the ALE engine repository and are Provider-conformance tested.
Changing them changes every Task foundation and requires image contract tests. Task
contributors add dependencies to their own Dockerfile rather than adding domain bases to
ALE.
