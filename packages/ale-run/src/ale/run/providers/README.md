# Providers

Providers turn prepared Task images into Sandboxes. ALE ships three backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container | immutable OCI image identity |
| `qemu.py` | QEMU/KVM guest runner | materialized qcow2 identity |
| `daytona.py` | Daytona cloud container | immutable OCI image identity, pushed by digest |

`daytona_api.py` is its REST client and `daytona_image.py` adapts and publishes images.

All three implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and episode
protocol code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).

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
| Switches `networkBlockAll` and a domain allowlist at runtime (Tier 3 or 4 only), asynchronously | `block` uses `networkBlockAll`. `allowlist` uses `domainAllowList`, each host written `*.host` because a bare entry does not match subdomains; addresses, ports, patterns and more than 100 hosts are refused in `accepts`. Every switch waits for a TLS handshake with a public host the Task did not declare to behave as the setting says |
| Serves GPUs from its own region, in several models, optionally as spot capacity it may reclaim | Asks for `gpu_types` in order of preference and never spot. The GPUs are read with `nvidia-smi` as the agent and recorded; another count or model, or a GPU the agent cannot open, destroys the sandbox |
| Rate-limits creates | A create answered 429 made nothing and is retried, honoring `Retry-After`, for up to five minutes. No other failed create is repeated, since it may have made a sandbox |
| Creates a sandbox even when the caller is cancelled mid-request | Every way out of a start deletes what Daytona made; a create whose reply was lost is found by its unique name |
| Organizations are shared between tools | Labels live under `ale.run.*`, and listing and destroying act only on those. Every sandbox has a lifetime (`ttlMinutes`), so one a crashed run leaves behind is deleted by Daytona |

Daytona's allowlist, as measured: a name not on the list does not resolve. Ports 80 and
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

- Checks first, from the hives alone (SAM for the account, SOFTWARE for automatic sign-in,
  UAC and Python 3.12+ for all users): each failure names the one thing the disk lacks.
  A disk left hibernated by Fast Startup cannot be written, and says so.
- The service `ALEGuestd` runs `guestd/service.py` as LocalSystem. At boot it limits
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
