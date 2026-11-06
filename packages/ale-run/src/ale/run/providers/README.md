# Providers

Providers turn prepared Task images into Sandboxes. ALE ships the following backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container | immutable OCI image identity |
| `qemu.py` | QEMU/KVM guest runner | materialized qcow2 identity |
| `daytona.py` | Daytona cloud container | immutable OCI image identity, pushed by digest |
| `orchard.py` | macOS VM on Orchard/Tart | digest-pinned OCI image, or a local image `build.sh` built |

`daytona_api.py` is its REST client and `daytona_image.py` adapts and publishes images.

They implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and episode
protocol code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).

## Gateway address

Every Sandbox dials its Gateway at `SANDBOX_GATEWAY_PORT` (7412), whatever port the Host
serves it on.

| Provider | Where the fixed port leads |
|---|---|
| Docker | `ale-gateway.internal` names the Host's address on the container's bridge. The helper that holds `NET_ADMIN` in the container's network namespace keeps an `ALE_GATEWAY` nat chain sending that address's port 7412 to the Gateway's port; a reattaching run flushes and refills it. `open` Tasks run the helper for this rule alone |
| QEMU | The runner's `ALE_GATEWAY` nat chain sends `172.30.0.1:7412` from the guest bridge to the Host at the Gateway's port, and a reattaching run refills it |
| Daytona | `guestd/tunnel.py` listens at 7412 on the sandbox's loopback |
| Orchard | A relay the Provider starts as root at boot listens at `127.0.0.1:7412` in the VM and connects to the Gateway's port at the Mac, the VM's default IPv4 route; it lives until the VM is deleted |

## Snapshots

| Provider | How a snapshot is saved, booted and destroyed |
|---|---|
| Docker | `docker stop`, then `docker commit` to `ale-snapshot:<sandbox id>`. The image keeps the container's command and labels, with `ale.retention=snapshot`, `ale.prepared` naming the prepared image, and no GPU label. A container boots from its image ID; `docker rmi` destroys it and fails while a container still uses it |
| QEMU | The guest shuts down: Windows with `shutdown.exe /s`, Linux with `sync` and then `systemctl poweroff`, since its own power-off leaves files written just before it empty. The overlay is then rebased on its own base and converted into a standalone zstd-compressed qcow2 in `~/.cache/ale/qemu-snapshots/<sandbox id>/`, beside `snapshot.json` with its episode, role, OS, prepared image and disk digest. Standalone, it outlives the image cache. Compression made a 23 GB Windows disk 15 GB and exported it in 71 s rather than 82 s. A VM boots from it through a fresh overlay. Destroying it fails while a runner mounts it |
| Daytona | None: snapshots count against an organization quota shared with other tools, so `accepts` refuses them |
| Orchard | None: Orchard keeps no VM past its episode, so `accepts` refuses retention and snapshots |

## Desktop resolution

Each Provider starts a desktop at the request's `desktop_resolution` and then checks it with
`desktop.require_resolution`, which reads Cua Driver's `get_screen_size`.

| Provider | How the desktop is started at the requested size |
|---|---|
| Docker | `--env VNC_RESOLUTION=WIDTHxHEIGHT`, which replaces the image's own value; the container base's Xvnc starts at that size (`-geometry`). The name is the one VNC desktop images share, but the image's X server need not be VNC: Xvfb starts at it with `-screen 0 ${VNC_RESOLUTION}x24`. A desktop of another size is refused with that advice, since only the image can change it |
| Daytona | The same variable, in the environment the Provider starts the image's command with and runs guestd in |
| QEMU | Every guest gets QEMU's standard VGA (`VGA=std`), whose EDID names the size as the display's preferred mode (`-global VGA.xres=… -global VGA.yres=…`). OVMF sets that mode at boot, Linux's `bochs` driver and GNOME start at it, and Windows' Microsoft Basic Display Adapter keeps the mode the firmware set; it ignores the size virtio-vga, the runner's own default, announces. The display has 64 MB of video memory, since Linux's driver keeps two frames of the screen. OVMF sets no mode above 3200x1800 and falls back to 1280x800, which is why `DesktopResolution` stops there |
| Orchard | Tart keeps a VM's display size in the VM's configuration, which a clone copies and Orchard cannot set, so each resolution boots its own clone of the framed image, `ale-framed-<key>-<WIDTHxHEIGHT>`, made on first use with `tart set --display WIDTHxHEIGHTpt` and deleted when the image is framed again. The size is in points, Tart's default for macOS: a 1024x768 desktop is 2048x1536 pixels, and Cua Driver returns its captures scaled down (1366x1025 for 1024x768). macOS keeps a display narrower than 1024 points at 1024x768, which is why `DesktopResolution` starts there |

