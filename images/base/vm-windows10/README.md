# Windows 10 base image

ALE's Windows base is a qcow2 derived from the GCP image `agenthle-win10-base-0210`: a
cold-boot seed, not an ISO build. ALE publishes it as a public image at
`ghcr.io/agentslastexam/vm-windows10-base`. The repository keeps the repeatable ALE
post-processing recipe but not the disk, so a new version is published from a Host that has
the disk, with `ale vm-image push`.

## Prepare the seed

1. Export the GCP image as qcow2 and boot a disposable overlay with QEMU.
2. Make this directory visible to the guest.
3. From an elevated PowerShell session as the agent account, copy it into a local staging
   directory and run:

```powershell
.\prepare.ps1 -AgentUser user
```

The recipe installs pinned Python, WinGet, Chocolatey, Node.js/npm and Git Bash. It
removes GCP/VMware integration and inherited domain applications, cleans their stale data
and uninstall inventory, and cleans stable system state. The recipe fixes the system at
en-US and UTC; disables update, rollback, recovery, sleep, hibernation, automatic
maintenance and background prompt paths; and clears the seed user's credentials, profiles,
history, caches, stale startup tasks, window placement and its own per-user Python. The
page file is zeroed at every shutdown, so neither the base nor a Task image saved from it
stores its contents. The recipe installs no part of ALE's framework, and removes what an
earlier recipe installed. Shut the guest down completely (Fast Startup is off) before
exporting the disk: ALE edits the disk offline and cannot write to one left hibernated.

Back on the Host, finish and compress the prepared disk. `compact.sh` runs `finalize.py`
in ALE's VM builder, which edits the disk in place while Windows is off: it disables
Defender and the Security Center that would warn about it (Tamper Protection keeps them on
while Windows runs), deletes their data, the last cumulative update's unpacked copy under
`servicing\LCU` and the page file, and zeroes free space. Then it compresses the disk with
qcow2's zstd compression:

```bash
uv run images/base/vm-windows10/compact.sh input.qcow2 output.qcow2
```

Every episode cold-boots a fresh qcow2 overlay; ALE does not create ready snapshots or a
warm pool. Publish the resulting disk under a new version after `docker login ghcr.io`;
`ale vm-image push` refuses a version that exists, and the
[Providers README](../../../packages/ale-run/src/ale/run/providers/README.md#registry-disks)
describes how it travels.

```bash
uv run ale vm-image push output.qcow2 ghcr.io/agentslastexam/vm-windows10-base:0.1.3
uv run ale vm-image pull ghcr.io/agentslastexam/vm-windows10-base:0.1.3 output.qcow2
```

A GUI base is not qualified by an oracle-only Task. Before publication, run a real visual
agent against screen-only information and inspect its canonical trajectory, screenshot
blobs, desktop actions, final artifact, and reward. Oracle runs remain useful for the
deterministic setup/verify and cross-OS protocol checks, but cannot prove that an agent can
see or control the desktop.

## Guest contract

- Task configuration: `[environment].os = "windows"`.
- Stage entries: `environment/setup/setup.ps1`, `solution/solve.ps1`, and `tests/test.ps1`.
- Framework root: `C:\ProgramData\ALE`; agent home: `C:\Users\user`.
- The base signs the agent account in automatically (Winlogon `AutoAdminLogon` and
  `DefaultUserName`) and keeps UAC on. ALE checks those, and that the agent account
  exists, before it installs anything.
- The base carries no guestd, Cua Driver or startup task. ALE installs them into a layer on
  the disk offline, with its own Python and a boot service on it that starts guestd and Cua
  Driver elevated in the agent's session; guestd then provides exec, file transfer, health, lifecycle, and the
  shared Sandbox protocol. Agent commands use the limited desktop token unless
  `[agent].sudo` grants Administrator access.
- Cua Driver exclusively provides screenshots and desktop input.
- The all-users Python 3.12 the base installs is for Tasks and agents; verification uses it
  when it can, since Task packages are installed there.

Autonomous CLI Harnesses use native Windows Node.js and Git Bash; their CLI packages are
installed per run, and model credentials stay on the Host Gateway.

Re-run `prepare.ps1` to update an existing ALE seed. After reboot, run `tools.ps1 -CheckOnly`
to check package managers, Python, Node.js/npm and Git Bash. Task-specific software belongs
in the Task's `environment/build.ps1`.
