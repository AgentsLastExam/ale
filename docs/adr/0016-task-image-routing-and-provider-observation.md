# 0016 — Task image kind routes; Providers report what ran

**Status**: Superseded by [0020](0020-container-and-vm-task-images.md) (2026-08-10).
Per-request kind routing and Provider observations are retained there with the current
`core/v1` declaration and preparation contract.

## Context

Container Tasks selected their image, while QEMU ignored it and booted one ambient local
disk. A single run-level Provider switch also made a mixed container/VM selection
impossible. Resource requests could be copied into provenance without proving that the
sandbox received them.

## Decision

Every `core/v2` Task declares a structured image with `kind`, `name`, and `tag`.
`container` routes to the configured container Provider slot and `vm` routes to the VM
slot. Providers are constructed lazily and reused across selected episodes.

The selected Provider owns immutable image resolution, start, resource admission, and
ready-sandbox verification. It returns `ResolvedImage` and `ResourceAllocation`
observations. Environment and provenance code consume those observations and never
inspect Provider-private state or infer effective resources from the request.

Container Providers start a digest-qualified image. QEMU resolves an OCI-carried qcow2
for each Task and boots an overlay backed by that exact disk. Local VM disks remain
test-only, non-reportable fixtures.

## Consequences

- One command can run an ordered mix of container and VM Tasks.
- Adding another container or VM backend is a Run configuration choice, not a Task
  schema change.
- A Provider must enforce each accepted resource or fail before setup.
- RunLock can distinguish what a Task requested from what the sandbox actually received.
