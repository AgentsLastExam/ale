# ALE documentation

This directory is the maintained description of ALE. Feature work under the repository's
top-level `specs/` directory is temporary planning material; once a feature lands, its
lasting contract belongs here.

| Need | Read |
|---|---|
| Durable project principles | [constitution.md](constitution.md) |
| Canonical terms | [specs/lexicon.md](specs/lexicon.md) |
| Task definition and quality standard | [specs/task-quality-standard.md](specs/task-quality-standard.md) |
| Task authoring, folder, and manifest contract | [specs/task-authoring.md](specs/task-authoring.md) |
| `StandardEnvironment` episode flow | [specs/standard-environment.md](specs/standard-environment.md) |
| Sandbox and image contract | [specs/sandbox-image.md](specs/sandbox-image.md) |
| Verification API and records | [specs/verification.md](specs/verification.md) |
| Harness integration | [specs/autonomous-harness.md](specs/autonomous-harness.md) |
| Results, trajectories, and logs | [specs/trace.md](specs/trace.md) |
| Trust boundaries and guarantees | [specs/security.md](specs/security.md) |
| Develop the engine | [guides/development.md](guides/development.md) |
| Configure subscription login | [guides/subscription-auth.md](guides/subscription-auth.md) |
| Why current architectural choices exist | [adr/](adr/) |

Living specifications describe what is true now. ADRs retain the rationale for current
cross-module decisions; superseded alternatives remain in Git history rather than in the
reading path. Guides explain how to use the contracts without redefining them.

When code and a living specification disagree, treat that as a defect and reconcile both
in the same change. Tests and schemas enforce contracts, but they do not replace their
human-readable specification.
