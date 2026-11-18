# Episode protocol

The episode protocol (`EpisodeProtocol`) turns one prepared Task instance into one Episode
using the same phase ordering for container and VM sandboxes.

## Inputs

Before the protocol starts, the engine has already:

- loaded the Task folder into `TaskSpec` and resolved its
  [variables](task-authoring.md#variables);
- linted the Task, decided any image kind its folder leaves open
  ([Source selection](sandbox-image.md#source-selection-and-identity)), selected the
  Provider of each of its Sandboxes
  ([Provider selection](sandbox-image.md#provider-selection)), and prepared the solver
  image and any dedicated verifier image for those Providers;
- run each selected leaf's preflight, and probed the container features its Sandboxes need;
- resolved the Harness and the effective Skill/MCP resources;
- resolved the Harness authentication path, created its episode capabilities and
  evidence sinks, and prepared the Provider registry, run policy, and RunLock inputs.

Image acquisition and building are preparation, not a protocol phase. `ale prepare`
stops at that boundary and creates no sandbox.

## Phase order

```text
provision -> setup -> agent/oracle -> final evidence capture
          -> shared, snapshot or separate verify -> teardown/retention -> result
```

An agent that receives [validation rounds](#validation-rounds) repeats one step:
`agent -> save, validate, boot` once per round, before its last turn.

Every phase is timed and attributed independently. Agent and verify default to the Task's
`[agent].timeout_sec` and `[verifier].timeout_sec`. Setup has a fixed framework bound of
1800 s, a guard against a hung setup rather than a scored budget. Run policy may override
`timeouts.agent` with positive seconds and `timeouts.verify` with positive seconds or
`"unlimited"`, and `validation_rounds` with a non-negative count. These overrides are
recorded in RunLock and the configuration hash without changing the Task identity. The Agent
deadline starts at Harness launch (or policy rollout), after installation, resource staging,
and network sealing. Evidence capture, verification, and cleanup do not consume it. An Agent
timeout terminates execution, is not retried, and destroys its Sandbox even if retention was
requested, after verification. Teardown is cancellation-shielded on every exit path, and
SIGTERM cancels a command as Ctrl-C does, so a supervisor or timeout that stops `ale` still
lets it destroy every Sandbox it created.

### Provision

Each Sandbox comes from the Provider the Run selected for its Task and role before the
episode began ([Provider selection](sandbox-image.md#provider-selection)); a continuation
continues on the Provider that kept its solver. The Provider admits the requested
resources, creates the sandbox, and returns the image and resource allocation it actually
started. Those observations, rather than the request alone, enter RunLock.

Shared verification provisions one physical sandbox with role `shared`. Separate and
snapshot verification provision a `solver` sandbox first and a `verifier` sandbox only after
solver evidence capture; a snapshot verifier boots from a snapshot of the solver, on the
leaf that saved it.

The image declares the agent account and home. ALE does not add a Task path prefix or
pre-create declared artifacts. Framework staging uses `/opt/ale` on Linux and macOS
and `C:\ProgramData\ALE` on Windows.

### Setup

If `environment/setup/` exists, ALE uploads the complete directory to `setup` under the
staging root, opens egress, and executes `setup.sh` on Linux and macOS or `setup.ps1` on
Windows with that directory as cwd, so its files are available by ordinary relative paths.
Setup is optional and runs once.

### Agent or oracle

ALE rejects unsupported Harness/OS combinations before provisioning. It installs the
selected Harness and its declared resources, checks staged stdio MCP
commands, then applies the Task network policy. Autonomous Harnesses launch inside the
sandbox, in `[agent].workdir` or else the agent's home; Policy Harnesses drive the
framework-owned `TaskEnv`. The evaluated solver and
the oracle, which runs a calibration case, both run as the image's agent account. Windows
autonomous CLI Harnesses use the base's native Node.js and Git Bash.

Only the directory of the calibration case the oracle runs is uploaded, the reference
solution's under `ale run --agent oracle`, and only for an oracle episode; none is present
for an evaluated solver. `tests/` and `validation/` are also absent throughout this phase,
in every turn. Answers are withheld by when they are staged and by the root-only staging
directory, not by a conventional path or a flag a Task must remember.

An agent run that ends early, by a timeout, a limit, a failure or a refusal, moves on to
evidence capture and verification, as in Harbor, because what it left is still its work. The
Episode's status stays what stopped the agent, and `rewards` holds the score. If capture or
verification then fails, that status stands without a score. An agent phase that never reached
its run, or a guest that stopped replying, ends the Episode without verification.

A Sandbox lost to memory or storage ends the Episode at once, without verification, unless
what stopped the agent before stands as above: lost after an OOM kill in it, at any time
since it started (`MemoryExhaustedError`), or after it wrote past its `storage_mb` where its
Provider holds it to that by ending it (`StorageExhaustedError`). Of memory, that holds
whether the memory its Task declares ran out or the memory around it, such as the Host's: an
operator reads every failure, and one who finds the Host at fault fixes it and reruns, so ALE
need not tell the two apart. In the Sandbox the agent ran in, in its phase and in shared
verification, the status is `memory_exhausted` or `storage_exhausted`, the agent's own
[final](trace.md#result-and-run-status) result: what the agent leaves in its Sandbox,
processes, files in memory such as a tmpfs and files on its disk, still holds memory and
storage after it stops, so a Task's shared verifier must fit beside it. In setup, and in a
separate or snapshot verifier, which hold the Task's code and the agent's files alone, the
same loss is the Task's: `task_error`. Either way the failure is that error with the phase
it ended, and its message says what showed the loss. A process the kernel ends for memory
while the Sandbox goes on is no loss, and neither is a write that fails as on a full disk:
the Episode goes on. Which evidence counts, and where a Provider has none, is in the
[Provider contract](sandbox-image.md#provider-contract).

### Evidence capture

While the agent runs, ALE copies the files and directories the Harness declares in
`Harness.logs` into `logs/<harness>/` every 60 seconds, so the evidence outlives a
Sandbox lost before the agent finishes. Each copy treats the logs as append-only: it
transfers the bytes beyond the Host copy, and a file that shrank is copied again from its
start. Nothing is hashed or converted while the agent runs, so a run's evidence cost
grows linearly with its logs. The rule is the same for every Harness and guest OS. File
reads use a second guest connection, so the running command cannot block them. After a
[validation round](#validation-rounds) the copy goes on from the next turn's solver, whose
snapshot holds the same files.

The guest enforces the agent deadline itself and stops the agent's process tree before
it replies, so the solver has stopped when evidence capture begins; the Host's own
deadline, 60 s later, only bounds a guest that no longer replies. ALE then stops copying
and makes a final copy with its own 300 s budget. Session files are appended to, but a
CLI may rewrite a small index or repair a session in place, so the final copy compares
each file's guest digest with its Host copy once and copies a rewritten file again.
Conversion is a pure function of the copied bytes, so it runs once, after the final
copy. An interrupted run or an incomplete copy produces a trajectory marked incomplete.

With separate verification, or when run policy sets `artifacts.collect = "host"`, every
Task-declared artifact is captured as an immutable file or directory snapshot. Files and
directories are selected directly by `artifacts`, including globs. Copying never follows
links and skips links, missing and uncopyable entries, with bounded traversal; see
[Artifact copying](task-authoring.md#artifact-copying). The verifier owns scoring.

A separate verifier receives solver output only through the Host. With the default
`artifacts.collect = "none"`, ALE discards that copy with the episode, and with shared or
snapshot verification it inspects no artifact path and copies nothing.

Under snapshot verification ALE then saves the solver, still the agent's: it sanitizes it as
for retention and saves its disk as a snapshot, which stops it, within a framework bound of
1800 s, as setup's, so that the verifier's budget covers only booting the verifier, any wait
for room on the leaf that keeps the snapshot included, and its tests. A failure here, the
Sandbox's loss to memory or storage included, is the solver's, as one of evidence capture
is.

### Verify

Shared verification uploads `tests/` into the completed solver sandbox. Separate
verification first releases or retains the solver according to run policy, provisions a
fresh verifier sandbox, restores captured artifacts to their original absolute paths,
and uploads the same `tests/`. It never reruns setup or copies the solver filesystem.

Snapshot verification boots a fresh verifier Sandbox from the solver's snapshot, on the leaf
that saved it, with the verifier's resources and the solver's account, and uploads the same
`tests/`. Nothing is restored: the solver's whole disk is there, so verification that needs
all of it, and more resources than the solver had, neither copies it through the Host nor
changes the solver. The snapshot outlasts every Sandbox booted from it; once the verifier is
released per its retention, it is destroyed, unless the solver's retention keeps it.

Verification runs as framework-controlled Task code with open egress and elevated privileges
(root on Linux and macOS, Administrator on Windows), in every mode. Task code may explicitly
drop privileges when executing a submitted program. ALE stages `ale_verify`, the
instruction, optional solver trajectory, and run-level Judge configuration, then executes
the OS entry from the verification staging directory. Linux and macOS use
`/opt/ale/tests/test.sh`; Windows uses `C:\ProgramData\ALE\tests\test.ps1`. ALE selects
Python 3.12+ (`python3`, then `python3.12` on Linux and macOS; `python.exe` on Windows; then
the framework's own) and provides its command as `ALE_VERIFY_PYTHON`. Every placement
leaves the same [result](verification.md#result) in `/logs/verifier`
(`C:\logs\verifier`, macOS `/opt/ale/logs/verifier`), which ALE empties and gives to the
framework alone before every attempt. The solver interpreter is unchanged.

### Teardown and retention

Solver and verifier retention are run policies and default to `destroy`, each independent
of the other except under snapshot verification. `keep` leaves the Sandbox running. The
solver may instead request `snapshot`: once the episode is scored its disk is saved and the
Sandbox destroyed, and any number of later Sandboxes boot from the snapshot. A snapshot
holds no processes, memory or desktop session, so follow-ups that need them keep the
Sandbox. A solver to be snapshotted needs a Provider that saves snapshots, so selection
gives it none that cannot, before anything is provisioned.
Before either, ALE removes every stage of the Task (setup, tests with their Judge state,
the oracle's calibration case), the verifier directory and known credentials, so a kept
solver or a snapshot holds only the agent's own state.
Under snapshot verification the solver is saved whatever its retention, on a leaf that
saves snapshots and serves its verifier too, which selection chooses for both, and the
snapshot it keeps is the one its verifier booted from. It cannot be kept, since saving the
snapshot stops it, and a kept verifier needs the snapshot kept, since it runs on it; a Run
asking otherwise is refused before anything is prepared.
With validation rounds, retention concerns the last solver and the verifier of `tests/`.
No solver of an episode with rounds is kept running: each after the first runs on a
round's snapshot, which is never kept, and `snapshot` keeps the last one's disk. A setting
whose effective policy would keep such a solver running, `keep` for the solver, or for the
verifier under shared verification, where the verifier is the solver, is refused before
anything is prepared, and so is a calibration with one. Every round's verifier and snapshot
are destroyed. A round's snapshot that cannot be is recorded `not-destroyed`, with its
handle, the reason and its cleanup command, and stays listed by `ale sandbox list`; it
holds the agent's state between turns, so it is never a continuation or reverification
source.
Subscription credentials never enter the Sandbox, so retention does not add a provider
credential cleanup step. Failure of other required sanitation still forces destruction.
Result and RunLock record the requested policy, actual Provider outcome, retained handle,
cleanup command and any reason.

### Validation rounds

A Task's `[agent].validation_rounds`, or the Run's `validation_rounds`, is `N`, how many
results of [`validation/`](task-authoring.md#validation) the agent receives. With `N = 0`
the episode is the one above. With `N > 0` the agent works in up to `N + 1` turns, and a
round follows each turn but the last: its result is the agent's next prompt, and `tests/`
scores the final state once, as above. Only the evaluated agent receives rounds; the oracle
never does. A turn ends when the Harness's program finishes normally, exiting zero with no
limit, timeout or failure, so the agent submits by ending its turn and needs no command in
the Sandbox to do it, and any Harness that resumes its session can take part. A turn that
anything else ends, or that leaves none of the agent's time, is the last, and its state is
verified as any stopped agent's is. Each round, whatever the verification mode:

1. copies the agent's logs, as the periodic copy does, and, under separate verification,
   captures the declared artifacts;
2. sanitizes the solver as for retention, the credential files every Harness declares
   included, and saves it as a snapshot, which stops it and every process in it, under the
   framework bound the save for a snapshot verifier has; once the save succeeds, the
   previous round's snapshot is released, so at most one round's snapshot exists at a time;
3. runs `validation/` in the round's verifier, a new Sandbox placed as the mode places
   `tests/`'s: shared, booted from the snapshot with the solver's resources and no sudo,
   since the agent goes on from the state the snapshot holds; snapshot, booted from it with
   the verifier's resources; separate, of the verifier's image with the artifacts restored,
   which are deleted when the round ends. It runs under the verify limit,
   `[verifier].timeout_sec` or the Run's `timeouts.verify`, which its Judge calls share, and
   Run retention never keeps it;
4. boots the next turn's solver from the snapshot, with the solver's resources and network,
   on the leaf that saved it, without running setup again, so nothing setup started runs
   in it; the Harness finds its CLI in place, is given the episode's Gateway session, the
   Sandbox is sealed, and the Harness resumes its native session with the round's prompt.

The rounds are the same in every mode so that nothing a verifier does reaches the state the
agent continues from: each turn after the first starts from the snapshot, which no verifier
changed. The agent's time, `[agent].timeout_sec` or the Run's `timeouts.agent`, covers all
its turns, and each turn gets what is left; saves, validation, boots and staging spend none
of it, so a Task's time limit means the same with and without rounds. Gateway limits count
across the turns, which share one session; between turns and after the last the agent's
session is closed ([Security](security.md#5-answers-are-absent-not-hidden)). The next turn's
solver is sealed as soon as it boots, and a boot that finds no room, as when another Run
took its GPUs while `validation/` ran, is tried again every 10 s for up to 30 minutes, a
framework bound outside the agent's time. A round whose verifier produces no score, by
failing, timing out or losing its Sandbox, is recorded with its failure and given to the
agent as unscored, and the episode goes on; a save or boot that fails ends the
episode without a score, as a failed save for a snapshot verifier does.

The prompts are framework-owned and neutral. Below the instruction, which stays verbatim
above it, a preamble says that ending a turn sends the work to a validation set whose result
the agent receives before it continues, at most `N` times, that the final state is then
scored once on a separate hidden test set, that the time limit covers all turns and that no
process survives a turn's end. Each round's prompt gives the round, its reward, or that it
has none and why, the verifier's [feedback](verification.md#feedback) verbatim, the rounds
and minutes left, and whether ending the next turn validates again or scores on the test
set.

Rounds need what each step does: a Task with `validation/`, a Harness with native resume,
since every turn after the first resumes the session, and a leaf that saves as many
snapshots in a row as the solver plans
([Provider selection](sandbox-image.md#provider-selection)) and, for a shared or snapshot
verifier, serves the round's verifier too. A Run giving rounds to a Harness without native
resume is refused before provisioning, and one that no leaf serves before anything is
prepared; either can set `validation_rounds = 0`.

## Reverification

`ale reverify TASK SOURCE_RUN` is an internal authoring optimization. Each source episode
must have a complete RunLock, the same prepared solver-image identity as the current Task,
and its solver kept as the current verification needs: running, for shared and separate
verification, or as a snapshot, for snapshot verification. A source of the other kind is
refused, and so is a kept shared Sandbox for separate or snapshot verification, since its
disk holds what its shared verifier changed. An episode with validation rounds keeps no
solver running, so it can be reverified only from a snapshot, under snapshot verification.

For shared verification, ALE reconnects to the retained shared Sandbox and runs the current
`tests/` there. For separate verification, ALE captures the declared artifacts from the
retained solver, detaches without destroying it, provisions a fresh verifier Sandbox, restores the
artifacts, and runs the current `tests/` there. For snapshot verification, ALE boots a fresh
verifier Sandbox from the source's snapshot, on the Provider that keeps it, captures the
declared artifacts there when run policy collects them, and runs the current `tests/`,
leaving the snapshot as it was. Before any path uploads `tests/`, ALE removes the previously
staged verifier files so deleted files cannot survive a revision.

The Harness, setup, and agent phases do not run again. The new episode copies the source trajectory,
with the blobs it references, for verifier and review use and records its own verification, result,
execution trace, and RunLock.

## Continuation

`ale run TASK --continue-from EPISODE_DIR` gives the Task's instruction to the agent of an
earlier episode whose solver was kept or snapshotted, in that Sandbox or one booted from the
snapshot, and in that native session, for follow-up turns on one workspace. The earlier
episode must have retained its solver and recorded a `continuation`. The run uses the same
Harness at the same CLI version, and the Task the same prepared solver image. A kept solver
also needs the same resources, role (shared, or a solver beside a verifier of its own) and
desktop resolution, because a running Sandbox keeps its allocation, role and desktop; one
booted from a snapshot takes the Task's and the Run's. Under snapshot verification, or in a
Run with validation rounds, a kept solver is not continued, since saving it for its
verifier or its first round would stop it: the Run is refused before anything is
provisioned, and a snapshot of it is continued instead. The model,
settings, instruction and verification may differ; the RunLock records this episode's own
and names the earlier one in `resumed_from`. The agent finds this run's Gateway at the
address it was given, since every Sandbox reaches its Gateway at the same one.

A kept solver is adopted, so the episode's own retention decides its fate, and keeping it
again lets a further episode continue. A snapshot is left as it was, so it can be
continued any number of times, concurrently. Setup runs again, the Harness finds its CLI
in place and is given this episode's Gateway session, the Sandbox is sealed, and the
Harness resumes the native session with the instruction instead of launching.
Verification and recording are unchanged. A continuation of a kept solver is not retried,
since its attempt already changed the solver; one from a snapshot is retried like any
episode. With shared verification the next agent starts from the state verification left;
separate and snapshot verification leave the solver first, so it holds only the agent's
work.

`ale reverify --episode-retries N` (or `--set episode_retries=N`) retries incomplete
separate or snapshot verification with a new verifier Sandbox each time, preserving the
source solver or snapshot.
Shared reverification remains a single attempt because verification may modify that same
source Sandbox; a clean retry requires a new full `ale run`. The original shared attempt
is still allowed when the retry configuration is nonzero.

## Calibration

`ale calibrate` runs every [calibration case](task-authoring.md#calibration) of a Task
through this same protocol as the oracle, each a complete episode in fresh Sandboxes:
`solution` first, then the other cases in the order `task.toml` lists them. When the Task
has `validation/`, each case also runs a validation check: `validation/` once on the case's
final state, before `tests/`, in a verifier placed as a round's is, with no turn after it,
so it is not a round. The oracle's solver is saved for it, so both verifiers score one
state, and under shared verification `tests/` then runs in a solver booted from that
snapshot, where it sees, as `validation/` always does, no process the oracle left running.

- a case passes when its episode completes and meets its
  [expectation](task-authoring.md#calibration), and, when the Task has `validation/`, its
  validation check completes and meets the expectation for `validation/`;
- setup, image, resource, artifact, verifier, Judge, timeout, and infrastructure failures
  fail the case rather than becoming scores.

Calibration tests Task feasibility and verifier behavior. It is separate from ordinary
run admission and from endpoint availability checks. The persisted `calibration.json`
records, for each case, its expectation, its attempt with scores and RunLock reference, the
values it observed and its failures, the same of its validation check, and whether each
case and Task passed.

Calibration honors `episode_retries` independently for each case. Every retry starts the
complete protocol in fresh Sandboxes. Only each case's last result enters
`calibration.json`; a completed result outside its expectation fails the case without
retrying to seek a different score. Attempt retention and cancellation follow the
[episode result contract](trace.md#result-and-run-status).

## Extension boundary

This is the only episode protocol. A Task that needs a different phase graph, several
cooperating solver sandboxes, or a human gate needs an engine change; it must not overload
fields in this contract.
