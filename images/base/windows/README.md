# Windows 10 base image

ALE's first Windows base is a private BYOL qcow2 derived from the GCP image
`agenthle-win10-base-0210`. It is a cold-boot seed, not an ISO build and not a public
redistributable artifact. The repository keeps the repeatable ALE post-processing recipe;
the licensed Windows bytes remain in private storage.

## Prepare the seed

1. Export the GCP image as qcow2 and boot a disposable overlay with QEMU.
2. Make this directory and `packages/ale-run/src/ale/run/guestd/` visible to the guest.
3. From an elevated PowerShell session, copy both into a local staging directory and run:

```powershell
.\prepare.ps1 -AgentUser user -GuestdSource .\guestd -Compact
```

The recipe installs pinned Python and Cua Driver releases, stages guestd under
`C:\ProgramData\ALE`, registers both interactive-login services, opens guestd only to the
QEMU runner, removes the GCP/VMware integration and non-base desktop applications, cleans
their stale data and uninstall inventory, cleans stable system state, and optionally zeroes
free space. It deliberately keeps Edge and WebView2, LibreOffice, Git, 7-Zip, Python, and
the Visual C++ runtimes. Reboot once and
verify both services before requesting shutdown. The current GCP seed reaches Windows'
shutdown screen but does not always complete ACPI power-off under the QEMU runner; wait for
disk I/O to quiesce before stopping the disposable preparation VM. Normal ALE episodes use
Provider destruction and do not depend on guest ACPI power-off.

Back on the Host, compact the prepared disk with qcow2's zstd compression:

```bash
uv run images/base/windows/compact.sh input.qcow2 output.qcow2
```

The resulting disk is packaged through the existing private VM-image distribution path.
Every episode cold-boots a fresh qcow2 overlay; ALE does not create ready snapshots or a
warm pool.

## Guest contract

- Task manifest: `os: windows`, `image.kind: vm`.
- Stage entries: `setup/run.ps1`, `oracle/run.ps1`, and `verify/run.ps1`.
- Framework root: `C:\ProgramData\ALE`; agent home: `C:\Users\user`.
- guestd provides exec, file transfer, health, lifecycle, and the shared Sandbox protocol.
- Cua Driver 0.12.6 exclusively provides screenshots and desktop input.

The current Windows path targets in-process Policy Harnesses plus `nop` and `oracle`.
Autonomous CLI Harness support is separate because their installers and native log paths
are currently Linux-specific.
