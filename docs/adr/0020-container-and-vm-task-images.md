# ADR 0020: Explicit container and VM Task images

**Status:** Accepted
**Date:** 2026-08-10
**Supersedes:** the image-kind, Docker-only, and verifier-image clauses of
[0016](0016-task-image-routing-and-provider-observation.md),
[0017](0017-self-contained-docker-tasks.md), and
[0019](0019-stage-local-task-assets-and-verification-topology.md)

## Context

ALE already had Docker and QEMU Providers, but standard Tasks could only author a local
container Dockerfile. Solver refs were unavailable, verifier refs were container-only,
and one run-wide Provider selection could not execute mixed solver/verifier kinds.

## Decision

Every solver declares `image.kind: container|vm` and may declare `image.ref`. A dedicated
verifier uses the identical nested `verify.image` shape. There is no inferred kind or
build flag. The fixed source rule is:

1. `image/Dockerfile` (or `verify/Dockerfile`) exists: build that local context; any ref
   is ignored and build failure is terminal.
2. No Dockerfile exists: `ref` is required and the matching Provider acquires it.
3. A separate verifier with neither source reuses the complete prepared solver image.

Both local kinds first produce an OCI image. Container preparation uses that image
directly. VM preparation passes its exported root filesystem to ALE's versioned
materializer and atomically publishes one derivation-named qcow2. Tasks inherit the
Ubuntu 24.04 full-GNOME `sandbox-base-vm-gui` and never own guestd, bootloader,
partitioning, mkosi, or qcow conversion machinery.

`PreparedTaskImage` is the boundary between preparation and execution. Each sandbox
request carries its prepared kind, and the existing Provider registry selects the
matching Provider per request. The standard Environment and verification lifecycle are
unchanged; a separate verifier is a fresh sandbox and receives only declared artifacts.

Provider preparation owns ref acquisition and immutable resolution. Providers report
the image and resource allocation they actually started. `ale prepare` exposes ordered
preparation stages without starting an episode. RunLock schema 2 records declaration,
prepared identity, Provider, and observed identity; the version remains unchanged while
the project is in pre-release development.

## Consequences

- Container, VM, and mixed-kind episodes share one orchestration path.
- Dockerfiles remain ordinary, independently reproducible Task sources with BuildKit
  layer reuse.
- Local VM reuse is keyed by final OCI and materializer identities; ALE never hashes the
  complete qcow2 to decide reuse.
- QEMU sizes every writable overlay from `storage_mb`, grows the guest root filesystem,
  and records observed usable `/` capacity as the effective allocation.
- Runtime Provider capabilities still decide whether a prepared image can be admitted
  and booted on a given Host.
