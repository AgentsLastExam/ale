# orbit-verify agent guide

This package is copied into the verification Sandbox and is a Task-author-facing public
API. Read [`README.md`](README.md) and `../../docs/specs/verification.md` first.

- Keep runtime dependencies small, pure Python and explicitly staged and never import `orbit.core` or `orbit.run`.
- Public names are exported from `orbit_verify`; underscore modules are implementation.
- Judge calls persist request and response events independently of task results.
- Judge transport/configuration failure is not a score and must remain an explicit
  exception; do not invent a zero score.
- Evidence paths are absolute regular files with no size limit of Orbit's; credentials
  must not enter records or logs.
- Update public examples and package tests when changing author-facing behavior.

Run `uv run pytest packages/orbit-verify/tests` and `just lint` from the repository root.
