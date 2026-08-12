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

Every phase is timed and attributed independently. Setup, agent, and verify use the
deadlines from `TaskSpec.timeouts`. Teardown is cancellation-shielded and runs on every
exit path.

### Provision

The prepared image kind selects the matching configured Provider. The Provider admits
the requested resources, creates the sandbox, and returns the image and resource
allocation it actually started. Those observations, rather than the request alone, enter
RunLock.

Shared verification provisions one physical sandbox with role `shared`. Separate
verification provisions a `solver` sandbox first and a `verifier` sandbox only after
solver evidence capture.

The image declares the unprivileged agent account. Its home is the solver workspace.
Framework material lives under root-owned `/opt/ale`; ALE does not add a Task path prefix
or pre-create declared artifacts.

### Setup

If `setup/` exists, ALE uploads the complete directory to `/opt/ale/setup`, opens egress,
and executes `run.sh` as trusted root with that directory as cwd. `assets/` is therefore
available by an ordinary relative path. Setup is optional and runs once.

### Agent or oracle

ALE installs the selected Harness and its declared resources, validates staged stdio MCP
commands, then applies the Task network policy. Autonomous Harnesses launch inside the
sandbox; Policy Harnesses drive the framework-owned `TaskEnv`. The evaluated solver and
the validation oracle both run as the image's agent account.

`oracle/` is uploaded only for an oracle episode and is absent for an evaluated solver.
`verify/` is also absent throughout this phase.

### Evidence capture

After the solver stops, the Harness performs idempotent cleanup and ALE converts its
observed evidence into `trajectory.json`. When run policy sets `artifacts.collect =
"host"`, every Task-declared artifact is captured as an immutable file or directory
snapshot. Missing, overlapping, symlink, special-file, and transfer failures are explicit
errors.

With `artifacts.collect = "none"`, ALE does not inspect artifact paths, create a spool,
copy bytes, or restore anything. A separate verifier that needs solver output therefore
requires host artifact collection.

### Verify

Shared verification uploads `verify/` into the completed solver sandbox. Separate
verification first releases or retains the solver according to run policy, provisions a
fresh verifier sandbox, restores captured artifacts to their original absolute paths,
and uploads the same `verify/`. It never reruns setup or copies the solver filesystem.

Verification runs as trusted root with open egress. ALE stages `ale_verify`, the rendered
instruction, parameters, optional solver trajectory, and run-level Judge configuration,
then executes `verify/run.sh` from `/opt/ale/verify`. Both placements must produce the
same reward envelope and `verification.json` contract.

### Teardown and retention

Solver and verifier retention are independent run policies and default to `destroy`.
Before a requested keep, ALE removes known verification credentials and Judge state.
Subscription credentials never enter the Sandbox, so retention does not add a provider
credential cleanup step. Failure of other required sanitation still forces destruction.
Result and RunLock record the requested policy, actual Provider outcome, retained handle,
cleanup command, and any reason.

## Validation

`ale validate` uses this same protocol twice with fresh sandboxes:

- untouched must complete with a non-empty all-zero reward map;
- oracle must complete with the same reward names;
- the oracle's actual values are recorded, and non-one values are warnings;
- setup, image, resource, artifact, verifier, Judge, timeout, and infrastructure failures
  fail validation rather than becoming scores.

Validation tests Task feasibility and verifier behavior. It is separate from ordinary
run admission and from endpoint availability checks. The persisted `validation.json`
contract is [schema version 1](schemas/validation-observation-v1.json).

## Extension boundary

`core/standard` is the only standard Task protocol. A protocol that requires a different
phase graph, several cooperating solver sandboxes, or a human gate is a new `Environment`
implemented in the engine; it must not overload fields in this contract.
