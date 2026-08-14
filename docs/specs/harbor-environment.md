# Harbor Environment compatibility

`HarborEnvironment` runs a strict, deliberately bounded subset of Harbor single-step
Tasks through ALE's episode, Harness, result, and retention machinery. A `task.toml`
selects this protocol; a `task.yaml` selects the standard protocol. One run cannot mix
the two formats.

ALE reads Harbor Task folders directly. It does not invoke Harbor, convert the folder,
or require Harbor as a dependency.

## Supported folder

```text
task/
├── task.toml
├── instruction.md
├── environment/
│   └── Dockerfile             # unless environment.docker_image is set
├── solution/
│   └── solve.sh               # required by ale validate's oracle pass
└── tests/
    ├── test.sh                # verifier entrypoint
    └── Dockerfile             # optional separate verifier image
```

Only Linux, single-step Tasks with one main container are supported. The instruction is
read from `instruction.md`; leading Harbor canary comments are not included in the agent
instruction. Harbor package names are normalized to ALE Task IDs and must remain unique
within the selected collection.

## Manifest subset

ALE accepts both current `schema_version` and legacy `version`, plus these runtime fields:

- `environment`: `docker_image`, CPU, memory, storage, GPU count, environment variables,
  workdir, healthcheck, build timeout, and network policy;
- `agent`: timeout and network policy;
- `solution`: environment variables;
- `verifier`: timeout, environment variables, network policy, `shared` or `separate`
  placement, and an optional separate environment;
- `artifacts`: main-container source, destination, and exclusion declarations;
- descriptive task/package metadata.

Legacy `memory` and `storage` sizes such as `2G` are accepted. Network policy is one of
`public`, `no-network`, or `allowlist` with explicit hosts.

The loader rejects unsupported declarations instead of approximating them. This includes
Docker Compose and sidecars, multi-step Tasks, Windows, TPU, GPU type constraints,
task-declared MCP servers or skills, explicit users, verifier collect hooks, and sidecar
artifacts.

## Image and sandbox

If `[environment].docker_image` is present, ALE resolves that image and ignores
`environment/Dockerfile`, matching Harbor. Otherwise ALE builds the Dockerfile with
`environment/` as its context. Harbor Tasks are container-only in this protocol.

Ordinary Harbor images do not need ALE guestd, Python, an `ale.user` account, or an ALE
base image. `HarborDockerProvider` implements the existing Provider and Sandbox contracts
with direct Docker operations and starts the image with a framework keepalive command.
It enforces the declared CPU, memory, storage, GPU count, and network request without
changing `DockerProvider` or the public Sandbox interface.

The image's configured user and `WORKDIR` are preserved. Framework file operations use
root; the agent, oracle, and shared verifier use the image user. ALE creates a configured
workdir only when it is absent and never changes ownership of an existing directory.

## Episode protocol

```text
image preparation -> provision -> healthcheck -> agent/oracle
-> evidence and artifact capture -> shared or separate tests/test.sh
-> teardown/retention -> ALE Episode Result
```

There is no ALE standard `setup/` phase for a Harbor Task. Fixed setup belongs in the
Harbor environment image, as it does in Harbor.

An ordinary run uses the selected ALE Harness with the Harbor instruction. Validation's
oracle uploads `solution/` only after provisioning and executes `/solution/solve.sh` from
the image `WORKDIR` with the declared solution environment. Verification material is not
present during the evaluated agent phase.

Before verification ALE creates `/logs/verifier`, uploads `tests/` to `/tests` when it is
host-authored, and executes `/tests/test.sh` from the verifier image `WORKDIR`. The script
must write either a numeric `/logs/verifier/reward.txt` or a non-empty numeric object to
`/logs/verifier/reward.json`; JSON takes precedence. stdout and stderr are retained below
the ALE episode logs.

## Verifier placement and artifacts

Shared verification runs in the completed solver container. A shared verifier cannot
change the solver's effective network policy and cannot declare a verifier image.

Separate verification starts a fresh container after solver evidence capture. In order:

1. `tests/Dockerfile` builds a local verifier image when present;
2. otherwise `[verifier.environment].docker_image` supplies a verifier image when set;
3. otherwise the prepared solver image is reused as the fresh verifier base.

The fresh container does not receive the solver's mutable filesystem. With host artifact
collection enabled, declared artifacts and the conventional `/logs/artifacts` path are
captured after the agent and restored to their original paths before `test.sh`. A separate
verifier must declare every solver-created path it needs. Only main-container artifacts
are supported.

Both placements produce the same ALE reward map, Episode Result, RunLock, traces, and
retention records. Harbor's own job database and viewer files are not reproduced.
