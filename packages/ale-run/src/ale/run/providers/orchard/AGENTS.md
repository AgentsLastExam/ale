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
- The Provider declares `block` and `allowlist` only where setup's Softnet is the worker's,
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
