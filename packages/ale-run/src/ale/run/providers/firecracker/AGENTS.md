# Firecracker Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#firecracker). This file
records the problems peculiar to it: what we saw, what we measured, and what we decided.

## A microVM booted from a snapshot was held to its own writes alone

**Problem.** A microVM booted from a snapshot, as a snapshot verifier or a continuation is,
got a fresh writable disk of its whole `storage_mb` over the snapshot's layers, however
much those already held, so it had more room than a disk of that size holding the snapshot
(November 2026, found comparing Providers for snapshot verification).

**Context.** The snapshot's layers are read-only disks under the fresh writable one, and
overlayfs reports only the writable disk's space.

**Solution.** A save records `written_mb` in `snapshot.json`: the writable disk's use as the
guest's `df /` shows it before the shutdown, plus what the snapshot it booted from held. A
microVM booted from it gets a writable disk of `storage_mb` less that, plus the overhead,
and its recorded storage is that disk's capacity plus what the snapshot holds. Its runner is
labelled with what the snapshot holds (`ale.firecracker.written_mb`), so a later Run that
drives a kept microVM again counts it too. One whose `storage_mb` the snapshot already fills
is refused before it boots, as the Task's error.

**Decisions.**
- Measured in the guest, before the shutdown, rather than from the sparse copy's blocks:
  the guest's `df` counts what was written on that filesystem, as a disk of that size would
  fill, while the copy's blocks also count the filesystem's own structures.
- A `storage_mb` the snapshot fills is the Task's error, as on Docker, whose watch ends such
  a Sandbox as storage-exhausted at once, the Task's result in a verifier of its own.
