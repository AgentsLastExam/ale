# The virtual-machine backend

> Superseded authoring path: `build-desktop.sh` is retained only for existing local
> guests. New Task VM images use `images/base/vm-gui` plus the versioned
> `vm-materializer`; the standard base release workflow no longer builds from an ISO.

Two images, and they are easy to confuse:

| | What it is | Built by |
|---|---|---|
| **the runner** | a *container* holding `qemu-system-x86_64` | `images/base/qemu-runner/Dockerfile` |
| **legacy guest** | an older locally installed Ubuntu desktop disk | `build-desktop.sh` |

The host needs Docker and `/dev/kvm`. It does not need qemu to *run* a sandbox: the runner
has it. Building a guest does need it locally.

## Getting the guest

```bash
uv run ale pull-guest                   # the published disk, download-speed
uv run ale run <task> --provider qemu
```

Or build your own, which takes about forty minutes:

```bash
bash images/base/qemu/build-desktop.sh
```

`ALE_QEMU_IMAGE` points the provider elsewhere if you put it somewhere else.

The published disk travels as the single layer of a container image
(`ghcr.io/agentslastexam/ale-guest-ubuntu-desktop`). A qcow2 is not a container image, but
shipping it as one means it moves over the registry everyone is already authenticated to,
with no second distribution channel and no extra tool to install. `pull-guest` copies the
file out of a stopped container; the image has no command and is not meant to have one.

**Concurrency costs almost nothing on disk.** Each episode gets a copy-on-write overlay
over the shared read-only base, so the 11GB is paid once however many run at a time —
measured at 0.0GB consumed across three concurrent desktop guests, which came up in 42
seconds together.

There is one guest and it has a desktop. A headless variant existed and was removed: it
answered no question the container backend does not answer faster, and keeping two guests
meant every change to the contract had to be made and verified twice.

The desktop is installed rather than assembled, from the official Desktop ISO, by running
Canonical's own installer unattended. It cannot come from a cloud image, because Canonical
publishes no desktop one — everything under `cloud-images.ubuntu.com` is a server image. Every answer a person would click is in `autoinstall.yaml` next
to the script, so the result is reproducible from this repository and nothing else. It is
24.04 rather than 22.04 because autoinstall is supported by the Desktop installer only
from 23.04 onward; on 22.04 the desktop installer is ubiquity, whose preseed equivalent is
exactly the kind of build nobody can repeat.

It then runs `customise.sh`, which is where the contract of `docs/specs/sandbox-image.md`
is applied — one system interpreter with the guest service's dependencies, the guest
service as a unit, a default-deny firewall, and `/etc/ale/image.json` declaring the agent
account. It is a separate script because the contract it applies is the same one the
container images satisfy, and it should be readable on its own.

Building needs `qemu-system-x86`, `qemu-utils`, `genisoimage` and `libguestfs-tools`. The
customise step runs under `sudo` when `/boot/vmlinuz-*` is root-readable only, which is
the default on Debian and Ubuntu; the finished disk is handed back, so running a task
needs no privilege at all.

## How a guest is confined

The guest's only route leads to the runner, and the runner forwards exactly one port —
the gateway's — to the host. Everything else the guest sends is dropped **in the runner**,
in a network namespace the agent cannot reach.

That last part is the point. The guest has its own `nftables` rules saying the same thing,
but a task may declare `resources.sudo`, and an agent with root can flush them. The
in-guest half is defence in depth and the posture the image keeps if it is booted
somewhere else; the half that binds the agent is the one it has no access to.

None of it applies outside the agent's phase. A task's network policy describes what binds
the agent, so setup, the agent's own installation and verify all run with egress open —
see ADR 0005 and `docs/specs/standard-environment.md`.

QEMU rule construction and mode transitions are covered by
`tests/unit/test_qemu_provider.py`; live boot and sandbox behavior are covered by
`tests/conformance/test_provider_qemu.py`.
