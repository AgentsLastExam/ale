# The virtual-machine backend

Two images, and they are easy to confuse:

| | What it is | Built by |
|---|---|---|
| **the runner** | a *container* holding `qemu-system-x86_64` | `images/base/qemu-runner/Dockerfile` |
| **the guest** | the *disk* that container boots | `images/base/qemu/build.sh` |

The host needs Docker and `/dev/kvm`. It does not need qemu: the runner has it.

## The guest disk

```bash
bash images/base/qemu/build.sh          # ~5 minutes, writes ~/.cache/ale/images/ale-ubuntu22.qcow2
uv run ale run <task> --provider qemu
```

It starts from Canonical's published `jammy-server-cloudimg-amd64.img` rather than anyone's
exported disk — versioned, signed, and accountable to a build nobody here ran — and adds
exactly what `docs/specs/sandbox-image.md` requires: one system interpreter with the guest
service's dependencies, an unprivileged agent account, the guest service as a unit, a
default-deny firewall, and `/etc/ale/image.json` declaring the first two.

Needs `libguestfs-tools` and `qemu-utils`. The customise step runs under `sudo` when
`/boot/vmlinuz-*` is root-readable only, which is the default on Debian and Ubuntu; the
finished disk is handed back, so running a task needs no privilege at all.

Point `ALE_QEMU_IMAGE` elsewhere to use a different disk.

## How a guest is confined

The guest's only route leads to the runner, and the runner forwards exactly one port —
the gateway's — to the host. Everything else the guest sends is dropped **in the runner**,
in a network namespace the agent cannot reach.

That last part is the point. The guest has its own `nftables` rules saying the same thing,
but a task may declare `resources.sudo`, and an agent with root can flush them. The
in-guest half is defence in depth and the posture the image keeps if it is booted
somewhere else; the half that binds the agent is the one it has no access to.

Both directions are covered by `tests/integration/test_backend_parity.py` and by the
`demo/netprobe` task, which probes the boundary as the agent rather than describing it.
