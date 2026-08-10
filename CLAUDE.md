# Working rules for coding agents

Read this before touching the repository. It is short on purpose.

## Environment

- **Always** run commands through `uv run <cmd>`. Never activate a virtualenv, never
  `pip install`, never create an environment by hand.
- **Never** share a `.venv` between worktrees or symlink one into place. Each tree owns
  its own; it is created by `just bootstrap` in seconds from the shared uv cache.
- Worktrees belong in `../ale-worktrees/<name>` and are created with `just wt <name>`.
- If anything about the environment looks wrong, run `just doctor` **before**
  investigating by hand. It prints the fix for every failure.
- Keep `uv.lock` committed and in sync (`uv lock --check`).

## Code

- Everything in this repository is standard English: code, comments, docstrings, docs,
  commit messages.
- Use the names in `docs/specs/lexicon.md`. Each term has exactly one meaning; a
  sandbox is never called an "environment", and `Environment` only ever means the
  administration layer that turns one task into one episode.
- Lay a model class out in this order: **fields → properties and methods → validators**,
  with the validators last under a `# --- validation ---` marker. Pydantic does not care
  about order, so this is free readability: the top of a class says what the thing *is*,
  and correctness checks stay in the model without interrupting that.
- Respect the import boundaries in `.importlinter` — they are the mechanical form of
  the architecture:
  - `ale.core` never imports `ale.run`
  - the gateway never imports a provider (its only interface is a URL plus a token)
  - `ale.run.guestd` imports the standard library only
  - harnesses reach sandboxes through the contract, not through a provider
- Before implementing a component, read its living contract in `docs/specs/` and the
  relevant feature specification in `../specs/`. Borrow what is proven; say in the commit
  message what you deliberately changed.
- Commit straight to `main`; no pull request ceremony is required here. Run
  `just lint && just test` first — that is the whole checklist.

## Contracts and decisions

- Normative specifications live in `docs/specs/`; decisions in `docs/adr/` (one page,
  append-only). Discussion notes are not normative.
- Changing a contract means updating its spec in the same change, and adding an ADR if
  the decision is new.
- Every result must carry a complete `RunLock`. Never report a run whose provenance is
  incomplete.
- During the evaluated solver phase, sandboxes are network-denied by default with the
  Gateway as the only egress and real provider credentials never enter the solver
  environment. Trusted verification may receive only its run-configured Judge credential
  for the verifier process lifetime. Task materials marked invisible never appear during
  the agent phase.

## Tests

```bash
just test        # unit
just test-int    # conformance + integration (needs a container runtime)
just lint        # ruff + import boundaries
```

Mark tests that need infrastructure with `needs_docker`, `needs_kvm` or
`needs_hf_gated` so they can be selected and skipped deliberately.