## Agent elevation on Linux and macOS

The Linux framework layer installs `sudo` with the image's package manager when the image
lacks it, and grants nothing. Every Linux and macOS Provider calls
`privileges.set_elevation` through the root guestd, then `privileges.verify_identities`,
before any Task code runs: Docker once `docker exec` has started guestd, QEMU once the
booted guest answers, Daytona before it starts the image's own command, Orchard once
guestd answers in an agent's VM. The script runs on the framework's Python:

- It replaces the last two lines of `/etc/sudoers`, a marker comment and one rule:
  `<user> ALL=(ALL:ALL) NOPASSWD: ALL` to grant, `<user> ALL=(ALL:ALL) !ALL` to revoke.
  Sudo applies the last rule that matches, so that rule decides whatever the image's own
  rules, drop-ins and groups say, including for processes the image started before it,
  such as Docker's container command and a VM's desktop session. A snapshot carries the
  rule its Sandbox had, and the next start replaces it.
- Revoking also takes the account out of `sudo`, `wheel` and `admin`, the groups that make
  administrators, which other programs trust without asking sudo: on Linux in `/etc/group`
  and `/etc/gshadow`, for polkit and others; on macOS with `dseditgroup`, since Directory
  Services keeps its groups and `admin` members approve system changes with their password.
  macOS's `/etc/sudoers` ends by including `sudoers.d`, so the appended rule comes last
  there too.
- An account that is uid 0 cannot be revoked, so `[agent].user` naming one without
  `[agent].sudo = true` is a `TaskDefinitionError`. On macOS the agent account is never
  root: the image must sign it in to a desktop, so Orchard refuses it either way.

`verify_identities` then checks `sudo -n true` as the agent, so a grant that does not work
(an image with no sudo and no package manager to install it) is refused with that reason,
as is a way up the revocation missed.

## Daytona

Daytona's sandboxes are built for a different trust model than ALE's, so `daytona.py`
works around several of its defaults to hold the same contract. Each row was measured on a
live organization (daemon 0.225.2). Operating it is in
[`docs/guides/daytona.md`](../../../../../../docs/guides/daytona.md).

| Daytona | What the Provider does |
|---|---|
| Builds sandboxes only from registries registered with the organization; push credentials expire hourly | `daytona_image.py` adds one layer (`USER root`, and `iptables` if absent), pushes it by digest to Daytona's own registry, and keeps one organization registry entry, `ale-transient`, holding the current credentials. The sandbox is built `FROM` that digest, so nothing is rebuilt remotely |
| Runs its daemon, and every command sent through it, as the image's final `USER` | That layer's `USER root`, so guestd is root |
| Replaces the image's entrypoint and command with its own daemon | Starts them itself, detached, as the image's `USER` with its `ENV` and working directory, then waits for the desktop as Docker does |
| Its daemon listens inside the sandbox (2280, 22220, 22222, 33333, and any other port PID 1 opens) and runs commands for anyone, as root | `iptables` owner rules reject every non-root user, IPv4 and IPv6; a probe as the agent must fail on every local address, or the sandbox is destroyed |
| Accepts no connection from the sandbox outward; its SSH gateway refuses `-R` | guestd runs over two SSH exec sessions, one for commands and one for file reads. A third carries `guestd/tunnel.py`, which delivers the Gateway on the sandbox's loopback; the sandbox names an index into the Host's list, never an address |
| Switches `networkBlockAll` and a domain allowlist at runtime (Tier 3 or 4 only), asynchronously | `block` uses `networkBlockAll`. `allowlist` uses `domainAllowList` with the hosts as declared, which Daytona matches as ALE does. `accepts` refuses addresses, ports and more than 100 hosts. Every switch waits for a TLS handshake with a public host the Task did not declare to behave as the setting says |
| Serves GPUs from its own region, in several models, optionally as spot capacity it may reclaim | Asks for `gpu_types` in order of preference and never spot. The GPUs are read with `nvidia-smi` as the agent and recorded; another count or model, or a GPU the agent cannot open, destroys the sandbox |
| Rate-limits creates | A create answered 429 made nothing and is retried, honoring `Retry-After`, for up to five minutes. No other failed create is repeated, since it may have made a sandbox |
| Creates a sandbox even when the caller is cancelled mid-request | Every way out of a start deletes what Daytona made; a create whose reply was lost is found by its unique name |
| Organizations are shared between tools | Labels live under `ale.run.*`, and listing and destroying act only on those. Every sandbox has a lifetime (`ttlMinutes`), so one a crashed run leaves behind is deleted by Daytona |

