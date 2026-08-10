# 0017 — Standard Tasks are self-contained local Docker builds

**Status**: Superseded in its Docker-only image clauses by
[0020](0020-container-and-vm-task-images.md) (2026-08-10). Its self-contained folder and
stable-state ownership decisions remain active; asset and topology details are refined by
[0019](0019-stage-local-task-assets-and-verification-topology.md).

**Supersedes**: the domain manifest, repository Kit, shared Image Tree, path-derived Task
identity, published final Task image, standard VM Task routing, implicit `files/` staging,
wide variant override, and manifest-only Task identity decisions in
[0003](0003-repo-topology.md), [0005](0005-workspace-and-templating.md),
[0006](0006-opaque-ids-and-symmetric-stages.md),
[0007](0007-store-and-workspace.md), [0008](0008-task-owned-paths.md),
[0015](0015-sandbox-local-verification.md), and
[0016](0016-task-image-routing-and-provider-observation.md), as qualified in each status.

## Context

The repository-level Task format accumulated several indirect dependencies: `domain.yaml`
supplied identity, `kits/` supplied shared Python, domain images supplied software, a
registry supplied final images, and `files/` acquired an implicit runtime destination.
Moving or exporting one Task therefore required understanding and copying content outside
its folder.

The same indirection made the stable initial state unclear. Fixed installation and input
placement could happen in an image, an asset staging phase, or setup. Variants could replace
most of the contract and thereby make one folder represent materially different Tasks.

## Decision

**One standard Task is one self-contained source folder.** It carries an explicit stable
`name`, prompt, `image/Dockerfile`, setup, verification, oracle, and optional Skills and MCP
resources. Its source digest covers the complete folder snapshot, with no implicit ignored
files. Framework-generated state is created outside the source folder. No `domain.yaml`,
repository Kit, Task `files/`, shared domain image, or sibling Task is part of its runtime
contract. The manifest remains the project's initial `core/v1`; this development-stage
redesign does not create another schema version.

**The current standard Task image is Docker-only and built locally.** `image/` is the sole
build context. The final Dockerfile stage starts directly from ALE's foundational CLI or
GUI base. ALE supplies no domain Image Tree. It builds rather than pulls the final Task
image and records the exact content started. Publishing final Task images is deferred as a
delivery optimization. General third-party builder stages are allowed, but no stage may
depend on another ALE Task's final image or an ALE domain, robotics, or application
intermediate image.

**Solver-visible fixed data belongs in the image.** Small data is authored under `image/`.
The later stage-local asset and ordinary Docker-context decision is recorded in ADR 0019.
Verify-only data never enters an image layer.

**Setup is dynamic initialization only.** It remains trusted root code with open egress,
but it resets or seeds episode state, generates runtime values, starts state-dependent
services, and waits for readiness. Fixed installation, compilation, download, and
configuration belong in the Dockerfile.

**Variants remain one Task.** The top level is permanently `base`; additional variants may
change only parameters, resources, and timeouts. Image, setup, verification, network,
artifacts, Skills, or MCP changes require another folder and name.

**Task is the public authored unit.** A collection may select several Tasks, but it is not
itself a Task or part of one Task's protocol. Folder and repository paths do not determine
identity.

QEMU and other VM Providers may remain engine capabilities. A standard VM Task image
contract is explicitly outside this decision and will be designed separately if needed.

## Consequences

- A Task folder can be reviewed, moved, copied, or exported without dependency discovery.
- Stable setup cost moves to Docker build and is reused through ordinary layer caching.
- Repeated Dockerfile lines are accepted in exchange for independent Tasks; shared
  abstractions require evidence beyond coincidental package overlap.
- Large data distribution stays separate from Git while the Dockerfile retains ownership
  of final filesystem layout.
- `verify/` remains withheld by timing, and answer bytes cannot leak through image history.
- A changed Dockerfile, verifier, oracle, Skill, or MCP server changes Task source identity.
- Image build identity is narrower than Task source identity: it covers `image/`, resolved
  image assets, build configuration, and effective base image, so verifier-only changes do
  not force a rebuild.
- Feature 006 implements this contract as refined by ADR 0019.
