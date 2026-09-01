# Providers

Providers turn prepared Task images into Sandboxes. ALE ships two backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container | immutable OCI image identity |
| `qemu.py` | QEMU/KVM on Linux, native QEMU/HVF or TCG on macOS | materialized qcow2 identity |

The macOS runtime starts one native QEMU process per episode. Image names containing
`arm64` select ALE's pinned `v10.0.2-utm` command-line QEMU with HVF; existing and x86_64
image names select Homebrew `qemu-system-x86_64` with TCG. Run
`scripts/build-darwin-qemu.sh` once to install the headless ARM executable and firmware
under ALE's cache. Linux retains the containerized KVM runner.

Darwin does not run QEMU inside Docker Desktop: its Linux VM cannot expose macOS
Hypervisor.framework. Each Sandbox instead owns a native QEMU process, overlay, QMP
socket, PID file, host port, and registry entry, so multiple VMs can run concurrently
without a GUI or shared mutable machine state.

Both implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and Environment
code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).
