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
# domain.yaml
name: demo
requires_core: ">=0.1,<0.2"
requires_extensions: []          # extensions are referenced by name, never imported
default_image: sandbox-base-cli  # optional fallback
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
harness_family: autonomous         # autonomous | policy
image: sandbox-base-cli
resources: { cpus: 1, memory_mb: 1024 }
network: { mode: block }           # block | allowlist (+ allowed_hosts) | open
timeouts: { setup: 120, agent: 900, verify: 300 }
setup:
  - script: setup/prepare.sh
  - assets: { component: hello_data, stage: setup }   # stage: setup | verify
  - kit: { name: grader-protocol, version: "0.5" }
params: { n: 3 }
variants:
  - { name: base }
  - { name: hard, params: { n: 10 } }
validate: { min_reward: 1.0 }      # or { mode: manual, reason: "..." }
metadata: { tags: [smoke] }
extras: {}                         # namespaced experiments only
```

The identifier is derived from the path and never written in the file.

## Workspace

| Path | Available | Contents |
|---|---|---|
| `/ale/input` | setup, agent | `files/`, assets staged for setup |
| `/ale/software` | setup, agent | tool and runtime assets |
| `/ale/output` | agent | deliverables; collected as artifacts, read by verify |
| `/ale/work` | agent | scratch, never collected |
| `/ale/reference` | verify only | gold answers and other verification-only assets |

Windows guests map the same names under `C:\ale\`. Instructions state these paths
literally; path templating is not supported (see ADR 0005).

## Instruction rendering

`${param}` substitution only (`string.Template`), from `params` merged with the
variant's `params`. Strict: an unresolved placeholder or an unused declared parameter
fails to load. The stored instruction is the rendered text, and it is what the task
hash covers, so each variant has its own identity.

Values that only exist at run time are not templated: setup writes a file and the
instruction tells the agent to read it.

## Verify and oracle

Both receive `ALE_TASK_DIR`, `ALE_PARAMS_JSON` and `ALE_VERDICT_PATH`. Verify must
write `{"rewards": {"reward": <0..1>, ...}}` to `$ALE_VERDICT_PATH`; a non-zero exit,
a missing file or malformed content yields status `task_error`, which is distinct from
a zero reward. `ale validate` runs `oracle/solve.sh` in place of the agent and requires
`validate.min_reward`; a task without an oracle must declare `validate.mode: manual`
with a reason.

## Minimum task

`task.yaml`, `instruction.md`, `verify/` — three files. Keeping that floor low is a
project requirement, not an accident.
