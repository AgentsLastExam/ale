# 0008: Isolate Harbor task compatibility

**Status:** Accepted

## Context

ALE's `Environment` contract administers an evaluation protocol, while a Provider
creates Sandboxes. Harbor uses the word environment for the latter and puts its
evaluation protocol in `SingleStepTrial`. A Harbor-compatible ALE Environment must
therefore reproduce the supported Harbor trial behavior while continuing to use ALE's
episode, Harness, Gateway, evidence, and result machinery.

The current standard task types and loader are named as though they were protocol
neutral, although they require `task.yaml`, `image/`, `setup/`, `verify/`, `oracle/`,
and the `standard` Environment. Ordinary Harbor images also do not satisfy ALE's
standard image contract: Harbor supplies the long-running command at runtime and does
not require system Python or an `ale.user` account.

## Decision

Use the explicit Environment selectors `standard` and `harbor`. Do not add namespace
prefixes or a dynamic plugin registry.

Separate the protocol-neutral runtime task fields from the standard authored format:

- rename the runtime `TaskSpec` base to `BaseTaskSpec`;
- add `StandardTaskSpec` with `environment="standard"`;
- rename `TaskManifestV1`, `ManifestTask`, `TaskFolder`, and their loader functions to
  `StandardTaskManifest`, `StandardTask`, `StandardTaskFolder`, and
  `load_standard_tasks`;
- add `HarborTaskSpec`, `HarborTask`, and a Harbor loader inside `ale.run.harbor`;
- keep a small `load_tasks` dispatcher that selects a loader from the manifest actually
  present (`task.yaml` or `task.toml`). Mixed-format collections are rejected.

Keep Harbor-specific behavior together under `ale.run.harbor`:

```text
ale.run.harbor
├── config.py       supported task.toml model and validation
├── task.py         HarborTaskSpec, HarborTask, folder mapping, loader
├── environment.py Harbor single-step evaluation protocol
├── docker.py       Harbor-compatible Provider and Sandbox adapter
└── providers.py    Harbor Provider selection
```

The existing `DockerProvider`, `QemuProvider`, `Sandbox` interface, Harnesses, and
`StandardEnvironment` behavior remain unchanged. The Harbor Docker adapter implements
the existing Provider and Sandbox contracts with direct Docker exec/copy operations. It
supplies Harbor's runtime keepalive command and does not weaken the standard Provider's
image checks.

CLI composition makes two explicit selections after loading a homogeneous collection:

```python
if task.spec.environment == "standard":
    environment = StandardEnvironment(harness)
    providers = ProviderRegistry(settings)
elif task.spec.environment == "harbor":
    environment = HarborEnvironment(harness)
    providers = HarborProviderRegistry(settings)
```

No new Sandbox method is proposed for the first implementation.

## Initial compatibility scope

Supported:

- Linux, single-step Harbor tasks;
- one main container, built from `environment/Dockerfile` or acquired from
  `[environment].docker_image`;
- Harbor's prebuilt-image precedence when both a ref and Dockerfile exist;
- CPU, memory, storage, and GPU count;
- public, no-network, and hostname allowlist policies supported by ALE Docker runtime;
- environment variables, workdir, and healthcheck;
- ALE autonomous Harnesses and the no-op/oracle validation paths;
- `solution/solve.sh` with solution environment variables;
- shared verification from `tests/test.sh`;
- separate verification using `tests/Dockerfile`, an explicit verifier image, or a
  fresh solver image;
- `reward.json` and `reward.txt`;
- artifacts from the main container, including destination and exclusion rules.

Explicitly rejected at task load time:

- Docker Compose and sidecar services, collect hooks, and sidecar artifacts;
- multi-step tasks and trajectory resume;
- Windows;
- TPU requests;
- GPU type constraints;
- task-declared MCP servers and `skills_dir`;
- explicit agent or verifier users;
- Harbor job-level custom verifier import paths.

Rejection is preferable to silently changing Harbor semantics. Each rejected capability
can be considered independently after the initial compatibility path is proven.

## Consequences

The shared `BaseTaskSpec` and `TaskFolder` surface remain deliberately small. Protocol
fields stay on `StandardTaskSpec` or `HarborTaskSpec`, and runtime behavior stays in its
matching Environment. Supporting another task protocol requires another explicit loader
and Environment selection rather than adding ambiguous fields to either existing format.

No existing Provider behavior, Sandbox method, persisted schema version, or run result
schema changes as a result of this decision.
