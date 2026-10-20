# Standard Environment evaluation protocol

`StandardEnvironment` implements ALE's `core/standard` evaluation protocol. It turns one
prepared Task instance into one Episode using the same phase ordering for container and
VM sandboxes.

## Inputs

Before the Environment starts, the engine has already:

- loaded and rendered one base or selected variant into `TaskSpec`;
- linted the Task and prepared the solver image and any dedicated verifier image;
- resolved the Harness and the effective Skill/MCP resources;
- resolved the Harness authentication path, created its episode capabilities and
  evidence sinks, and prepared the Provider registry, run policy, and RunLock inputs.

Image acquisition and building are preparation, not an Environment phase. `ale prepare`
stops at that boundary and creates no sandbox.

## Phase order

```text
provision -> setup -> agent/oracle -> Harness cleanup -> evidence capture
          -> shared or separate verify -> teardown/retention -> result
```

Every phase is timed and attributed independently. Setup, agent, and verify default to
`TaskSpec.timeouts`. Run policy may override `timeouts.agent` with positive seconds and
`timeouts.verify` with positive seconds or `"unlimited"`. These overrides are recorded in
RunLock and the configuration hash without changing the Task identity. The Agent deadline
starts at Harness launch (or policy rollout), after installation, resource staging, and
network sealing. Evidence capture, verification, and cleanup do not consume it. An Agent
timeout terminates execution, destroys its Sandbox even if retention was requested, and
returns `timeout` without retry. Teardown is cancellation-shielded on every exit path.

### Provision

The prepared image kind selects the matching configured Provider. The Provider admits
the requested resources, creates the sandbox, and returns the image and resource
allocation it actually started. Those observations, rather than the request alone, enter
RunLock.

Shared verification provisions one physical sandbox with role `shared`. Separate
verification provisions a `solver` sandbox first and a `verifier` sandbox only after
solver evidence capture.

The image declares the agent account and home. ALE does not add a Task path prefix or
pre-create declared artifacts. Framework staging uses `/opt/ale` on Linux and
`C:\ProgramData\ALE` on Windows.

### Setup

If `setup/` exists, ALE uploads the complete directory to the OS staging root, opens
egress, and executes `run.sh` on Linux or `run.ps1` on Windows with that directory as cwd.
`assets/` is therefore available by an ordinary relative path. Setup is optional and runs once.

### Agent or oracle

ALE rejects unsupported Harness/OS combinations before provisioning. It installs the
selected Harness and its declared resources, validates staged stdio MCP
commands, then applies the Task network policy. Autonomous Harnesses launch inside the
sandbox; Policy Harnesses drive the framework-owned `TaskEnv`. The evaluated solver and
the validation oracle both run as the image's agent account. Windows autonomous CLI
Harnesses use the base's native Node.js and Git Bash.

`oracle/` is uploaded only for an oracle episode and is absent for an evaluated solver.
`verify/` is also absent throughout this phase.

### Evidence capture

After the solver stops, the Harness performs idempotent cleanup and ALE converts its
observed evidence into `trajectory.json`. When run policy sets `artifacts.collect =
"host"`, every Task-declared artifact is captured as an immutable file or directory
snapshot. Task-authored `artifact_excludes` omit exact relative files or subtrees from each
directory snapshot. Excluded entries are never traversed; included symlinks and special
files remain errors. Missing, overlapping and transfer failures are explicit errors,
except for a missing artifact with selection or an explicit opt-in as described below.
For Linux container Tasks, filtered collection stages an independent tree before transfer;
the exclusion list is recorded in the snapshot manifest.

Alternatively, Task-authored `artifact_include_files` selects exact relative file paths
from directory artifacts. The solver sandbox resolves selected paths as the agent
account, including links, and ALE stores their regular-file contents rather than links.
For separate verification, `artifact_omit_unreadable_files: true` omits explicitly
selected files when agent-side access is denied. The snapshot records the policy and
the omitted paths; separate verification sees them as missing. This option requires
`artifact_include_files` and does not suppress other capture failures.
`artifact_include_directories` may select entire relative subtrees in the same snapshot;
links inside those subtrees are followed as the agent and their contents are stored.
Unreadable files, dangling links and special files within a selected subtree fail capture.
Missing paths and paths of the wrong type are omitted by default. With
`artifact_preserve_file_directories: true`, a selected file path resolving to a directory
becomes an empty directory without traversing its contents. Unselected paths are never
traversed. If a selected
artifact root is missing, its snapshot has `kind: missing`;
restoration removes the corresponding verifier path so it remains missing. Both
selections are recorded in the snapshot manifest. This policy is limited to Linux
containers and cannot be combined with exclusions.

