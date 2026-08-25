# Windows 11 ARM64 base image

This base starts from Microsoft's Windows 11 Enterprise Evaluation ARM64 ISO and boots
natively on Apple Silicon through QEMU/HVF.  It shares the Windows guest contract and
post-processing recipe with `vm-windows10`; the final disks remain separate because a
Windows system disk cannot be converted between x86_64 and ARM64.

The image name is the architecture declaration:

```text
ghcr.io/agentslastexam/vm-windows11-arm64-base:VERSION
```

## Build on Apple Silicon

Download the official Enterprise Evaluation ARM64 ISO and the current virtio-win ISO,
then run:

```bash
images/base/vm-windows11-arm64/build.sh \
  Windows11EnterpriseEvaluation-arm64.iso \
  virtio-win.iso \
  output.qcow2
```

The builder creates an 80 GiB sparse disk, supplies an unattended answer ISO, installs
the ARM64 VirtIO drivers, creates the fixed `user` desktop account, runs ALE's shared
Windows preparation, and waits for the guest to shut down.  The output can then be
published with:

```bash
uv run ale vm-image push output.qcow2 \
  ghcr.io/agentslastexam/vm-windows11-arm64-base:0.1.0
```

The default WIM image name is `Windows 11 Enterprise Evaluation`. To validate the
pipeline with Microsoft's multi-edition consumer ISO instead, select its Pro image
without changing the answer file:

```bash
ALE_WINDOWS_IMAGE_NAME='Windows 11 Pro' \
  images/base/vm-windows11-arm64/build.sh Windows11-arm64.iso virtio-win.iso output.qcow2
```

The Microsoft evaluation is time limited.  Do not publish the ISO or an installed disk
outside the access and licensing policy chosen for ALE.
