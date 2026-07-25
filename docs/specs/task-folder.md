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
  assets: [hello_inputs]           # names; visibility comes from assets.lock.yaml
  kits:   [data-prep]              # names; versions come from kits.lock.yaml
  prebakeable: false               # true only if this setup is deterministic
verify:
  assets: [hello_answers]
  kits:   [grader-protocol]
params: { n: 3 }
variants:
  - { name: base }
  - { name: hard, params: { n: 10 } }
validate: { min_reward: 1.0 }      # or { mode: manual, reason: "..." }
metadata: { tags: [smoke] }
extras: {}                         # namespaced experiments only
```

The identifier is derived from the folder path (`tasks/demo/hello` → `demo-hello`) and
never written in the file. It is opaque: the domain and the variant are separate fields,
and nothing in the framework parses an identifier (ADR 0006).

Scripts are not declared. A stage's folder is copied into the sandbox when that stage
runs, and its entry point (`setup/run.sh`, `verify/run.sh`) executes if present.

## Workspace

Every task sees the same layout, so no prompt or script depends on a task's name:

| Path | Available | Contents |
|---|---|---|
| `/ale/input` | setup, agent | `files/`, assets staged for setup |
| `/ale/software` | setup, agent | tool and runtime assets |
| `/ale/output` | agent | deliverables; collected as artifacts, read by verify |
| `/ale/work` | agent | scratch, never collected |
| `/ale/reference` | verify only | gold answers and other verification-only assets |
| `/ale/kits/<name>` | the stage that asked | shared domain libraries, on `PYTHONPATH` |

Windows guests map the same names under `C:\ale\`. Instructions state these paths
literally; path templating is not supported (ADR 0005).

Data reaches the workspace from the **store** (`/ale/store/<data_key>/…`), which is an
engine-only concern and may hold many tasks' data or be baked into an image (ADR 0007).
Instructions must never name a store path.

## Assets and kits

```yaml
# assets.lock.yaml — visibility travels with the data, not with the task
components:
  hello_inputs:  { path: artifacts/inputs.tar.zst,  visibility: setup }
  hello_answers: { path: artifacts/answers.tar.zst, visibility: verify }
```

A component marked `verify` can never be staged before the agent, whatever a task
requests; asking for one in `setup` is a lint error.

```
kits/grader-protocol/
├── kit.toml           # name, package, python_min, runtime (stdlib | image_deps)
└── grader_protocol/   # the package itself
```

Kits are copied to `/ale/kits/<name>` and added to `PYTHONPATH` — never pip-installed,
because guest interpreters are not ours to manage. `ale kit lock` records each kit's
content hash in `kits.lock.yaml`; task manifests reference kits by name only.

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
