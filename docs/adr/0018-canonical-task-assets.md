# ADR 0018: Canonical Task assets

**Status:** Superseded by [0019](0019-stage-local-task-assets-and-verification-topology.md)
**Date:** 2026-08-05

This file records the former central-cache/named-context design. It is not current
authoring guidance.

## Decision

Large Task data is developed and consumed from:

```text
$ALE_REPO_PATH/.cache/assets/<task-repo>/<task-relative-path>/{image,setup,verify}
```

Task manifests contain no asset declarations. `ale assets pull|push|status PATH...`
explicitly synchronizes selected Task subtrees with the same-named Hugging Face dataset
in an operator-configured collection.

Docker receives one generated image-only context named `ale_assets`. Setup and verify
stages use the existing `Sandbox.upload_dir()` contract. Verifier code locates optional
verify data through `Verification.asset_path()`.

## Consequences

- no-assets Tasks remain simple and portable;
- Task repositories do not depend on HF at run time after pull;
- Dockerfiles show the full owner-free source path they consume;
- verify references remain absent during solver execution;
- provenance records actual bytes separately from their last synchronized commit;
- cloud stores, VM assets, implicit pull, and provider-specific staging APIs remain
  outside this decision.
