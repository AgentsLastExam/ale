# 0004 — Two normative document kinds, nothing else

**Status**: accepted (2026-07-24)

## Context

Design discussion produces a lot of prose: alternatives, half-formed ideas, revisions.
Implementers and reviewers need the opposite — a short, current, unambiguous statement
of what is true now. Mixing the two makes both useless: discussion buries the decision,
and readers cannot tell which paragraph is binding.

## Decision

The engine repository carries exactly two kinds of normative document:

- `docs/adr/NNNN-<slug>.md` — **decision records**. One page each: Context, Decision,
  Consequences. Append-only: a superseded decision gets a new ADR that says so, the old
  one is never edited into agreement.
- `docs/specs/<topic>.md` — **living specifications** (lexicon, task folder, trace,
  security model). Always describe the current state; edited in the same change as the
  code they describe.

Exploratory discussion stays outside the repository. Where notes and these documents
disagree, these documents win. Both kinds are deliberately compact: if a spec cannot be
skimmed, it will not be read, and an unread spec governs nothing.

## Consequences

- A reviewer can answer "is this allowed?" from one short file.
- The reason behind a rule survives the rule's authors, without polluting the rule.
- Writing a decision down is cheap enough that it actually happens.
