# Episode protocol

The episode protocol (`EpisodeProtocol`) turns one prepared Task instance into one Episode
using the same phase ordering for container and VM sandboxes.

## Inputs

Before the protocol starts, the engine has already:

- loaded the Task folder into `TaskSpec`;
- linted the Task and prepared the solver image and any dedicated verifier image;
- resolved the Harness and the effective Skill/MCP resources;
- resolved the Harness authentication path, created its episode capabilities and
  evidence sinks, and prepared the Provider registry, run policy, and RunLock inputs.

Image acquisition and building are preparation, not a protocol phase. `ale prepare`
stops at that boundary and creates no sandbox.

## Phase order

```text
provision -> setup -> agent/oracle -> final evidence capture
          -> shared or separate verify -> teardown/retention -> result
```

Every phase is timed and attributed independently. Agent and verify default to
`TaskSpec.timeouts`. Setup has a fixed framework bound of 1800 s, a guard against a hung setup
rather than a scored budget. Run policy may override `timeouts.agent` with positive seconds and
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
`verify/` is also absent throughout this phase. Answers are withheld by when they are
staged, not by a conventional path or a flag a Task must remember.

### Evidence capture

While the agent runs, ALE copies the files and directories the Harness declares in
`Harness.logs` into `logs/<harness>/` every 60 seconds, so the evidence outlives a
Sandbox lost before the agent finishes. Each copy treats the logs as append-only: it
transfers the bytes beyond the Host copy, and a file that shrank is copied again from its
start. Nothing is hashed or converted while the agent runs, so a run's evidence cost
grows linearly with its logs. The rule is the same for every Harness and guest OS. File
reads use a second guest connection, so the running command cannot block them.

The guest enforces the agent deadline itself and stops the agent's process tree before
it replies, so the solver has stopped when evidence capture begins; the Host's own
deadline, 60 s later, only bounds a guest that no longer replies. ALE then stops copying
and makes a final copy with its own 300 s budget. Session files are appended to, but a
CLI may rewrite a small index or repair a session in place, so the final copy compares
each file's guest digest with its Host copy once and copies a rewritten file again.
Conversion is a pure function of the copied bytes, so it runs once, after the final
copy. An interrupted run or an incomplete copy produces a trajectory marked incomplete.

When run policy sets `artifacts.collect = "host"`, every Task-declared artifact is
captured as an immutable file or directory snapshot. Files and directories are selected
directly by `artifacts`, including globs. Copying never follows links and skips links,
missing and uncopyable entries, with bounded traversal; see
[Artifact copying](task-authoring.md#artifact-copying). The verifier owns scoring.

With the default `artifacts.collect = "none"`, ALE does not inspect artifact paths,
create a spool, copy bytes, or restore anything. A separate verifier that needs solver
output therefore requires host artifact collection.

### Verify

Shared verification uploads `verify/` into the completed solver sandbox. Separate
verification first releases or retains the solver according to run policy, provisions a
fresh verifier sandbox, restores captured artifacts to their original absolute paths,
and uploads the same `verify/`. It never reruns setup or copies the solver filesystem.

Verification runs as framework-controlled Task code with open egress and elevated
privileges (root on Linux, Administrator on Windows), in both shared and separate modes.
Task code may explicitly drop privileges when executing a submitted program.
ALE stages `ale_verify`, the instruction, optional solver trajectory, and run-level Judge
configuration, then executes the OS entry from the verification staging directory. Linux uses
`/opt/ale/verify/run.sh`; Windows uses `C:\ProgramData\ALE\verify\run.ps1`.
ALE selects Python 3.12+ (`python3`, then `python3.12` on Linux; `python.exe` on Windows)
and provides its command as `ALE_VERIFY_PYTHON`. Both placements leave the same
[result](verification.md#result) in `/logs/verifier` (`C:\logs\verifier`), which ALE empties
and gives to the framework alone before every attempt. The solver interpreter is unchanged.

### Teardown and retention

Solver and verifier retention are independent run policies and default to `destroy`.
Before a requested keep, ALE removes every stage of the Task (`setup/`, `verify/` with
its Judge state, the oracle), the verifier directory and known credentials, so a kept
solver holds only the agent's own state.
Subscription credentials never enter the Sandbox, so retention does not add a provider
credential cleanup step. Failure of other required sanitation still forces destruction.
Result and RunLock record the requested policy, actual Provider outcome, retained handle,
cleanup command, any reason, and the Gateway port a retained Sandbox was given.

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

## Continuation

`ale run TASK --continue-from EPISODE_DIR` gives the Task's instruction to the agent of an
earlier episode whose solver was kept, in that Sandbox and native session, for follow-up
turns on one workspace. The earlier episode must have retained its solver and recorded a
`continuation`. The run uses the same Harness at the same CLI version, and the Task the same
prepared solver image, resources and verification mode, because a running Sandbox keeps
its image, allocation and role. It also keeps the Gateway address it was given, which an
agent may hold in its native session: the run serves its Gateway on that port and fails
if the port is taken. The model, settings, instruction and verification may differ; the
RunLock records this episode's own and names the earlier one in `resumed_from`.

The episode adopts the retained solver, so its own retention decides the solver's fate,
and keeping it again lets a further episode continue. Setup runs again, the Harness finds
its CLI in place and is given this episode's Gateway session, the Sandbox is sealed, and
the Harness resumes the native session with the instruction instead of launching. Verification and
recording are unchanged. A continuation is not retried: its attempt already changed the
solver. With shared verification the next agent starts from the state verification left;
separate verification releases the solver first, so it holds only the agent's work.

`ale reverify --episode-retries N` (or `--set episode_retries=N`) retries incomplete
separate verification with a new verifier Sandbox each time, preserving the source solver.
Shared reverification remains a single attempt because verification may modify that same
source Sandbox; a clean retry requires a new full `ale run`. The original shared attempt
is still allowed when the retry configuration is nonzero.

## Validation

`ale validate` uses this same protocol twice with fresh sandboxes:

- untouched must complete with `reward` equal to zero;
- oracle must complete with `reward` equal to one;
- both use the [two-decimal result precision](verification.md#result) applied to every run;
  all additional result fields are ignored by validation;
- setup, image, resource, artifact, verifier, Judge, timeout, and infrastructure failures
  fail validation rather than becoming scores.

Validation tests Task feasibility and verifier behavior. It is separate from ordinary
run admission and from endpoint availability checks. The persisted `validation.json`
records the untouched and oracle attempts, scores, failures, and RunLock references.

Validation honors `episode_retries` independently for untouched and oracle episodes.
Every retry starts the complete protocol in fresh Sandboxes. Only each episode's last
result enters `validation.json`; a completed result with an incorrect reward map fails
validation without retrying to seek a different score. Attempt retention and cancellation
follow the [episode result contract](trace.md#result-and-run-status).

## Extension boundary

This is the only episode protocol. A Task that needs a different phase graph, several
cooperating solver sandboxes, or a human gate needs an engine change; it must not overload
fields in this contract.
