# Windows 11 ARM64 base image

This base starts from Microsoft's official Windows 11 multi-edition ARM64 ISO and boots
natively on Apple Silicon through QEMU/HVF.  It shares the Windows guest contract and
post-processing recipe with `vm-windows10`; the final disks remain separate because a
Windows system disk cannot be converted between x86_64 and ARM64.

The image name is the architecture declaration:

```text
ghcr.io/agentslastexam/vm-windows11-arm64-base:VERSION
```

## Build on Apple Silicon

Download the official multi-edition ARM64 ISO and the current virtio-win ISO. Install
the build dependencies and compile ALE's pinned UTM QEMU fork once:

```bash
brew install qemu meson ninja pkgconf
scripts/build-darwin-qemu.sh
```

The fork is pinned to `v10.0.2-utm` and supplies the `virtio-ramfb` device and matching
EDK2 firmware needed by Windows ARM. It is a native command-line executable; neither
the UTM application nor any GUI interaction is part of the build or runtime path.

Then run:

```bash
images/base/vm-windows11-arm64/build.sh \
  Windows11-arm64.iso \
  virtio-win.iso \
  output.qcow2
```

The builder creates an 80 GiB sparse disk, exposes the unchanged Microsoft ISO as an
optical disk, and supplies a small FAT32 USB device containing the unattended answer
file. A second one-shot FAT32 device starts the optical boot and is hot-unplugged through
QMP before the first restart. QEMU runs with `-display none`; UEFI and optical-boot keys
are also sent through QMP.
The builder installs the ARM64 VirtIO drivers, creates the fixed `user` desktop account,
runs ALE's shared Windows preparation, and waits for the guest to shut down. The
`specialize` pass registers a SYSTEM logon task; on the first `user` login it launches a
second elevated interactive task so Cua Driver is installed in that user's profile.
This avoids depending on `FirstLogonCommands` and leaves bootstrap logs and an
`install.done` or `install.failed` marker under `C:\ALESeed`.

The local account uses the public password `ale` so Windows AutoLogon remains valid
after Setup removes its temporary secrets. QEMU exposes no remote login service; the
password is an image bootstrap value, not a credential.

The output can then be published with:

```bash
uv run ale vm-image push output.qcow2 \
  ghcr.io/agentslastexam/vm-windows11-arm64-base:0.1.0
```

The default WIM image name is `Windows 11 Pro`, which is present in Microsoft's public
multi-edition ARM64 ISO. The default product key is Microsoft's public Pro GVLK; it
selects the edition but does not activate Windows or grant a license. To build from
another official image, override both values without changing the answer file:

```bash
ALE_WINDOWS_IMAGE_NAME='Windows 11 Pro N' \
ALE_WINDOWS_PRODUCT_KEY='MH37W-N47XK-V7XM9-C7227-GCQG9' \
  images/base/vm-windows11-arm64/build.sh Windows11-arm64.iso virtio-win.iso output.qcow2
```

Do not publish the ISO or an installed disk outside the access and licensing policy
chosen for ALE.
