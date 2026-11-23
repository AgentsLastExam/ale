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
labelled with what the snapshot holds (`orbit.firecracker.written_mb`), so a later Run that
drives a kept microVM again counts it too. One whose `storage_mb` the snapshot already fills
is refused before it boots, as the Task's error.

**Decisions.**
- Measured in the guest, before the shutdown, rather than from the sparse copy's blocks:
  the guest's `df` counts what was written on that filesystem, as a disk of that size would
  fill, while the copy's blocks also count the filesystem's own structures.
- A `storage_mb` the snapshot fills is the Task's error, as on Docker, whose watch ends such
  a Sandbox as storage-exhausted at once, the Task's result in a verifier of its own.

## A Sandbox that will be saved can boot from a snapshot it cannot save again

**Problem.** A microVM stacks at most eight layers, so one booted from a snapshot of eight
fails when it is saved, after the agent's whole run, as a snapshot verifier's solver or a
solver kept as a snapshot would be.

**Solution.** A request says how many snapshots in a row its episode may save
(`SandboxRequest.saves`), counted as
[Provider selection](../../../../../../../docs/specs/sandbox-image.md#provider-selection)
says. The profile declares the limit (`max_saves = 8`), so selection passes a Task over, or
refuses its Run, before anything starts when it plans more; `create` refuses, before it
boots anything, a request whose snapshot's layers and saves together exceed eight, as one
booted from a snapshot an earlier episode left may.

**Decisions.**
- Refused rather than flattened: merging the layers would copy the whole disk, which the
  layers exist to avoid.

## Volumes on a microVM

**Problem.** A separate verifier of another image must see the solver's volumes as its
snapshot keeps them without a copy through the Host, and a microVM's devices are few.

**Solution.** The init mounts each volume as an overlay of the image's directory and of
what each layer wrote for it, `volumes/<hash>/upper` on that layer's disk, under a layer on
the writable disk, apart from the root's own upper. A snapshot keeps it as it keeps the
root, with no file of its own. A separate verifier attaches the snapshot's root and layer
disks read-only after its own, at most nine more drives beside its own two, within
Firecracker's 19 interrupt lines, and mounts the same layers under one of its own.

**Decisions.**
- On the writable disk rather than a disk per volume: no more devices, and `storage_mb`
  counts it as it counts the rest.
- Named by a hash of the path, the same in every snapshot, so a verifier finds a volume's
  layers without a record of their order.
- The verifier detaches the snapshot's disks once the overlays hold them, so the rest of
  the solver's disk is not in its tree.
