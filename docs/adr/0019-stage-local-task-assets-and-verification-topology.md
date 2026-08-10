# ADR 0019: Stage-local Task assets and verification topology

**Status:** Accepted; verifier image shape and kind routing superseded by
[0020](0020-container-and-vm-task-images.md)
**Date:** 2026-08-09
**Supersedes:** [0018](0018-canonical-task-assets.md), and the asset/topology details of
[0015](0015-sandbox-local-verification.md) and
[0017](0017-self-contained-docker-tasks.md)

## Context

A central `$ALE_REPO_PATH/.cache/assets` tree and generated `ale_assets` BuildKit context
made a Task's development paths differ from its runtime paths. Dockerfiles encoded full
repository paths, Task renames required asset-view changes, and setup/verify needed a
second publication protocol. Shared-only verification also prevented a verifier from
running in a clean image with only declared solver evidence.

## Decision

Large assets live directly below optional ignored `image/assets`, `setup/assets`,
`verify/assets`, and `oracle/assets` roots. `ale assets pull|push|status PATH...`
synchronizes only those paths
with a same-named Hugging Face dataset. Pull restores the exact Task-repository structure;
runtime never downloads. One repository-local `.ale-cache` record stores commit and dirty
generation state. Public provenance records one optional repository/Task/commit/dirty
observation and does not hash asset bytes.

`image/` is the sole ordinary Docker context. Dockerfiles use normal relative `COPY`.
Setup, verify, and oracle each upload their entire stage once and run from that directory.
Verify and oracle assets therefore remain absent from evaluated solver execution without a
special publication API.

One Task-owned `verify/run.sh` and stateful `Verification` program support two placements.
Shared is the default. Separate verification declares explicit resources and optionally
builds `verify/Dockerfile` from the `verify/` context or uses a resolved external image;
omission reuses the prepared
solver image. After Harness cleanup ALE captures immutable declared file/directory
artifacts, releases a default-destroy solver, restores evidence to exact absolute paths in
the verifier sandbox, and runs the same verifier without rerunning setup.

Artifact collection is literal operator policy. `host` captures and can restore declared
outputs; `none` does not inspect or copy them, creates no temporary spool, and therefore
cannot transfer solver outputs into a separate verifier.

Solver and verifier retention are independent operator run policies. Both default to
destroy. Retained Docker sandboxes are sanitized, labeled, persisted in Result/RunLock,
discoverable by ALE, and returned with cleanup commands. Retention does not change Task
identity, rewards, verification records, or artifacts.

Agent Judge adapter and exact CLI version belong to run configuration. Judge prompt,
rubric, evidence, and optional invocation-local MCP belong to Task verification code.
Direct LLM/Agent calls run inside the active verification sandbox and create diagnostic
transcripts, not a second trajectory.

## Consequences

- local development, Docker build, setup, and verification use the same understandable
  paths;
- each Task remains independently movable once its ignored assets are synchronized;
- asset status is deliberately a cheap commit/dirty observation, not an adversarial
  content proof;
- a separate verifier sees declared evidence rather than an implicit solver workspace;
- explicit verifier resources and image resolution fail before spending a solver call;
- QEMU verifier images, object-store runtime pulls, final solver-image publication,
  endpoint health checks, and Agent Judge workspace isolation remain outside this ADR.
