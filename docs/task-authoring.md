# Writing a task

A task is a folder. Four files make a complete one, and the scaffold gives you all four
already working, so your first move is to change something rather than to fill in blanks.

## Start

```bash
git clone https://github.com/AgentsLastExam/ale-tasks-base ale-tasks-<domain>
cd ale-tasks-<domain> && git remote rename origin upstream
# edit domain.yaml: set `name`

ale new-task tasks/my_first
ale lint tasks/my_first        # passes now
ale validate tasks/my_first    # scores 1.0 now
```

Clone rather than fork: GitHub allows one fork per organisation, and several domains
usually live in the same one. The result behaves identically.

The scaffold's oracle writes exactly what its verifier expects, so you begin from a green
run. Change one thing, re-run `ale validate`, and you always know which change broke it.

## The four files

```
tasks/my_first/
├── task.yaml        # what to provision, what data, what to keep
├── instruction.md   # the prompt — the only file the agent sees
├── verify/run.sh    # scores the result; copied in after the agent is gone
└── oracle/run.sh    # your own solution; run in place of the agent by `ale validate`
```

`setup/run.sh` is optional and runs in the sandbox before the agent.

Nothing declares these scripts. A stage's folder is copied in when that stage runs and
its `run.sh` executes if present, so the layout on disk *is* the execution order.

## Identity comes from the path

`tasks/demo/hello` in domain `demo` becomes `demo-hello`. Never write an id in the file.

It is opaque: nothing in the framework parses it back into parts, so you can reorganise
folders without touching data, prompts or stored results.

## Where your data goes is your decision

There is no framework layout to conform to. A mount says where its data comes from and
where it lands; both are yours to choose:

```yaml
setup:
  assets:
    - repo: agents-last-exam/ale-tasks-assets
      revision: 1d0d026c7a21226f619b74f3912e23573c7105ab
      path: mydomain/my_first/input
      dest: /ale/input
```

**Pin a commit, not a branch.** A branch moves, and then two runs that named the same
source read different bytes — which quietly makes their scores incomparable. `ale lint`
rejects anything that is not a commit.

The framework creates the destinations you declared, the artifact paths you declared, and
a scratch directory. **Everything else your task needs, your setup script creates.** If
`setup/run.sh` writes to `/ale/input` and no mount put anything there, `mkdir -p` it
first — a directory that happens to exist in one image is not a contract.

## Keeping answers away from the agent

Put them in the verify stage:

```yaml
verify:
  assets:
    - { repo: …, revision: …, path: mydomain/my_first/reference, dest: /ale/reference }
```

The guarantee is **timing**, not a flag. A verify-stage mount is materialised during
scoring, so while the agent works it is not hidden — it is absent. Same for `verify/` and
`oracle/` themselves.

Anything the agent must not see goes here. There is no way to mark a file secret and
leave it in the setup stage, because that would be a promise the framework could not keep.

## Instructions are rendered once, strictly

```markdown
Solve ${n} cases and write the answers to /ale/output/result.txt
```

`${param}` comes from `params`, merged with the variant's. Strict both ways: an
undeclared placeholder and an unused parameter are both errors. Write paths **literally** —
your task chose them and the image is fixed, so there is nothing to compute.

Legacy patterns (`{self.input_dir}`, `E:\agenthle`, `${output_dir}`) are rejected.

For a value that only exists at run time — a generated secret, a URL for a service your
setup started — do not template it. Have setup write a file and tell the agent to read it.
That keeps the instruction static, hashable, and free of the answer.

## Variants

```yaml
params: { n: 3 }
variants:
  - { name: base }
  - { name: hard, params: { n: 10 } }
```

One task instance each, own rendered instruction, own identity. They share an `id` and
differ in `variant`, so results aggregate either way without anyone parsing a string.

## Scoring

`verify/run.sh` writes rewards to `$ALE_VERDICT_PATH`:

```bash
printf '{"rewards": {"reward": 1.0}}' > "$ALE_VERDICT_PATH"
```

Also available: `$ALE_TASK_DIR`, `$ALE_PARAMS_JSON`, `$ALE_WORK_DIR`.

A verifier that exits non-zero or writes nothing is a `task_error` — a defect in *your*
task — and is deliberately distinct from a zero score. Keep that distinction sharp: it is
what stops a broken verifier from looking like a hard task.

## Before you submit

```bash
ale lint tasks/           # every task in the repo
ale validate tasks/       # every oracle must reach its min_reward
```

CI runs both. A task with no oracle fails admission unless it declares
`validate: {mode: manual, reason: "..."}` — and the reason is read by a person.

## Two things people get wrong

**Assuming a directory exists.** The framework builds only what you declared. Create the
rest yourself.

**Naming a public image the short way.** An unqualified name is *ours* and resolves to the
project registry. Public images need their full path: `docker.io/library/python:3.12-slim`.
