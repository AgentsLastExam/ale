# Documentation agent guide

[`README.md`](README.md) is the maintained documentation index.

- `specs/` says what is true now and is normative.
- `guides/` teaches an operator or contributor how to perform a workflow.
- Top-level `../specs/` is temporary feature planning and is removed after its durable
  behavior is incorporated here.
- Give the reason for a cross-module choice in a sentence or two where the spec states
  it; superseded decisions live in Git history.
- Keep one fact in one owner and link to it; do not repeat command sequences across the
  root README, package READMEs and guides.
- Use canonical terms from `specs/lexicon.md` and update code/spec/tests together when a
  contract changes.

Documentation commands must be executable against the current CLI and use placeholder
credentials only.
