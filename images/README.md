# Sandbox images

This directory separates Task foundations from the tools that build and run them.

```text
base/
├── container-ubuntu22/  Ubuntu desktop container base
├── vm-ubuntu24/         Ubuntu OCI source for a bootable VM
└── vm-windows10/        private Windows seed preparation recipe
builders/
└── vm-materializer/     converts a VM OCI rootfs to qcow2; frames a Linux qcow2 ref
runtimes/
├── qemu-runner/         container that runs a prepared qcow2 with QEMU/KVM
└── firecracker-runner/  container that runs a container Task as a Firecracker microVM
```

Task repositories extend or reference only entries under `base/`. Linux VM Tasks build an
OCI rootfs from `vm-ubuntu24`; the materializer converts it to qcow2 and the QEMU runner
boots it. Windows preparation already produces qcow2, so it goes directly to the runner.
The Firecracker runner takes a container Task's prepared image instead: it converts the
image's filesystem into an ext4 disk once and boots it with its own kernel.
macOS Tasks name a Tart image of their own; the Orchard and Tart that run it come from
`scripts/setup_macos.py`, not from this directory.

The image contract, identity rules and required labels are defined in
[`../docs/specs/sandbox-image.md`](../docs/specs/sandbox-image.md).

Every image is published once per version and never overwritten. The builder and runner
run the engine's own logic, so they share the engine's version
(`packages/ale-run/pyproject.toml`) and a changed one ships with the next engine version.
A base carries no part of the framework and has its own version: the Linux bases' are in
`BASES` in `packages/ale-run/src/ale/run/__init__.py`, the Windows base's in its README.
Raise a base's version to publish it changed.

```bash
just images                                  # pull published images, build unpublished ones
images/build.sh vm-ubuntu24-base             # one image
```

`.github/workflows/images.yml` publishes each version once; its manual `promote` input
gives an already-published image a version without rebuilding it. It pushes with this
repository's workflow token, so each GHCR package must grant `AgentsLastExam/ale` write
access in its package settings (Manage Actions access); a package first pushed by hand has
none, and its pushes fail with `permission_denied: write_package`. The licensed
Windows disk remains private. Do not add Task content or Provider credentials here.
