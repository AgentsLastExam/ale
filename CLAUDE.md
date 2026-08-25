# Working rules for coding agents

Read this before touching the repository. Then read the nearest nested `AGENTS.md` for
the area being changed; nested rules add local ownership and checks without repeating
this file. `AGENTS.md` is a symlink to this file so every coding agent sees one source.

## Environment

- **Always** run commands through `uv run <cmd>`. Never activate a virtualenv, never
  `pip install`, never create an environment by hand.
- **Never** share a `.venv` between worktrees or symlink one into place. Each tree owns
  its own; it is created by `just bootstrap` in seconds from the shared uv cache.
- Worktrees belong in `../ale-worktrees/<name>` and are created with `just wt <name>`.
- If anything about the environment looks wrong, run `just doctor` before investigating.
- Keep `uv.lock` committed and in sync (`uv lock --check`).

## Code

- Everything in this repository is standard English: code, comments, docstrings, docs
  and commit messages.
- Use the names in `docs/specs/lexicon.md`. A Sandbox is never an Environment.
- Lay a model class out as fields, properties/methods, then validators under a
  `# --- validation ---` marker.
- Respect `.importlinter`: `ale.core` and `ale_verify` are independent; the Gateway never
  imports a Provider; `guestd` is standard-library only; Harnesses use the Sandbox
  contract instead of concrete Providers.
- Put a responsibility in the narrowest existing owner. Do not add a package, registry,
  interface, flag or compatibility layer until more than one concrete use needs it.
- This project is in active development. Do not preserve backward compatibility: change
  interfaces directly and update every caller, test and document in the same change.
- Keep CLI modules as composition, `ale.core` as contracts, and Provider/Harness code as
  edge adapters. Cross boundaries through public types, not `Any`, ambient state or
  private imports.
- Start at `docs/README.md` and read the owning living specification before changing a
  contract. Top-level feature specs are temporary plans, not lasting documentation.
- Use a dedicated worktree for non-trivial changes. Review and run the checks below
  before merging the tested branch into `main`.

## Contracts and decisions

- Normative specifications live in `docs/specs/`; current architectural rationale lives
  in `docs/adr/`. Guides do not override either.
- Changing a contract updates its spec in the same change and adds an ADR only for a new
  cross-module decision.
- Every reportable result carries a complete `RunLock`.
- During the evaluated solver phase, API-key and shipped subscription credentials remain
  on the Host Gateway. Trusted verification may receive only its configured Judge
  credential for that verifier process. `verify/` and `oracle/` never appear during the
  evaluated agent phase.

## Local guides

- `packages/ale-core/AGENTS.md` — public contracts and persisted schemas
- `packages/ale-run/AGENTS.md` — runtime ownership and the episode path
- `packages/ale-run/src/ale/run/harnesses/AGENTS.md` — native agent adapters
- `packages/ale-run/src/ale/run/gateway/AGENTS.md` — model transport and credentials
- `packages/ale-run/src/ale/run/providers/AGENTS.md` — Docker and QEMU backends
- `packages/ale-verify/AGENTS.md` — sandbox-local public verification API
- `tests/AGENTS.md` — test taxonomy and infrastructure markers
- `docs/AGENTS.md` — specifications, ADRs and guides
- `images/AGENTS.md` — Sandbox bases, image builders and Provider runtimes

## Checks

```bash
just test
uv run pytest packages/ale-verify/tests
just test-int
just lint
```

Mark infrastructure tests with `needs_docker`, `needs_kvm`, `needs_gui`, `needs_llm` or
the appropriate Hugging Face marker so they can be selected deliberately.
