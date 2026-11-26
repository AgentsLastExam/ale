# Development

## The environment contract

One rule makes everything else work: **the committed `uv.lock` is the only source of
truth, and every command goes through `uv run`.** No virtualenv activation, no
`pip install`, no hand-built environments.

```bash
just bootstrap        # uv sync --frozen + pre-commit hooks
uv run pytest -m unit
uv run orbit --help
```

## Worktrees

Worktrees live in `../orbit-worktrees/<name>`, one per feature or track:

```bash
just wt us1-gateway   # creates the worktree, branch, and a ready-to-use environment
cd ../orbit-worktrees/us1-gateway
just doctor
```

### Why each tree gets its own `.venv`

Workspace members (`orbit-core`, `orbit-run`) are installed as *editables pointing at
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
| Task checkouts and guest disks | `~/.cache/orbit` (`ORBIT_CACHE_DIR`) | a new worktree can reuse operator-managed runtime artifacts |
| Task asset sync state | each Task repository's `.orbit-cache/` | generated commit/dirty marker; ignored by Git |
| Task assets | each Task's [asset roots](../specs/task-assets.md) | direct development paths; explicit HF pull/push/status |
| Secrets | checkout `.env` (`ORBIT_ENV_FILE` overrides) | `ORBIT_`-prefixed names from [`.env.example`](../../.env.example), and the [Task variables](../specs/task-authoring.md#variables) the selected Tasks refer to; provider and Judge keys stay on the Host Gateway |
| Default run configuration | `ORBIT_RUN_CONFIG` points to a run TOML | used when `--config` is absent; see [`orbit-run`](../../packages/orbit-run/README.md) for precedence |

Set `ORBIT_REPO_PATH` to the absolute active engine checkout for engine discovery. Configure
`ORBIT_HF_TOKEN` to push Task assets or pull them from a gated or private dataset, with an account
that dataset has approved. The `assets`
collection of `agents-last-exam`, or of `ORBIT_ASSETS_NAMESPACE` when set, is discovered
automatically; see [Task assets](../specs/task-assets.md). Runtime never downloads assets,
and Tasks without stage-local assets require no collection or token.

## When something looks wrong

Run `just doctor` first. It checks the interpreter, lockfile sync, venv freshness,
cache locations and filesystem, secrets, container runtime, KVM and QEMU, and prints
the exact command that fixes each failure. It is standard-library only, so it works
even before `uv sync`.

## Host prerequisites

| Backend | Requirement | Status check |
|---|---|---|
| container (`docker:runc`, default) | Docker Engine reachable, with its `runc` runtime | `just doctor` |
| container in a Kata microVM (`docker:kata`) | Docker, `/dev/kvm`, Kata's shim on the daemon's PATH and `vhost_vsock` ([Kata](../../packages/orbit-run/src/orbit/run/providers/README.md#docker-on-kata-containers)) | `just doctor` for KVM; the Run's preflight names a missing shim or `/dev/kvm` |
| container on gVisor (`docker:gvisor`) | Docker with Orbit's `runsc` registered as `runsc`, `runsc-net-raw` and `runsc-nested` ([install](../../images/runtimes/gvisor/README.md#install-on-a-host)) | the Run's preflight names a missing runtime or argument, another build of `runsc` or one without the GPU memory limit, or a GPU driver nvproxy does not support |
| virtual machine (`qemu`, default) | `/dev/kvm` plus `qemu-system-x86_64`, `qemu-img` | `just doctor` |
| microVM (`firecracker`) | Docker, `/dev/kvm` and `/dev/net/tun`; `images/build.sh orbit-firecracker-runner` until the runner is published | `just doctor` for KVM; the Provider names anything else missing |
| macOS VM (`orchard`, default) | an Apple Silicon Mac running the local Orchard of `scripts/setup_macos.py` | `uv run scripts/setup_macos.py --check` |

An x86-64 machine without KVM still runs every Linux container Task that `docker:runc` or
`docker:gvisor` serves, but not one that needs `perf`, `bpf` or `io_uring`, which only Kata
and Firecracker give here; one without a container runtime cannot run the default path.
The Linux Tasks of Agents' Last Exam are x86-64, so Docker on an arm64 machine, such as an
Apple silicon Mac, runs none of them.

## Choose Providers

A Run starts its Sandboxes through the Providers its run configuration lists by provider id,
most preferred first, as [Provider selection](../specs/sandbox-image.md#provider-selection)
says. The default sends container Tasks to Docker under runc, Linux and Windows VMs to QEMU
and macOS VMs to the local Orchard:

```toml
[sandbox]
providers = ["docker:runc", "qemu", "orchard"]  # the default

[sandbox.docker]
gpus = [0, 1]  # the Host GPU indices a Docker leaf may lease; default all
gpu_memory_margin_mb = 4096  # free beyond a shared Sandbox's gpu_memory_mb; default 4096
```

A Task that names `gpu_memory_mb` shares each of its GPUs with other such Sandboxes under
`docker:gvisor`, which limits each to its share, and under `docker:runc`, which only watches
it and kills a process past it; the Run names each Task it watches at its start. On a Host
whose GPUs other users' jobs share, raise the margin to what their jobs grow by after they
start ([GPU memory](../../packages/orbit-run/src/orbit/run/providers/README.md#gpu-memory)).

A Docker leaf holds a Task's `storage_mb` by measuring what its Sandbox writes and ending
the Sandbox once that passes the limit, so leave the Host's disk room for what Sandboxes
write between two measurements, a few GiB at a fast disk's speed
([Docker writable storage](../../packages/orbit-run/src/orbit/run/providers/README.md#docker-writable-storage)).

| Provider id | Starts |
|---|---|
| `docker:runc` | Linux containers in local Docker |
| `pbs` | Single-node Linux containers on PBS Professional, with rootless gVisor ([queue configuration and the Flow2 preset](../../packages/orbit-run/src/orbit/run/providers/README.md#pbs-professional)) |
| `slurm` | Linux containers, on one node or several, in batch allocations of the cluster whose login node Orbit runs on, one per solver and the Sandboxes booted from its snapshots ([configure partitions](../../packages/orbit-run/src/orbit/run/providers/README.md#slurm)) |
| `docker:kata` | each Linux container in a Kata microVM of its own, in local Docker |
| `docker:gvisor` | each Linux container on gVisor's kernel in user space, in local Docker |
| `firecracker` | each Linux container as a Firecracker microVM |
| `qemu` | Linux and Windows VMs |
| `orchard` | macOS VMs on this Mac's local Orchard |
| `daytona` | Linux containers on Daytona's cloud; see [Daytona](daytona.md) |
| `fleet` | any Sandbox, on the Nodes of a Fleet; see [Fleet](fleet.md) |

What each one gives, [container features](../specs/task-authoring.md#container-features)
included, is in
[Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#provider-ids-and-profiles).
With `["docker:runc", "docker:kata", "qemu", "orchard"]`, a Task that needs a feature runc
lacks, such as `perf`, runs on Kata while every other container Task stays on runc.
`--set 'sandbox.providers=["docker:kata", "qemu"]'` replaces the list for one command.

A Run with a Sandbox that no listed Provider serves is refused before anything is prepared,
naming each such Sandbox and each Provider's reason. Before the first episode, each chosen
leaf runs its preflight, which finds its runtime, and one whose Tasks need container
features probes them in a throwaway Sandbox for each set they ask for; `docker:gvisor`
checks in its first such Sandbox, or in one started for that alone, that it runs on gVisor.
`orbit prepare` prints the Provider chosen for each image.

## Workflow

This repository is developed primarily by one maintainer, so the process is
deliberately light:

- **Commit straight to `main`.** No pull request is required for your own work; CI
  is the safety net. Every push to `main` runs everything; a pull request runs lint and
  the unit suites, and the slow backend suites run on demand
  (`gh workflow run ci.yml --ref BRANCH`).
- Use a worktree (`just wt <name>`) when you want parallel tracks or a risky change,
  not as a ritual for every edit.
- Keep the *substance* of the norms even where the ceremony is dropped: when a change
  adds an abstraction, the commit message says what it buys and what it replaces; when
  it borrows from an existing implementation, it says what was deliberately changed.
- Run `just lint && just test` before pushing. That is the whole checklist.

## Task repository CI

Task repositories, including `ale-tasks-base`, call the public
[`task-check.yml`](../../.github/workflows/task-check.yml) reusable workflow at `main`.
Their `tasks` workflow keeps only `push`/`pull_request` triggers, read-only contents
permission, and a `check` job using
`AgentsLastExam/orbit/.github/workflows/task-check.yml@main`. GitHub reports the nested
check as `check / check`; required-check rules must use that name when enabled.
Updating the shared workflow affects each caller's next run, not already recorded runs.

The shared workflow statically lints Tasks and compiles Python source on Python 3.12.
It does not execute Task code, pull images, download assets, or call models. Brew
Workers own actual submitted-Task calibration (`orbit calibrate`), beta review, and solver
runs against the recorded source/assets revisions. Engine integration and Judge regression
tests remain in this repository's `ci` workflow. A passing static check is not build
evidence.

## Conventions

- Everything in this repository is written in standard English.
- Names come from [the lexicon](../specs/lexicon.md); each has one meaning.
- Start at [`docs/README.md`](../README.md) and read the owning specification before
  changing a contract.
- Commit per task or logical group; keep `uv.lock` committed and in sync.
