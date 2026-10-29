# ALE documentation

This directory is the maintained description of ALE. Feature work under the repository's
top-level `specs/` directory is temporary planning material; once a feature lands, its
lasting contract belongs here.

| Need | Read |
|---|---|
| Durable project principles | [constitution.md](constitution.md) |
| Canonical terms | [specs/lexicon.md](specs/lexicon.md) |
| Task definition and quality standard | [specs/task-quality-standard.md](specs/task-quality-standard.md) |
| Task engineering: directory, configuration, environment, visibility, and stage interfaces | [specs/task-authoring.md](specs/task-authoring.md) |
| When to prepare Task-local offline software on Linux or Windows | [specs/task-authoring.md#software-assets](specs/task-authoring.md#software-assets) |
| Build Windows Task images and prepare reusable MSIX assets | [guides/windows-task-images.md](guides/windows-task-images.md) |
| Task asset versions, local checkpoints, and Hugging Face publication | [specs/task-assets.md](specs/task-assets.md) |
| Episode protocol: phases of one episode | [specs/episode-protocol.md](specs/episode-protocol.md) |
| Sandbox and image contract | [specs/sandbox-image.md](specs/sandbox-image.md) |
| Verification API and records | [specs/verification.md](specs/verification.md) |
| Harness integration | [specs/autonomous-harness.md](specs/autonomous-harness.md) |
| Results, trajectories, and logs | [specs/trace.md](specs/trace.md) |
| Trust boundaries and guarantees | [specs/security.md](specs/security.md) |
| Develop the engine | [guides/development.md](guides/development.md) |
| Configure subscription login | [guides/subscription-auth.md](guides/subscription-auth.md) |
| Run episodes on Daytona | [guides/daytona.md](guides/daytona.md) |

Living specifications describe what is true now and, briefly, why; superseded
alternatives remain in Git history. Guides explain how to use the contracts without
redefining them.

When code and a living specification disagree, treat that as a defect and reconcile both
in the same change. Tests and schemas enforce contracts, but they do not replace their
human-readable specification.
