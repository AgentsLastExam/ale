# Documentation agent guide

[`README.md`](README.md) is the maintained documentation index.

- `specs/` says what is true now and is normative.
- `adr/` explains one current cross-module decision and its tradeoffs.
- `guides/` teaches an operator or contributor how to perform a workflow.
- Top-level `../specs/` is temporary feature planning and is removed after its durable
  behavior is incorporated here.
- Superseded decisions live in Git history, not the current ADR reading path.
- Keep one fact in one owner and link to it; do not repeat command sequences across the
  root README, package READMEs and guides.
- Use canonical terms from `specs/lexicon.md` and update code/spec/tests together when a
  contract changes.

Documentation commands must be executable against the current CLI and use placeholder
credentials only.
