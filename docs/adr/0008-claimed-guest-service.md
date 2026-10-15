# 0008 - The framework claims each VM guest service

**Status:** Accepted

## Context

A VM guest service listens on TCP so the Host can reach it through the runner's forwarded
port. On Linux it runs as root, and the guest firewall accepts loopback traffic, so the
evaluated agent could connect to it and run any operation as root. The forwarded Host port
is also reachable by every local Host account and, during open phases, by other episodes.
Agent identity, `sudo: false`, and the recorded isolation were therefore not guarantees for
VM Tasks.

Container sandboxes are unaffected: their guest service is a `docker exec` stdio session
that never listens on a network.

## Decision

Every TCP guest connection must authenticate before its first operation, and the token it
must present is claimed by the framework:

- The Provider creates a fresh random token per sandbox and keeps it, readable only by the
  Host account, beside the sandbox overlay for reattachment.
- The first `auth` request a booted guest service receives claims that token. Every later
  connection must present the same token; any other first request is refused and the
  connection closed before an operation runs.
- The claim is sound because nothing but trusted image services runs in a cold-booted guest
  before the framework's first connection, which is also its readiness probe.
- Linux keeps the claimed token in root-only `/run/ale/guestd-token`, so a restarted guest
  service stays claimed while a reboot or a saved disk carries no token forward. Windows
  keeps it in memory, because its image builder saves a running guest's disk; its guest
  service already runs as the agent account.
- A guest service that does not support authentication fails closed. Linux disks the
  engine prepares carry its own guestd ([0009](0009-versioned-images-and-framework-layer.md)),
  so only a disk prepared by an older engine, or an old Windows base, needs rebuilding.

## Alternatives considered

Injecting the token at boot through QEMU `fw_cfg` or SMBIOS needs runner and kernel-module
plumbing, and command-line delivery exposes the token in Host process listings. A guest
firewall rule refusing non-root connections to the port is small but leaves the Host-side
port open and silently disappears when a Task or a Docker installation rewrites the guest
ruleset. A virtio-serial channel removes the network listener entirely but requires a new
multiplexed transport.

## Consequences

The agent, other Host accounts, and other episodes can no longer operate a VM guest. Old
Linux and Windows VM images stop working until rebuilt; the failure names the cause. A
sandbox whose overlay token is lost cannot be reattached.
