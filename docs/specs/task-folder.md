# Standard Task folder specification

Normative for the container or VM `setup -> agent -> verify` protocol. A Task repository
is only a collection; each Task folder is an independent authored unit.

## Folder contract

```text
<task>/
├── task.yaml                         # required
├── instruction.md                    # required
├── image/
│   ├── Dockerfile                    # optional local solver build
│   └── assets/                       # optional, ignored
├── setup/                            # optional
│   ├── run.sh
│   └── assets/                       # optional, ignored
├── verify/
│   ├── run.sh                        # required
│   ├── verify.py                     # conventional entry program
│   ├── Dockerfile                    # optional separate verifier build
│   └── assets/                       # optional, ignored
├── oracle/
│   ├── run.sh                        # required
│   └── assets/                       # optional, ignored
└── tools/                            # optional
    ├── skills/<name>/SKILL.md
    └── mcp/<server>.toml
```

Repository `domain.yaml`, `kits/`, `images/`, and implicit Task `files/` are invalid.
Task-level `skills/` and `mcp/` are invalid; agent resources belong below `tools/`.

## core/v1 manifest

`task.yaml` is strict. Its complete field set is:

| Field | Type | Default / rule |
|---|---|---|
| `spec_type` | literal | required `core/v1` |
| `name` | Task ID | required, stable, collection-unique |
| `image` | mapping | required `kind`; optional `ref` |
| `environment` | string | `core/standard` |
| `resources` | mapping | solver resource request |
| `network` | mapping | solver/oracle network policy |
| `timeouts` | mapping | phase deadlines |
| `artifacts` | absolute path list | empty; unique and non-overlapping |
| `tools` | mapping | Task-owned Skills and MCP declarations |
| `params` | mapping | instruction template inputs |
| `verify` | mapping | shared/separate verification placement |
| `variants` | list | additional bounded instances |
| `metadata` | mapping | descriptive only |
| `extras` | namespaced mapping | no standard runtime semantics |

Resource fields are `cpus >= 1`, `memory_mb >= 128`, optional `storage_mb >= 256`,
`gpus >= 0`, and `sudo`. Defaults are 1 CPU, 1024 MB memory, no storage quota, no GPU,
and no sudo.

Network forms are:

```yaml
network: {mode: block}
network: {mode: open}
network: {mode: allowlist, allowed_hosts: [api.example.com]}
```

`allowed_hosts` is non-empty only in allowlist mode. Timeouts are positive `setup`,
`agent`, and `verify` seconds, defaulting to 120, 900, and 300.

Instruction placeholders use `${name}`. Every placeholder must have a `params` key and
every parameter must be used after the selected variant is applied.

## Base and variants

The top-level Task is permanently `base`. `variants` contains only additional names:

```yaml
params: {count: 3}
variants:
  - name: hard
    params: {count: 10}
    resources: {cpus: 2}
    timeouts: {agent: 1800}
```

A variant accepts exactly `name`, `params`, `resources`, and `timeouts`. Mapping values
overlay base key by key. The name `base`, duplicate names, and overrides of verification,
image, artifacts, tools, network, metadata, or extras fail.

Unqualified run/validate selects base only. `@name` selects one additional variant and
`@{base,name,...}` preserves the requested order.

## Source identity

Task identity comes from the explicit `name`, not its directory. ALE computes a canonical
source digest over every Task entry, relative path, type, regular-file bytes, executable
bit, and supported symlink target except the exact roots:

- `image/assets`;
- `setup/assets`;
- `verify/assets`;
- `oracle/assets`;
- generated `.ale-cache` state if it appears below a selected boundary.

No similarly named directory is pruned. Image source identity is independently computed
from `image/`, excluding only `image/assets`; Docker still consumes the current asset bytes
through its native build context.

When inside Git, runtime records repository root, basename, and Task-relative path. A
copied no-assets Task remains loadable without that source context.

## Image source and preparation

Every solver explicitly declares `image.kind: container|vm` and may declare `image.ref`.
`image/` is the only local build context. If `image/Dockerfile` exists, ALE always builds
it and does not fall back to `ref`; otherwise `ref` is required and acquired by the
Provider for the declared kind. The final stage begins from the matching ALE CLI/GUI or
`sandbox-base-vm-gui` base.

Both local kinds first produce an OCI image. For `vm`, ALE's versioned materializer turns
the exported rootfs into one derivation-named qcow2. Tasks must not contain guestd, mkosi,
bootloader, partition, or qcow conversion machinery. `ale prepare PATH` reports ordered
build/ref, materialization/reuse, and artifact-check stages without starting an episode.

## Stage-local asset synchronization

