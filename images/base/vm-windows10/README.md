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

The recipe installs pinned Python, Cua Driver, WinGet, Chocolatey, Node.js/npm and Git Bash.
It stages guestd under `C:\ProgramData\ALE`, registers the interactive-login services,
opens guestd only to the QEMU runner, removes GCP/VMware integration and inherited domain
applications, cleans their stale data and uninstall inventory, cleans stable system state, and optionally
zeroes free space. The recipe fixes the system at en-US and UTC; disables update,
rollback, recovery, sleep, hibernation, automatic maintenance and background prompt paths;
and clears the seed user's credentials, profiles, history, stale startup tasks and window
placement. Reboot once and verify both services before requesting shutdown.

Back on the Host, compact the prepared disk with qcow2's zstd compression:

```bash
uv run images/base/vm-windows10/compact.sh input.qcow2 output.qcow2
```

The resulting disk is packaged through the existing private VM-image distribution path.
Every episode cold-boots a fresh qcow2 overlay; ALE does not create ready snapshots or a
warm pool.

```bash
uv run ale vm-image push output.qcow2 ghcr.io/agentslastexam/vm-windows10-base:0.1.0
uv run ale vm-image pull ghcr.io/agentslastexam/vm-windows10-base:0.1.0 output.qcow2
```

A GUI base is not qualified by an oracle-only Task. Before publication, run a real visual
agent against screen-only information and inspect its canonical trajectory, screenshot
blobs, desktop actions, final artifact, and reward. Oracle runs remain useful for the
deterministic setup/verify and cross-OS protocol checks, but cannot prove that an agent can
see or control the desktop.

## Guest contract

- Task manifest: `os: windows`, `image.kind: vm`.
- Stage entries: `setup/run.ps1`, `oracle/run.ps1`, and `verify/run.ps1`.
- Framework root: `C:\ProgramData\ALE`; agent home: `C:\Users\user`.
- guestd provides exec, file transfer, health, lifecycle, and the shared Sandbox protocol.
- Cua Driver 0.12.6 exclusively provides screenshots and desktop input.

Autonomous CLI Harnesses use native Windows Node.js and Git Bash; their CLI packages are
installed per run, and model credentials stay on the Host Gateway.

Re-run `prepare.ps1` to update an existing ALE seed. After reboot, run `tools.ps1 -CheckOnly`
to check package managers, Python, Node.js/npm and Git Bash. Task-specific software belongs
in the Task's `image/run.ps1`. The login launcher runs guestd elevated only when the Host
sets the `ale-sudo` SMBIOS serial for image preparation or a Task requesting `sudo`;
ordinary Tasks retain the limited token.
