# Task folder specification

Normative for the default loader (`ManifestTaskset`). Tasks that need custom loading
may bypass the layout, but the manifest field semantics still apply.

## Repository layout

```
<task-repo>/
├── domain.yaml              # one per domain namespace
├── kits/<name>/kit.yaml     # optional sandbox-side shared code
├── images/<name>/Dockerfile # optional; MUST derive from an official base image
└── tasks/[<group>/]<task>/  # task folders; id is derived from this path
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
├── verify/            # required — run.sh, copied in at scoring time
├── oracle/            # recommended — run.sh, the task's own solution
└── setup/             # optional — run.sh, runs in the sandbox before the agent
```

A stage's folder reaches the sandbox only when that stage runs, which is what keeps
`verify/` and `oracle/` away from the agent.

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

artifacts: [/ale/output]           # absolute paths holding this task's output

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

## Images

An **unqualified** name is ours, and resolves against the project registry:
`sandbox-base-cli` becomes `ghcr.io/agentslastexam/sandbox-base-cli`.

A **public** image must be written fully qualified — `docker.io/library/python:3.12-slim`,
not `python:3.12-slim`. Without the rule, a short public name is silently rewritten into
our namespace and the run fails against a registry that never had it.

Tags resolve to a digest at run time and the digest is recorded in provenance, so a tag
that moved upstream is detectable rather than silently comparable.

## Who runs what

| | Identity |
|---|---|
| `setup/run.sh`, `verify/run.sh` | **root** — framework machinery run on the task's behalf |
| the agent | an **unprivileged user** the image declares |
| `oracle/run.sh` | the **same unprivileged user**, because it stands in for the agent |

The oracle's identity is not a detail: `ale validate` is the only check a task gets before
publication, so an oracle with more privilege would pass exactly the tasks a real agent
then fails on access alone.

**A task decides what it opens up to the agent.** The framework creates what the task
declared and hands it over; what a task's own setup then produces is the task's to make
writable or not. There is no correcting pass, because only the task knows what the agent
is meant to change — and forgetting is caught by the task's own validation.

A task needing elevated privileges declares them with its other provisioning requests:

```yaml
resources: { cpus: 2, memory_mb: 4096, sudo: true }
```

The sandbox is configured accordingly, a backend that cannot grant it refuses rather than
running with less, and the grant is recorded in provenance — an episode run that way was
less isolated than one without.

## Paths

**A task decides where its own data goes.** There is no framework-wide layout: a mount's
`dest` and an artifact's `path` are absolute paths chosen by the task, so a simulation
domain can put scenes at `/opt/sim/scenes` and nothing has to be adapted.

Whether those artifacts are copied back is decided per run (`artifacts.collect` in the
run configuration), not per task: declaring where the output lives and deciding to keep
a copy of it are different questions, asked by different people.

The framework creates the mount destinations, the artifact paths, and the run's scratch
directory (`work_dir`, configured per run rather than per task, and inside the agent
user's home so files land with the right owner). Anything else a task needs, it creates in
its own setup script.

The framework keeps the stage directories and the rewards file for its own machinery.
Shared libraries go where the image's interpreter already searches, so there is no
framework directory for a task author to learn.

What keeps gold answers away from an agent is **timing, not location**: a mount listed
under `verify` is copied in during scoring, so while the agent works it is absent.


