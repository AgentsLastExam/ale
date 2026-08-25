# Providers

Providers turn prepared Task images into Sandboxes. ALE ships two backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container | immutable OCI image identity |
| `qemu.py` | QEMU/KVM on Linux, native QEMU/HVF or TCG on macOS | materialized qcow2 identity |

The macOS runtime starts one native QEMU process per episode. Image names containing
`arm64` select `qemu-system-aarch64` with HVF; existing and x86_64 image names select
`qemu-system-x86_64` with TCG. Linux retains the containerized KVM runner.

Both implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and Environment
code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).
