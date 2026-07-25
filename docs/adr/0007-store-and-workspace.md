# 0007 — Separate the data store from the workspace

**Status**: accepted (2026-07-25)

## Context

Fixing the sandbox workspace at `/ale/input`, `/ale/output`, ... is what frees tasks
from encoding their own names into paths (ADR 0006). But taken alone it forecloses an
optimisation the previous framework relied on and we will want back.

That optimisation is pre-baking: put task data into the image so no episode pays a
download, and — further out — run many tasks' setup once in a shared container, commit
the result, and reuse that image for all of them. With a single fixed location, one
image can hold exactly one task's data. Every additional task overwrites the last.

So the fixed workspace and pre-baking are in direct conflict, and the conflict is
structural rather than a matter of tuning.

## Decision

Two layers, with different jobs:

| Layer | Path | Visible to | Holds |
|---|---|---|---|
| Store | `/ale/store/<data_key>/…` | the engine only | many tasks' data at once; may be baked into an image or cached on the host |
| Workspace | `/ale/{input,software,output,work}`, plus `/ale/reference` at verification | agents and task scripts | one episode's materialised view |

`data_key` is derived from `repo + revision + component` — **content, not identity**. A
task renamed or moved keeps the same key, so a baked image stays valid; two tasks
sharing a bundle share one copy.

At episode start the engine materialises the components a stage declared, from the store
into the fixed workspace. Lookup order is image-baked → host cache → download, decided
by a `manifest.json` the image carries. Every component's origin (`baked`, `cache`,
`download`) and key go into the run's provenance, so a fast run is exactly as
explainable as a slow one.

Setup is also split conceptually: deterministic work (materialising assets, installing
dependencies) may be pre-baked; per-episode work (minting a secret, seeding state) must
not. A task declares `setup.prebakeable`, defaulting to false. **Nothing consumes it
yet** — the engine always runs setup — but recording it now is what lets an image
builder later select a bakeable set without revisiting every task.

## Consequences

- Instructions and scripts still see one fixed layout, so ADR 0006 holds unchanged.
- Pre-baking becomes an image-build concern rather than a schema change: the contract
  that makes it possible is in place before the optimisation is needed.
- Content-derived keys make the baked image survive renames, which the previous
  path-keyed design did not.
- Cost today: one indirection during materialisation, and one boolean nobody reads yet.
  Both are cheap; adding them later would have meant re-cutting the data contract.
