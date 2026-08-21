# Sandbox images

This directory separates Task foundations from the tools that build and run them.

```text
base/
├── container-ubuntu22/  Ubuntu desktop container base
├── vm-ubuntu24/         Ubuntu OCI source for a bootable VM
└── vm-windows10/        private Windows seed preparation recipe
builders/
└── vm-materializer/     converts a VM OCI rootfs to qcow2
runtimes/
└── qemu-runner/         container that runs a prepared qcow2 with QEMU/KVM
```

Task repositories extend or reference only entries under `base/`. Linux VM Tasks build an
OCI rootfs from `vm-ubuntu24`; the materializer converts it to qcow2 and the QEMU runner
boots it. Windows preparation already produces qcow2, so it goes directly to the runner.

The image contract, identity rules and required labels are defined in
[`../docs/specs/sandbox-image.md`](../docs/specs/sandbox-image.md).

```bash
just images
```

Publishing for buildable images is handled by `.github/workflows/images.yml`. The licensed
Windows disk remains private. Do not add Task content or Provider credentials here.
