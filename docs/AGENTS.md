# Documentation agent guide

[`README.md`](README.md) is the maintained documentation index.

- `specs/` says what is true now and is normative. It holds general principles and
  cross-module design: what other modules, Harness adapters or Task authors must obey or
  know, each with its reason.
- A fact about one implementation does not belong in a spec, however important it is to
  that implementation. It goes in the owning module's README, and the spec states at most
  the general rule and links there:
  - one Provider's platform behavior → `packages/ale-run/src/ale/run/providers/README.md`;
  - one Gateway dialect's routes, statuses and quirks → `packages/ale-run/src/ale/run/gateway/README.md`;
  - one Harness's or agent program's behavior → its adapter's `README.md`.
- Before adding a sentence to a spec, ask whether another module or a Task author needs
  to obey or know it. If not, it goes in the owning README, or in a guide when an
  operator needs it. Adding a Provider, Harness or dialect normally changes a spec by a
  list entry or a link, not a section.
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
