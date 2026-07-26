# Rebuilding a legacy task

The previous framework's tasks are **source material**, not something to adapt in place.
Their data, images and scoring logic are worth keeping; their structure is what this
engine exists to replace. A rebuilt task is a new task that happens to test the same
thing — which is why every rebuilt task must pass `ale validate` on its own merits.

## What a legacy task looked like

```
tasks/<domain>/<task>/
├── main.py          # host-side Python: setup, scoring, path interpolation, all mixed
└── task_card.json   # prompt with {self.input_dir}-style placeholders
```

Data lived in a bucket at `<domain>/<task>/<variant>/{input,software,reference}`, and
prompts interpolated absolute paths because one Python class had to serve a Windows root
(`E:\agenthle`) and a Linux root (`/media/user/data/agenthle`).

## The four moves

### 1. Data to a pinned dataset repository

Upload the legacy `input`/`software`/`reference` directories to
`agents-last-exam/ale-tasks-assets` and reference each by **commit**:

```yaml
setup:
  assets:
    - repo: agents-last-exam/ale-tasks-assets
      revision: 1d0d026c7a21226f619b74f3912e23573c7105ab
      path: <domain>/<task>/<variant>/input
      dest: /ale/input
verify:
  assets:
    - { repo: …, revision: …, path: <domain>/<task>/<variant>/reference, dest: /ale/reference }
```

A bucket path can change underneath a recorded result; a commit cannot. That is the whole
reason for the move, and `ale lint` enforces it.

The old `reference` directory is the answer material, so it goes in the **verify** stage.
That single placement is what keeps it away from the agent — see
[task-authoring.md](task-authoring.md).

### 2. `main.py` splits in two

Legacy `main.py` mixed three jobs. Separate them:

| Was | Becomes |
|---|---|
| environment preparation | `setup/run.sh`, running **in the sandbox** |
| scoring | `verify/run.sh`, running **in the sandbox** after the agent |
| path interpolation | deleted — write literal paths |

Both scripts run in the sandbox now, not on the host. A task repository contains no
host-side code at all (Constitution II), which is what lets a task repository be
untrusted content rather than code the engine executes.

Shared scoring helpers become a **kit**: a Python package under `kits/<name>/`, copied to
`/ale/kits/<name>` and put on `PYTHONPATH`. Kits are never pip-installed — the guest
interpreter belongs to the image, not to us.

### 3. Placeholders get triaged

Roughly 95% of legacy placeholders were path derivations. They all disappear.

| Kind | Legacy examples | Now |
|---|---|---|
| Path derivation | `input_dir`, `remote_output_dir`, `task_dir`, `software_dir`, `python_wrapper` | **Delete.** Write the literal path your task chose. |
| Variant parameter | `VARIANT_NAME`, seeds, difficulty knobs | **Keep** as `${name}`, from `params` + the variant's. |
| Runtime-discovered | a generated secret, a URL for a service setup started | **Never template.** Setup writes a file; the instruction says to read it. |

`ale lint` rejects the legacy patterns outright, so a missed one is caught in a second
rather than by a confused agent.

### 4. Images get rebuilt, not reused

Legacy Docker images carried no task data; the QEMU images had it baked in. Neither
matches the current standard, and neither carries the guest service.

- **CLI tasks** → `sandbox-base-cli`.
- **Desktop tasks** → `sandbox-base-gui`, which is built `FROM
  agentslastexam/ale-ubuntu22-docker` — the exported rootfs those tasks were authored
  against — plus guestd. Rebuilding an equivalent desktop from a different base would
  reintroduce exactly the small differences that make a task pass in one place and fail
  in another.

Data is no longer baked. It is fetched per mount and cached by content, so two tasks
sharing a bundle download it once. Pre-baking stays possible later precisely because the
store key is derived from where data came from rather than from any task's name.

## Checklist for one task

```bash
ale new-task tasks/<group>/<task>       # start from something that already passes
# port the prompt, dropping every path placeholder
# port setup and scoring into setup/run.sh and verify/run.sh
# upload data, pin the commit, declare the mounts
# write oracle/run.sh — the legacy task usually had a known solution

ale lint tasks/<group>/<task>
ale validate tasks/<group>/<task>       # must reach min_reward
```

If the oracle cannot reach the threshold, the rebuild is not finished. That is the point
of the gate: a task nobody can solve is a broken task, and finding out costs one container
rather than one agent run.

## What does not carry over

- **Task ids.** They are derived from the folder path now (`tasks/demo/hello` →
  `demo-hello`) and are opaque. Old ids that encoded structure have no successor.
- **The fixed workspace.** There is no `/ale/input` unless your task declares it. Many
  rebuilt tasks keep those paths out of habit, which is fine — just declare them.
- **Host-side task code.** If a legacy task did something on the host that neither stage
  can do in the sandbox, that is a finding worth raising rather than working around.
