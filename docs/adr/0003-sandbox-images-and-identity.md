# 0003 - Images declare capabilities and Providers report reality

**Status:** Accepted

## Context

Inferring users, desktops, commands, interpreters, image kind, or effective resources
from names caused silent mismatches. Docker Tasks selected images while QEMU once booted
an operator-global disk, and a run-wide Provider choice could not support mixed solver
and verifier kinds.

## Decision

ALE maintains foundational Linux CLI, GUI, Ubuntu VM GUI, and private Windows 10 BYOL
bases. A conforming image declares its OS, agent user/home, and GUI capability through the
Provider-visible contract, supplies system Python and guest integration, and owns its
lifecycle. Tasks build from these bases rather than shared domain images.

Every Task explicitly declares or defaults its OS and every solver and dedicated verifier
declares `image.kind: container|vm` and may declare `image.ref`. A fixed local Dockerfile
wins where local building is supported; without it, the matching Provider acquires the
ref. Linux VM preparation uses ALE's versioned OCI materializer. The Windows base is a
private licensed seed plus a maintained ALE post-processing recipe, not a public ISO
build. Task authors do not own guestd, bootloader, partition, mkosi, or qcow conversion
machinery.

`PreparedTaskImage` is the boundary between acquisition/build and execution. Each
sandbox request routes independently through the Provider registry by prepared kind.
The Provider admits the full resource request, starts that exact prepared object, and
reports immutable image and actual resource observations. QEMU grows a fresh overlay to
the requested storage and observes usable guest root capacity. Tasks request only a GPU
count; provider configuration chooses eligible devices and provenance records those
actually assigned.

The evaluated solver and oracle run as the image's declared user. Linux trusted setup,
verification, staging, and collection run as root under `/opt/ale`. The first Windows
base uses the interactive user's guestd session and `C:\ProgramData\ALE`; phase ordering
withholds later-stage content. Cua Driver is the sole guestd GUI backend on both OSes. A
Task may request solver sudo/admin, but the Provider verifies and records the grant.

## Consequences

Container, VM, and mixed-kind episodes share one Environment. Failures in image
capability, resource admission, or observed identity stop before scoring. Local VM reuse
depends on final OCI and materializer identities without scanning the complete disk.
Every VM episode cold-boots a fresh overlay; ready snapshots and warm pools are deferred.