`artifact_allow_missing: true` also records absent artifact roots as missing, with or
without exclusions or selection. Separate restoration removes those paths from the
verifier. The option applies to all declared artifact paths on Linux containers and VMs.
Path filtering remains limited to Linux containers. The option does not suppress
inspection or transfer errors, and its use is
recorded in each snapshot entry.

`artifact_include_regular_files` instead selects direct regular-file entries from
each declared relative directory, optionally recursing into real subdirectories.
It skips symbolic links and special files and omits empty descendant directories.
Explicit selected files can overlap and retain their ordinary link-following behavior.
Missing or non-directory selections are omitted; unreadable selected entries fail
capture. An optional case-sensitive `pattern` filters entry names using filename
wildcards. With `follow_symlinks: true`, the selected directory and matching links
to regular files are followed as the agent; directory links are never traversed
recursively. The snapshot contains bytes rather than links. With the explicit
`unreadable: empty` policy, a matching regular file that cannot be opened due
to permissions becomes an empty placeholder. The snapshot records each replaced
path; other capture errors still fail. Use this only when the verifier treats
an unreadable file and empty contents equivalently. This policy requires separate
verification. This selection has the same Linux container and exclusion restrictions
and is recorded in the snapshot manifest. A nonrecursive regular selection can contain
an explicitly selected directory subtree; recursive overlaps are rejected.

With `artifacts.collect = "none"`, ALE does not inspect artifact paths, create a spool,
copy bytes, or restore anything. A separate verifier that needs solver output therefore
requires host artifact collection.

### Verify

Shared verification uploads `verify/` into the completed solver sandbox. Separate
verification first releases or retains the solver according to run policy, provisions a
fresh verifier sandbox, restores captured artifacts to their original absolute paths,
and uploads the same `verify/`. It never reruns setup or copies the solver filesystem.

Verification runs as framework-controlled Task code with open egress. ALE stages `ale_verify`, the rendered
instruction, parameters, optional solver trajectory, and run-level Judge configuration,
then executes the OS entry from the verification staging directory. Linux uses
`/opt/ale/verify/run.sh` and system `python3`; Windows uses
`C:\ProgramData\ALE\verify\run.ps1` and system `python.exe`. Both placements must produce
the same reward envelope and `verification.json` contract.

### Teardown and retention

Solver and verifier retention are independent run policies and default to `destroy`.
Before a requested keep, ALE removes known verification credentials and Judge state.
Subscription credentials never enter the Sandbox, so retention does not add a provider
credential cleanup step. Failure of other required sanitation still forces destruction.
Result and RunLock record the requested policy, actual Provider outcome, retained handle,
cleanup command, and any reason.

## Reverification

`ale reverify TASK SOURCE_RUN` is an internal authoring optimization. Each source episode must
have a running retained solver or shared Sandbox, a complete RunLock, and the same prepared
solver-image identity as the current Task.

For shared verification, ALE reconnects to the retained shared Sandbox and runs the current
`verify/` stage there. For separate verification, ALE captures the declared artifacts from the
retained solver, detaches without destroying it, provisions a fresh verifier Sandbox, restores the
artifacts, and runs the current `verify/` stage there. Before either path uploads `verify/`, ALE
removes the previously staged verifier files so deleted files cannot survive a revision.

The Harness, setup, and agent phases do not run again. The new episode copies the source trajectory
for verifier and review use and records its own verification, result, execution trace, and RunLock.

`ale reverify --episode-retries N` (or `--set episode_retries=N`) retries incomplete
separate verification with a new verifier Sandbox each time, preserving the source solver.
Shared reverification remains a single attempt because verification may modify that same
source Sandbox; a clean retry requires a new full `ale run`. The original shared attempt
is still allowed when the retry configuration is nonzero.

## Validation

`ale validate` uses this same protocol twice with fresh sandboxes:

- untouched must complete with a non-empty all-zero reward map;
- oracle must complete with the same reward names;
- every oracle reward must equal one;
- setup, image, resource, artifact, verifier, Judge, timeout, and infrastructure failures
  fail validation rather than becoming scores.

Validation tests Task feasibility and verifier behavior. It is separate from ordinary
run admission and from endpoint availability checks. The persisted `validation.json`
contract is [schema version 1](schemas/validation-observation-v1.json).

Validation honors `episode_retries` independently for untouched and oracle episodes.
Every retry starts the complete protocol in fresh Sandboxes. Only each episode's last
result enters `validation.json`; a completed result with an incorrect reward map fails
validation without retrying to seek a different score. Attempt retention and cancellation
follow the [episode result contract](trace.md#result-and-run-status).

## Extension boundary

`core/standard` is the only standard Task protocol. A protocol that requires a different
phase graph, several cooperating solver sandboxes, or a human gate is a new `Environment`
implemented in the engine; it must not overload fields in this contract.
