# Task folder specification

Normative for the default loader (`ManifestTaskset`). Tasks that need custom loading
may bypass the layout, but the manifest field semantics still apply.

## Repository layout

```
<task-repo>/
├── domain.yaml              # one per domain namespace
├── assets.lock.yaml         # only if the domain has large assets
├── kits/<name>/kit.yaml     # optional sandbox-side shared code
├── images/<name>/Dockerfile # optional; MUST derive from an official base image
└── tasks/[<group>/]<task>/  # task folders; id = <domain>/[<group>/]<task>
```

```yaml
# domain.yaml — deliberately almost empty. Data, images and collection are per task,
# because that is where the knowledge lives.
name: demo
requires_core: ">=0.1,<0.2"
```

## Task folder

```
<task>/
├── task.yaml          # required — manifest            (invisible to the agent)
├── instruction.md     # required — the prompt          (visible, after rendering)
├── verify/            # required — entry verify.sh|.py (invisible; injected at verify)
├── oracle/            # recommended — entry solve.sh   (invisible)
├── setup/             # optional — scripts run in the sandbox before the agent
└── files/             # optional — small task files, ≤10 MB each (visible)
```

Visibility is enforced structurally: invisible entries are never uploaded during setup
or the agent phase.

## Manifest

```yaml
spec_type: core/v1                 # default
environment: core/standard         # default; extensions referenced by name
image: sandbox-base-cli
resources: { cpus: 1, memory_mb: 1024 }
network: { mode: block }           # block | allowlist (+ allowed_hosts) | open
timeouts: { setup: 120, agent: 900, verify: 300 }

setup:
  assets:                          # each mount says where the data is and where it goes
    - repo: agents-last-exam/ale-tasks-assets
      revision: 1d0d026c…          # a commit: two runs naming it read the same bytes
      path: demo/hello/base/input
      dest: /ale/input
  kits: [data-prep]
verify:
  assets:
    - { repo: …, revision: …, path: demo/hello/base/reference, dest: /ale/reference }
  kits: [grader-protocol]

artifacts:
  - { path: /ale/output }          # collect: host (default) | none

params: { n: 3 }
variants:
  - { name: base }
  - { name: hard, params: { n: 10 } }
validate: { min_reward: 1.0 }
```

The identifier is derived from the folder path (`tasks/demo/hello` → `demo-hello`) and
never written in the file. It is opaque: the domain and the variant are separate fields,
and nothing in the framework parses an identifier (ADR 0006).

Scripts are not declared. A stage's folder is copied into the sandbox when that stage
runs, and its entry point (`setup/run.sh`, `verify/run.sh`) executes if present.

## Paths

**A task decides where its own data goes.** There is no framework-wide layout: a mount's
`dest` and an artifact's `path` are absolute paths chosen by the task, so a simulation
domain can put scenes at `/opt/sim/scenes` and nothing has to be adapted.

The framework creates the mount destinations, the artifact paths, and the run's scratch
directory (`work_dir`, default `/ale/work`, configured per run rather than per task).
Anything else a task needs, it creates in its own setup script.

The framework keeps a small namespace of its own — `/ale/kits`, the stage directories,
and the rewards file — because it has to put its machinery somewhere.

What keeps gold answers away from an agent is **timing, not location**: a mount listed
under `verify` is copied in during scoring, so while the agent works it is absent.