Daytona's allowlist, as measured: a name not on the list does not resolve. `example.com`
admits that host alone. `*.example.com` admits every name below it, at any depth, and
`example.com` itself. Ports 80 and
443 go through Daytona's proxy, which forwards by the name in the TLS handshake or HTTP
request to that name's own host, so sending an allowed name to another address reaches only
the allowed host; other ports of unlisted addresses are unreachable. The proxy accepts every
TCP connection to port 443 before it decides, which is why egress is checked with a TLS
handshake rather than a connect.

## QEMU

`QemuProvider.create` frames the disk it is handed with `framework.framed_disk`, the
function Task preparation uses for a qcow2 `ref`, so both share one cache in `vm-builds`.
Measured on a KVM host:

- A disk that already carries this framework, such as one ALE prepared, boots as it is.
  Reading its marker (`/opt/ale/framework`, or `C:\ProgramData\ALE\framework` on Windows)
  with the VM builder's `virt-cat` takes about 3 s, and the answer is recorded next to the
  cache, so each disk pays it once per Host.
- Any other disk is copied with `qemu-img convert` and framed offline, once per disk,
  framework identity and builder image. The copy is an uncompressed qcow2: about 4.5 GB for
  the 2.3 GB Linux VM base, 23 GB for the 14 GB Windows base.
- The resolved image keeps the request's prepared identity and observes the copy that
  boots, which the RunLock records.

Linux is framed by `virt-customize` in about 30–40 s. Its setup script runs inside
libguestfs's appliance VM, because it calls the disk's own programs.

Windows is framed by `framework_windows.py`, run by the builder's Python, with nothing on
the disk executed; copy and install take about a minute, and the framed disk boots to a
ready sandbox in about 25 s. Windows cannot register a logon task offline, so the
framework is a Windows service instead. It is a registry key under the control set the
disk boots from, written into the SYSTEM hive with hivex, plus files copied into
`C:\ProgramData\ALE`:

- Checks first, from the hives alone (SAM for the account, SOFTWARE for automatic sign-in
  and UAC): each failure names the one thing the disk lacks. A disk left hibernated by
  Fast Startup cannot be written, and says so.
- The service `ALEGuestd` runs `guestd/service.py` as LocalSystem on the framework's own
  Python, copied to `C:\ProgramData\ALE\python`, as is guestd. At boot it limits
  `C:\ProgramData\ALE` to the system and Administrators, because files written offline
  carry no access list and otherwise come out writable by everyone, then gives Users read
  and execute access to `cua-driver`. It opens guestd's port to the runner's address only,
  waits for the agent's sign-in, and starts guestd and Cua Driver on the agent's desktop
  with the account's elevated token (the one UAC keeps beside the limited one), each in its
  own hidden console, restarting either that exits.
- The service reads the Host's `ale-sudo` SMBIOS serial and passes it to guestd, which
  runs agent commands with the desktop's limited token unless it is set.
- A Task image build boots a framed copy of its base, so its `run.ps1` runs beside the
  same guestd as every sandbox, and the saved image already carries the framework.

## Orchard

Every macOS VM is a fresh Tart VM on the local Orchard controller and worker that
`scripts/setup_macos.py` runs; the Provider reads their address, credentials and Tart
from its state directory, so macOS needs no run settings. Setup and cleanup are in the
[macOS guide](../../../../../../docs/guides/macos-orchard.md).

