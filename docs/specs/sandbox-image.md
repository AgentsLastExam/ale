# Container and VM sandbox image specification

Normative for standard container and VM Tasks. Every solver and dedicated verifier
declares `image.kind`; a fixed Dockerfile selects local preparation, otherwise `image.ref`
is required.

## ALE base images

- `sandbox-base-cli` provides a headless Linux sandbox.
- `sandbox-base-gui` additionally starts a usable desktop.
- `sandbox-base-vm-gui` provides Ubuntu 24.04, systemd, full GNOME/GDM, the declared
  unprivileged user, and ALE guest integration for QEMU.

Both provide framework integration that should not be recreated per Task:

- system `python3` 3.12 or newer;
- an unprivileged account with a real home, identified by `ale.user` (default `user`);
- the guest service and dependencies needed by the Docker Provider;
- a long-lived image command that ALE does not replace;
- `sudo` where Tasks requesting solver elevation are supported;
- `ale.gui=true` and a ready graphical session for the GUI base.

They do not contain domain stacks, benchmark data, databases, robotics suites, or
Task-specific tools.

## Task image

For a local image, `image/` is the sole ordinary Docker build context:

```dockerfile
FROM ghcr.io/agentslastexam/sandbox-base-cli:latest

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

## Local VM preparation

A VM Task Dockerfile ends in `sandbox-base-vm-gui`. ALE exports the final OCI rootfs and
runs its pinned materializer, which owns initramfs, boot, partition assembly, and the
minimal bootable qcow2 template. Reuse is keyed by final OCI plus materializer identities
and validated structurally without hashing the whole disk.

The QEMU Provider sizes a fresh overlay from `storage_mb`, boots it over the prepared
qcow2, grows the guest root filesystem, waits for guestd and the declared GNOME session,
and observes usable guest `/` capacity. Tasks may install ordinary OS packages and
services, including Docker; VM boot machinery stays engine-owned.

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

Any verifier image must satisfy the same guest contract, expose system `python3` 3.12 or
newer, and permit ALE to stage `ale_verify`. Verification runs as framework/root.

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

Retained handles are always provider-qualified: `docker:<runtime-id>` for containers and
`qemu:<runtime-id>` for VMs. Unqualified handles are rejected.

GPU workload libraries such as CUDA, PyTorch, and simulators remain Task Dockerfile
responsibility. Tasks request a count only; Provider configuration selects eligible host
indices and ALE records actual UUIDs. QEMU GPU passthrough remains a Provider capability,
not a Task image declaration.

## Permissions

Solver and oracle run as the image user. Setup and verification run as root. A requested
solver sudo grant is verified before use and recorded. ALE does not pre-create declared
artifacts or silently repair Task filesystem ownership: the image/setup/agent owns the
type and existence of each output.

## Base-image changes

Base Dockerfiles live in the ALE engine repository and are Provider-conformance tested.
Changing them changes every Task foundation and requires image contract tests. Task
contributors add dependencies to their own Dockerfile rather than adding domain bases to
ALE.
