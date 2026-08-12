# ale-verify agent guide

This package is copied into the verification Sandbox and is a Task-author-facing public
API. Read [`README.md`](README.md) and `../../docs/specs/verification.md` first.

- Keep runtime dependencies empty and never import `ale.core` or `ale.run`.
- Public names are exported from `ale_verify`; underscore modules are implementation.
- Every mutation persists a valid intermediate verification record atomically.
- Judge transport/configuration failure is not a score and must remain an explicit
  terminal verification failure.
- Evidence paths are bounded regular files; credentials must not enter records or logs.
- Update public examples and package tests when changing author-facing behavior.

Run `uv run pytest packages/ale-verify/tests` and `just lint` from the repository root.
