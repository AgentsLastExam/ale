# 0002 - A standard Task is one self-contained folder

**Status:** Accepted

## Context

Repository-level domain manifests, shared Kits, domain images, implicit file staging,
and path-derived identity made a Task depend on content outside its folder. Fixed
framework workspace names also failed across domains, while broad variants allowed one
folder to describe materially different environments.

## Decision

Engine code and Task content live in separate repositories. A Task repository is only a
collection; each standard Task folder contains its explicit `name`, instruction, image
source, setup, verification, oracle, and optional agent resources. There is no
`domain.yaml`, Domain Kit, sibling-Task dependency, shared domain image, or implicit
`files/` upload.

The authored manifest remains strict `core/v1`. Its top level is always the `base`
instance. Additional variants may override only parameters, resources, and timeouts.
`${name}` parameters are rendered strictly: undeclared placeholders and unused values
fail. Image, network, artifacts, tools, verification, or expected-outcome changes require
another Task folder.

Tasks use literal absolute sandbox paths. ALE neither derives paths from Task identity nor
adds a workspace prefix. Agent-visible fixed state belongs in `image/`; `setup/` is only
irreducibly per-episode initialization. Task-owned Skills and MCP servers live under
`tools/` and must be declared; ambient host resources are not discovered.

Large files live in ignored `image/assets`, `setup/assets`, `verify/assets`, or
`oracle/assets` directories. Explicit `ale assets` commands synchronize only those roots
with a same-named dataset. Runtime never downloads them. Task source identity covers the
folder except exact asset roots and generated state; asset provenance records the remote
commit and whether the selected local view is dirty without hashing all bytes.

## Consequences

A synchronized Task folder can be reviewed, moved, and exported independently. Repeated
Dockerfile instructions are accepted in exchange for independence and ordinary layer
caching. Domain-specific verifier helpers stay in the Task until repeated use proves a
small framework-level primitive.
