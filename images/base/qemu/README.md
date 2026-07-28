# The virtual-machine backend

Three images, and the first two are easy to confuse:

| | What it is | Built by |
|---|---|---|
| **the runner** | a *container* holding `qemu-system-x86_64` | `images/base/qemu-runner/Dockerfile` |
| **the headless guest** | the *disk* it boots, no screen | `build.sh` |
| **the desktop guest** | the same, with a real Ubuntu desktop | `build-desktop.sh` |

The host needs Docker and `/dev/kvm`. It does not need qemu to *run* a sandbox: the runner
has it. Building a guest does need it locally.

## Which guest

```bash
bash images/base/qemu/build.sh          # ~5 min, ~1GB, headless
bash images/base/qemu/build-desktop.sh  # ~40 min, several GB, real desktop
uv run ale run <task> --provider qemu
```

`ALE_QEMU_IMAGE` points the provider at whichever you built.

**Headless** starts from Canonical's published `jammy-server-cloudimg-amd64.img` and adds
the contract. It cannot grow a desktop, because Canonical publishes no desktop cloud image
— everything under `cloud-images.ubuntu.com` is a server image.

**Desktop** therefore installs one, from the official Desktop ISO, by running Canonical's
own installer unattended. Every answer a person would click is in `autoinstall.yaml` next
to the script, so the result is reproducible from this repository and nothing else. It is
24.04 rather than 22.04 because autoinstall is supported by the Desktop installer only
from 23.04 onward; on 22.04 the desktop installer is ubiquity, whose preseed equivalent is
exactly the kind of build nobody can repeat.

Both then run `customise.sh`, which is where the contract of `docs/specs/sandbox-image.md`
is applied — one system interpreter with the guest service's dependencies, the guest
service as a unit, a default-deny firewall, and `/etc/ale/image.json` declaring the agent
account. Sharing that step is what keeps the two guests from drifting apart in what they
promise.

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
see ADR 0013 and `docs/specs/task-folder.md`.

Both directions are covered by `tests/integration/test_backend_parity.py`,
`tests/integration/test_network_phases.py`, and by the `demo/netprobe` task, which probes
the boundary as the agent rather than describing it.
