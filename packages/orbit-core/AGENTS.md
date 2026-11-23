# ale-core agent guide

This package owns ALE's public contracts. Read [`README.md`](README.md) and the owning
living specification under `../../docs/specs/` before changing a model or interface.

- Never import `ale.run` or `ale_verify`.
- Keep boundary and persisted models strict, serializable and explicitly versioned.
- A schema change updates its living spec in the same change. During early development,
  persisted records carry no migrations or legacy readers: a record is read by the engine
  commit that wrote it (see `docs/specs/trace.md`).
- Add an abstract method only when at least two implementations need the same contract.
- Model order is fields, behavior, then validators under `# --- validation ---`.
- Do not place runtime discovery, filesystem orchestration or provider behavior here.

Run `just lint && just test` from the repository root.