Only `**/image/assets/**`, `**/setup/assets/**`, `**/verify/assets/**`, and
`**/oracle/assets/**` participate.
Each Task repository maps by basename to a same-named dataset in the configured Hugging
Face collection; remote owners are transport configuration and never appear locally.

`ale assets pull|push|status PATH...` recursively discovers and deduplicates selected
Tasks. Pull has no revision selector and atomically replaces only selected roots; it
refuses dirty overwrite unless `--force` is supplied. Push mirrors selected additions,
changes, and deletions without uploading manifests, code, tools, or oracle material.

`.ale-cache/assets.json` stores schema version 1 and one Task marker containing the last
remote commit, generation timestamp, and dirty state. Public episode provenance is one
optional `{repository, task_path, commit, dirty}` observation. Normal operation does not
hash asset bytes. No-assets Tasks have no asset requirement. Runtime performs no network
synchronization.

## Lifecycle

```text
local builds/ref resolution -> provision -> setup -> agent/oracle
-> Harness cleanup -> trajectory/optional artifact capture -> verify -> teardown/final retention
```

Setup and verify run as trusted root with open egress. Solver and oracle run as the image's
declared unprivileged user under Task network policy. `setup/`, `verify/`, and (only for
validation) `oracle/` are uploaded as complete directories and execute with their own staged root as cwd.
Assets are therefore addressed relatively. Setup is optional; no automatic output path
creation or asset publication occurs.

`verify/` remains absent through solver execution and Harness cleanup. With
`artifacts.collect = "host"`, ALE then captures every declared artifact; roots must be
regular files or directories and snapshot metadata records source, type, mode, stored
path, size, and content identity. With `artifacts.collect = "none"`, ALE does not inspect,
copy, retain, or restore declared artifacts and creates no artifact spool. A separate
verifier that needs solver outputs therefore requires host artifact collection.

## Verification modes

Shared is the default:

```yaml
verify: {environment_mode: shared}
```

Shared forbids verifier image and resource declarations. Separate requires explicit
resources:

```yaml
verify:
  environment_mode: separate
  resources: {cpus: 1, memory_mb: 1024, storage_mb: null, gpus: 0}
```

For a separate verifier, `verify/Dockerfile` is built with `verify/` as context and
requires an explicit nested `verify.image.kind`. The same local-first rule applies to
optional `verify.image.ref`. Without either verifier source, ALE reuses the complete
prepared solver image and kind. Container and VM verifier refs are both supported by
their matching Providers. Verifier resources are independently admitted.

After solver evidence capture, a default-destroy solver is released before separate
verifier provisioning. ALE starts the verifier image, restores declared artifacts to the
same absolute paths with captured modes, uploads the same `verify/`, and does not rerun
setup. Both modes emit one canonical `verification.json` and the same reward envelope.
All verifier/Judge/configuration/infrastructure failures remain distinct from completed
zero rewards.

Verification code reads optional stage assets with ordinary relative paths such as
`assets/reference.json`; its cwd is the staged `verify/` root. `Verification` has no asset
resolver, and ALE does not manufacture an empty asset directory.

## Tools

Task Skill paths must resolve below `tools/skills/`. Task MCP descriptors must resolve
below `tools/mcp/` and use stdio or Streamable HTTP. A stdio descriptor may reference
adjacent code with `{mcp}`; ALE uploads the descriptor directory under the agent home and
resolves the placeholder before Harness configuration. Same-name conflicting resources
fail before provisioning. Ambient host resources are never discovered.

## Retention

Retention is not a manifest or variant field. Run TOML owns independent
`sandbox_retention.solver` and `.verifier` values, each exactly `destroy` or `keep` and
defaulting to destroy. Shared mode has one physical sandbox and keeps it if either logical
role requests keep. Separate mode applies policies independently.

Before exposure, ALE completes Harness cleanup/evidence capture, removes known temporary
credential/native homes, and revokes the Gateway session. A failed sanitation triggers
safe destruction and `retention-failed` without altering a completed evaluation. Result
v2 and RunLock schema 2 record roles, requested policy, outcome, Provider, handle, reason,
cleanup command, and GPU devices. Docker retained sandboxes are discoverable and removable
with `ale sandbox list` and `ale sandbox destroy HANDLE`.

## Validation

Validation uses the same builds, resources, setup, topology, verifier, and default
retention behavior as a run. Untouched must complete with a non-empty all-zero reward map.
Oracle must complete with exactly the same reward names; its actual values are recorded,
and non-one values are warnings. Topology, setup, artifact, verifier, Judge, timeout,
resource, and infrastructure failures make validation fail explicitly.
