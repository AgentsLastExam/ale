# Development

## The environment contract

One rule makes everything else work: **the committed `uv.lock` is the only source of
truth, and every command goes through `uv run`.** No virtualenv activation, no
`pip install`, no hand-built environments.

```bash
just bootstrap        # uv sync --frozen + pre-commit hooks
uv run pytest -m unit
uv run ale --help
```

## Worktrees

Worktrees live in `../ale-worktrees/<name>`, one per feature or track:

```bash
just wt us1-gateway   # creates the worktree, branch, and a ready-to-use environment
cd ../ale-worktrees/us1-gateway
just doctor
```

### Why each tree gets its own `.venv`

Workspace members (`ale-core`, `ale-run`) are installed as *editables pointing at
absolute paths*. A venv shared between worktrees would therefore import another
tree's source — silently, and only sometimes. So sharing is not a shortcut, it is a
bug generator.

Speed comes from elsewhere: uv's global cache (`~/.cache/uv`) holds already-built
wheels and installs them into a new tree by **hardlink**, so a fresh worktree is ready
in seconds and nothing is rebuilt. `just doctor` verifies that the cache and your
checkout are on the same filesystem, because hardlinks cannot cross filesystems — that
is the one condition under which installs silently degrade to copies.

### Shared state, by default

| What | Where | Note |
|---|---|---|
| Built wheels | `~/.cache/uv` (uv default) | shared by every tree; hardlinked in |
| Task checkouts and guest disks | `~/.cache/ale` (`ALE_CACHE_DIR`) | a new worktree can reuse operator-managed runtime artifacts |
| Task asset sync state | each Task repository's `.ale-cache/` | generated commit/dirty marker; ignored by Git |
| Task assets | each Task's `{image,setup,verify,oracle}/assets/` | direct development paths; explicit HF pull/push/status |
| Secrets | checkout `.env` (`ALE_ENV_FILE` overrides) | solver keys stay on the Host; configured Judge keys exist only for the verifier command |

Set `ALE_REPO_PATH` to the absolute active engine checkout for engine discovery. Configure
`ALE_ASSETS_COLLECTION` only when synchronizing Task assets. Runtime never downloads
assets, and Tasks without stage-local asset directories require no collection or token.

## When something looks wrong

Run `just doctor` first. It checks the interpreter, lockfile sync, venv freshness,
cache locations and filesystem, secrets, container runtime, KVM and QEMU, and prints
the exact command that fixes each failure. It is standard-library only, so it works
even before `uv sync`.

## Host prerequisites

| Backend | Requirement | Status check |
|---|---|---|
| container (default) | Docker Engine (or Podman) reachable | `just doctor` |
| virtual machine | `/dev/kvm` plus `qemu-system-x86_64`, `qemu-img` | `just doctor` |

Machines without KVM can still run everything on the container backend; machines
without a container runtime cannot run the default path.

## Workflow

This repository is developed primarily by one maintainer, so the process is
deliberately light:

- **Commit straight to `main`.** No pull request is required for your own work; CI
  runs on every push and is the safety net.
- Use a worktree (`just wt <name>`) when you want parallel tracks or a risky change,
  not as a ritual for every edit.
- Keep the *substance* of the norms even where the ceremony is dropped: when a change
  adds an abstraction, the commit message says what it buys and what it replaces; when
  it borrows from an existing implementation, it says what was deliberately changed.
- Run `just lint && just test` before pushing. That is the whole checklist.

## Conventions

- Everything in this repository is written in standard English.
- Names come from [the lexicon](../specs/lexicon.md); each has one meaning.
- Start at [`docs/README.md`](../README.md), read the owning specification before
  changing a contract, and update the relevant ADR when its architectural rationale
  changes.
- Commit per task or logical group; keep `uv.lock` committed and in sync.
