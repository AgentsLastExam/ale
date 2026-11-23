# Orchard Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#orchard), its sealing in
[Restricted networking](../README.md#restricted-networking). This file records the
problems peculiar to it: what we saw, what we measured, and what we decided.

## macOS VMs could run only `open` Tasks

**Problem.** The Provider refused `block` and `allowlist`, so a macOS Task had to declare
`public` whatever it needed.

**Context.** Tart's default network is Virtualization.framework's NAT, which nothing on the
Mac filters per VM. Tart's Softnet filters a VM's frames on the Mac, outside the guest,
and enforces its MAC and IPv4 identity; Softnet 0.25.0 matches protocols and ports, and
Orchard 0.57.0's worker (with Tart 2.34.0 or later) changes a running VM's Softnet rules in
place. Softnet needs root on the Mac to start and macOS 26 or later, and it sets the Mac's
DHCP lease time to 600 seconds for every VM.

**Solution.** Restricted VMs boot on Softnet with every IPv4 address allowed; `close_egress`
replaces the rules with the sealed ones and `open_egress` puts the open ones back, through
`set_softnet_policy`. `scripts/setup_macos.py` installs Softnet root-owned and
set-user-ID, and provisioning refuses a restricted Task on a Mac without it.

**Decisions.**
- Softnet rather than `pf` on the Mac: `pf` rules keyed on a VM's address miss a guest
  root that changes or spoofs it, and nothing proves vmnet's bridge passes every frame
  through `pf`. Guest firewalls were ruled out, since the agent may be root.
- The sealed rules allow `in proto tcp @host port 22`, Orchard's own exec path, and only
  the episode's Gateway and proxy ports out; `@host` DNS stays shut.
- Softnet is set-user-ID, as Tart documents, not run through a sudoers rule like the
  framer: Tart starts it by name from the worker's PATH. Any local account may run it, and
  it is one file per Mac, so every checkout on a Mac must pin the same release.
- The Provider declares `block`, `allowlist` and `blocklist` only where setup's Softnet is
  the worker's,
  so a Fleet never places them on a Mac that would refuse them.

## A policy change strands Orchard's SSH connection

**Problem.** After a Softnet change, the next exec into the VM hung.

**Context.** The controller carries every exec session of a VM over one pooled SSH
connection, and Softnet clears its flow table when the rules change, so the pooled
connection's packets are dropped without either end being told. In a macOS 26 guest
`pgrep` and `pkill` do not see `sshd-session` processes, though `ps` lists them (measured on
Cirrus Labs' Tahoe base, November 2026).

**Solution.** Before a change, both guest sessions close and `end_exec_connection` stops the
`admin` account's `sshd-session` processes from inside, found with `ps`, while the old rules
still carry the close; the next session opens a new connection. Orchard reports that
command's session as failed, not with an exit code; the command never exits by itself, so
any exit means the connection survived, and the change fails rather than strand the next
session.

## A worker may restart the VM instead of changing its rules

**Problem.** Orchard's worker falls back to stopping and starting a VM when an in-place
change fails, which would hand the agent a fresh VM with none of the framework's processes.

**Context.** Orchard records no such restart: `restart_count` counts its scheduler's
restarts and `started_at` is set once.

**Solution.** A change fails when the VM's `running` condition reads false while it waits
for `observedGeneration`, or when the guest's `kern.bootsessionuuid` differs afterwards.

## Softnet forgets idle connections

**Context.** Softnet drops a connection the VM sent nothing on for five minutes, as a long
model call through the Gateway can be.

**Solution.** The in-VM Gateway relay, and guestd's egress relay toward the proxy, turn on
TCP keepalive after a minute of silence. Orchard's SSH connection into the VM needs none: a
sealed command silent for 400 seconds completed, and the next one ran at once.

## The macOS system proxy

**Context.** Safari, Chrome and other CFNetwork clients take their proxy from the network
services' settings, which are the whole system's. `networksetup` sets them as root but
cannot remove a web proxy's server, only turn the proxy off. Python's urllib reads them
only when no proxy variable at all is set, `NO_PROXY` included.

**Solution.** guestd points every enabled network service's web and secure web proxies at
the relay and restores, by service, what still names it: a service turned off meanwhile is
left off, and a proxy the account never set comes back off, with the relay's address left
in it, unused. A proxy that required authentication comes back without it, since
`networksetup` sets one without credentials. Measured with Safari on the Tahoe base: the
declared host loads and another is refused.

## A macOS VM that runs out of its memory

**Problem.** Orbit cannot tell a macOS Sandbox lost to memory from one lost otherwise, so
such a loss stays an environment error, which is retried.

**Context.** Measured on a 16 GB Mac mini on macOS 27, with Orchard 0.58, and a 4096 MiB VM
(November 2026):
- macOS swaps to the guest's own disk, about 11 GB free at boot. One process, writing
  incompressible memory, ran to 13.7 to 18.4 GiB before macOS killed it
  (`memorystatus: triggering no paging space action`), after 25 to 50 s; `exec` returned
  exit -9 and the VM answered at once.
- 400 processes of 16 MiB lived on in swap. 400 of 48 MiB, past memory and free disk, froze
  the guest at about 12 GiB: guestd, a new SSH session and Tart's guest agent stopped
  answering, the VM's two vCPUs ran flat out, Orchard reported it running, and the Host
  itself reached memory pressure after five minutes. Orbit's command ended after its timeout,
  or with `GuestUnreachableError` once Orchard dropped guestd's channel.
- A RAM disk needs no privilege (`hdiutil attach -nomount ram://...`) and fills to its size
  or until macOS drops it (at 12 GiB of 20), swapped like any memory; the VM lived.
- Nothing outside the guest records why: Orchard's VM log and the worker's log are empty,
  and Tart keeps no console.

**Solution.** None: a lost macOS Sandbox is an environment error, and the shared
conformance test of such a loss is skipped for Orchard.

**Decisions.**
- No attribution from pressure readings: a watcher in the guest saw pressure level 4 and
  shrinking disk for 30 s before the freeze, but pressure is no exhausted limit, and the
  readings stop with the guest.
