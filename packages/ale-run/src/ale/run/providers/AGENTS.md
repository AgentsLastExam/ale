# Provider agent guide

Read [`README.md`](README.md) and `docs/specs/sandbox-image.md` before changing a backend.

- Implement the public Provider/Sandbox contracts; do not expose Docker or QEMU details
  to Harnesses or Environments.
- Record what actually ran: resolved image, allocation, account, network and retention.
- Lifecycle operations must be idempotent enough for cancellation and failure cleanup.
- Never infer capability success; preflight and image checks return actionable failures.
- Keep Docker and QEMU behavior local unless an identical operation has the same failure
  semantics in both backends.
- Mark real-runtime tests with `needs_docker`, `needs_kvm`, `needs_gui` or `needs_gpu`.

Run the matching conformance suite and backend integration tests before merging.
