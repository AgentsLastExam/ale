# 0003 - Images declare capabilities and Providers report reality

**Status:** Accepted

## Context

Inferring users, desktops, commands, interpreters, image kind, or effective resources
from names caused silent mismatches. Docker Tasks selected images while QEMU once booted
an operator-global disk, and a run-wide Provider choice could not support mixed solver
and verifier kinds.

## Decision

ALE maintains foundational CLI, GUI, and Ubuntu VM GUI images. A conforming image declares
its agent user and GUI capability, supplies system Python and guest integration, and owns
its long-lived command. Tasks build from these bases rather than naming arbitrary
upstream runtime images or shared domain images.

Every solver and dedicated verifier explicitly declares `image.kind: container|vm` and
may declare `image.ref`. A fixed local Dockerfile wins; without it, the matching Provider
must acquire the ref. Both local kinds first produce OCI content. Container execution
uses it directly; VM preparation uses ALE's versioned materializer to publish an atomic,
derivation-keyed qcow2. Task authors do not own guestd, bootloader, partition, mkosi, or
qcow conversion machinery.

`PreparedTaskImage` is the boundary between acquisition/build and execution. Each
sandbox request routes independently through the Provider registry by prepared kind.
The Provider admits the full resource request, starts that exact prepared object, and
reports immutable image and actual resource observations. QEMU grows a fresh overlay to
the requested storage and observes usable guest root capacity. Tasks request only a GPU
count; provider configuration chooses eligible devices and provenance records those
actually assigned.

The evaluated solver and oracle run as the image's unprivileged user. Trusted setup,
verification, staging, and collection run as root. Framework-private content lives under
root-only `/opt/ale`; agent work lives in the declared user's home. ALE does not repair
Task ownership after setup. A Task may request solver sudo, but the Provider verifies and
records the grant.

## Consequences

Container, VM, and mixed-kind episodes share one Environment. Failures in image
capability, resource admission, or observed identity stop before scoring. Local VM reuse
depends on final OCI and materializer identities without scanning the complete disk.