A Tart disk is APFS, which only macOS reads, so `orchard_framing.py` frames an image on
the Mac, once per image, framework and account, the way the QEMU Provider frames a qcow2:
it clones the image with Tart and has `framework_macos.sh` change the clone's disk offline.
`scripts/setup_macos.py` installs that script root-owned as
`/usr/local/libexec/ale/framework-macos` with a sudoers rule for it alone, and the Provider
refuses to frame when the installed copy differs from its own. The framer attaches the disk
without mounting it, mounts only its Data volume, hidden from the Finder and without device
files or set-uid programs, follows no link on it and runs none of its programs. It checks
that the account it frames for, the Task's or `admin` for a `build.sh` VM, exists with a
home and is signed in automatically (`autoLoginUser` and `/etc/kcpassword`), then installs:

- guestd and a standalone Python under `/opt/ale` (the Python readable by every account,
  for the agent's desktop tool and the Harnesses' helper scripts);
- Cua Driver in `/Applications`, with Accessibility and Screen Recording rows in the
  system TCC database carrying the app's designated requirement, as System Settings
  writes them, and the account's replayd approval to capture without the system window
  picker, with the dates replayd checks and a reminder date far ahead;
- Remote Login and passwordless sudo for `admin`, the account Orchard logs in as.

The clone is then booted once, without the agent or the Driver, until the account is
signed in and `sfltool dumpbtm` shows its LaunchAgents registered, plus 30 seconds: macOS
posts a never-booted disk's background-item notice a few seconds after it registers the
items and reports neither the notice nor having none. Notification Center shows a notice
nobody dismissed again at every login, so the account's delivered notifications are then
cleared from its `usernoted` database, as the account, and `usernoted` is killed before it
can write them back at shutdown. `/Library/Preferences/com.apple.windowserver.displays.plist`
is deleted too: macOS restores the display mode saved there on any display of the same
identity, whatever size Tart gives it, so every Sandbox would keep the first login's
[resolution](#desktop-resolution). The guest then shuts itself down, as a `build.sh` VM's
does before its disk is kept: Orchard stops a VM with `tart stop --timeout 5`, which powers
macOS off when its signed-in desktop does not shut down at once and loses what is still in
memory. The stopped VM becomes the local Tart image `ale-framed-<key>`; every Sandbox boots
a clone of it, which on APFS copies no data.

In each Sandbox the agent works in, the Provider starts the Driver in the account's login
session (`launchctl asuser`, then `open` so LaunchServices runs it under the identity the
grants name) and waits until it captures. guestd sends desktop pointer actions with
`delivery_mode: foreground`, because the Driver otherwise refuses to move the real pointer
over a background app's window. Elevation for `[agent].sudo` is set at every boot, as on
Linux ([Agent elevation](#agent-elevation-on-linux-and-macos)). `admin` keeps passwordless
sudo for ALE, which runs as root through it, so a Task running as `admin` must declare
`sudo = true`. A separate verifier VM runs no agent, so it gets neither the desktop nor the
elevation and its checks, and a `build.sh` VM is kept exactly as the build left it.

Preparing an OCI reference resolves its tag through registry manifest metadata without
downloading VM disks or checking the worker cache, and checks the installed framer, the
local controller and an eligible ARM64 Tart worker; a local image `build.sh` built is only
looked up on the worker. Framing clones either. Provisioning checks the framed image and
the CPU and memory the controller reports, and measures the root disk from the guest.
Provisioning (1800 s, with queueing and pulls) and guest readiness (300 s) have separate
deadlines.

Orchard credentials travel through verified TLS, never command arguments or Run records.
SIP is unchanged. `orchard_desktop.py` verifies actual capture as the Task account in every
agent Sandbox before the solver starts. Desktop input uses native screenshot pixels; cursor
read-back uses logical points, following the Driver's coordinate contract.

Commands and file reads use separate authenticated WebSocket exec channels. File reads
are bounded, screenshot transfers are integrity-checked, and command output is
acknowledged after Host consumption to avoid overrunning Orchard's exec output queue.

Only explicit `network.mode: open` is supported. Restricted networking needs integration
and validation across Orchard/Tart versions and worker network configurations. GPU,
retention, snapshot and reset requests are rejected. Orchard credentials remain on the
Host; Orchard/Tart and the selected image are trusted infrastructure.

Cleanup checks the VM name and UID, waits for worker stop acknowledgement, then deletes
it. Ambiguous creation requires operator reconciliation rather than adopting a same-name
VM. Deletion is not conditional, so the controller must not replace names during cleanup.
Worker disk garbage collection is asynchronous. Cancellation, command timeout or lost
command transport tears down the VM; uncollected evidence can be lost. Cleanup errors
are recorded separately from execution failures.
