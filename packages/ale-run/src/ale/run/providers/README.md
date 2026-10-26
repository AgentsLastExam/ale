# Providers

Providers turn prepared Task images into Sandboxes. ALE ships the following backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container | immutable OCI image identity |
| `qemu.py` | QEMU/KVM guest runner | materialized qcow2 identity |
| `daytona.py` | Daytona cloud container | immutable OCI image identity, pushed by digest |
| `orchard.py` | macOS VM on Orchard/Tart | digest-pinned external OCI image |

`daytona_api.py` is its REST client and `daytona_image.py` adapts and publishes images.

They implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and Environment
code must use those contracts rather than backend-specific methods.

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

## Orchard

Orchard creates a fresh Tart VM per Sandbox. Select it with `vm.provider = "orchard"`;
QEMU remains the default. Local Apple Silicon deployments are validated; remote workers
are unvalidated. Setup, configuration and cleanup are in the
[macOS guide](../../../../../../docs/guides/macos-orchard.md).

Images provide Python 3.12+, the Task account and its home, required applications, and
an execution account with passwordless sudo for guest bootstrap. ALE installs guestd
in each new VM without changing the shared image; it does not build macOS images.
Desktop Tasks use either a preauthorized CuaDriver service in the Task account's login
session or the optional `desktop_bootstrap` flow described in the guide.

Preparation resolves OCI tags through registry manifest metadata without downloading
VM disks or checking the worker cache. VM creation uses that pinned digest even if the
tag changes afterward. Provisioning checks the controller-reported image digest and
CPU/memory allocation; this trusts Orchard rather than independently measuring the disk.
Both stages check authenticated cluster access and an eligible ARM64 Tart worker.
Orchard owns scheduling; provisioning and guest readiness have separate deadlines.

`orchard_permissions.py` installs the pinned, signed Driver and authorizes it through
System Settings. The `macos_permission_*` helpers isolate Host-side VNC/OCR dependencies
from guestd. Passwords travel through a private pipe and verified TLS, never command
arguments or Run records. SIP and TCC databases are unchanged. Helpers and tunnels close
on success, failure and cancellation; `orchard_desktop.py` verifies actual capture as
the Task account before the solver starts. Desktop input uses native screenshot pixels;
cursor read-back uses logical points, following the Driver's coordinate contract.

Commands and file reads use separate authenticated WebSocket exec channels. File reads
are bounded, screenshot transfers are integrity-checked, and command output is
acknowledged after Host consumption to avoid overrunning the relay's output queue.
For local default NAT, the Provider discovers the Host Gateway address from the guest's
IPv4 route; `gateway_host` overrides it for other network layouts.

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
