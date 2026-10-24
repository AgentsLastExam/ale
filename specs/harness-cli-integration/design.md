# Shared CLI Harness and periodic evidence collection

Status: implemented in the feature worktree, 2026-10-23; not yet merged.
This document supersedes the implementation recommendations in [research.md](research.md).
It keeps the inspected baseline and factual findings there, while narrowing the work
to a shared CLI base, optional native session collection, and periodic trajectory
updates. Durable contracts live in `docs/specs/`; validation and remaining platform
coverage are recorded in [validation.md](validation.md).

## Scope and decisions

Keep ALE's Sandbox, Gateway, ATIF, and same-live-Sandbox continuation boundaries.
Do not add a capability registry, qualification framework, software-content checksum
scheme, cross-Sandbox recovery mechanism, or new trajectory format. Do not add new
CLIs or turn the earlier bug inventory into a prerequisite project.

The concrete change is that every native CLI follows the same execution and evidence
path. Adapter authors declare where their native sessions live and explain their
CLI's commands and records; shared code executes, collects, and schedules conversion.

The existing resume contract already covers the relevant continuation case. Sandbox
retention preserves the live environment and native state. Harbor's load/handoff
features are additional workflows, not evidence that ALE's native resume is missing.
Retaining a Sandbox does not itself keep an expired episode's Gateway authorization
alive; a later authorized continuation must still have a valid Gateway session.
This design does not expand continuation scope.

## Ownership

```text
ale.core.AutonomousHarness
  Generic autonomous-agent contract; also usable by non-CLI implementations
    |
ale.run.harnesses.base.CliHarness
  Shared native CLI execution, stdio, continuation checks, native-log declaration
    |
ClaudeCodeHarness / CodexCliHarness / GrokBuildHarness / OpenClawCliHarness
  Native commands, settings, success/error interpretation, native record semantics

StandardEnvironment
  Starts and stops one episode-local collection task; owns finalization and cleanup
    |
CliLogCollector
  Discovers declared files, downloads changes, maintains the Host mirror
    |
Sandbox public file operations
  Providers supply reads that can run concurrently with a long agent command
```

`CliHarness` lives in `ale.run`, not in `ale.core`. Oracle, policy Harnesses, and other
non-CLI implementations should not acquire shell or native-session requirements.
The Environment selects this common CLI path by type, never by individual CLI name.
Other Harness implementations keep their existing evidence path. Existing CLI
`logs` declarations and post-exit exports are replaced, not kept as duplicate paths.

The collection task, file offsets, command-local state, and stop signal belong to
an episode or invocation. The shared Harness instance holds immutable settings
and declarations only; concurrent episodes cannot share mutable run state.

## A small base class with two new native hooks

The following signatures summarize the implemented interface. Existing `install`,
`install_resources`, resource validation,
and pure `parse_trajectory` hooks retain their current responsibilities.

```python
@dataclass(frozen=True)
class CliCommand:
    argv: tuple[str, ...]
    env: dict[str, str]
    expected_session_id: str | None = None


@dataclass(frozen=True)
class NativeResult:
    session_id: str | None
    final_message: str | None


@dataclass(frozen=True)
class NativeLogs:
    root: str
    append_globs: tuple[str, ...] = ()


class CliHarness(AutonomousHarness):
    @property
    @abstractmethod
    def native_logs(self) -> tuple[NativeLogs, ...]: ...

    @abstractmethod
    async def prepare_command(
        self, instruction_path, sandbox, session, *, native_session_id
    ) -> CliCommand: ...

    @abstractmethod
    def read_result(self, result: ExecResult, session: HarnessSession) -> NativeResult: ...

    @abstractmethod
    def parse_trajectory(self, context: TrajectoryParseContext) -> AtifTrajectory: ...

    # launch() and resume() are concrete, final template methods.
    # Installation and resource configuration use the existing Harness hooks.
```

The two new native hooks have deliberately different jobs:

- `prepare_command` builds the CLI invocation and environment. The base supplies the
  already-written instruction file path; the adapter prepares native configuration
  and checks that a requested native session exists.
  `native_session_id=None` means a new run. For Claude, this hook allocates an ID and
  returns it as `expected_session_id`; for a fresh Codex run, the CLI reports the ID
  later. A resume always requests the continuation's exact ID.
- `read_result` interprets only this invocation's process result and output. It detects
  CLI-specific terminal errors, including errors reported with exit code zero, and
  returns the native session ID and final answer. Existing error-detail and
  subscription-error helpers remain available. It performs no file export or polling.

The base owns the common sequence:

1. On resume, validate Harness, episode, live Sandbox, and the existing effective
   settings/model/resources/authentication fingerprint. Reject unsupported resume.
2. Write the entire instruction as UTF-8 to `instruction.md`, then call
   `prepare_command` with its path. Grok and OpenClaw use native file options;
   Claude and Codex receive the fixed ASCII request `Read instruction.md in the current
   working directory and follow its instructions.` The original task text never
   passes through argv, environment variables, or stdin. Native configuration remains
   local, with no new command-description language or universal config renderer.
3. Execute as `Identity.AGENT` in `session.home`, with the supplied timeout and the
   existing Sandbox execution/cancellation behavior. Place stream redirection outside
   the entire prepared command so adapters do not choose their own log names or merge
   stderr into stdout.
4. Append raw stdout and stderr directly to separate standard files. Record their
   starting byte offsets and pass only this invocation's output to `read_result`,
   using the existing `ExecResult` shape. A failed or empty resumed turn must never
   reuse a preceding turn's successful result. No temporary segment-and-concatenate
   step is required for the evidence to exist while the CLI is running.
5. Compare the observed ID with any expected ID, and on resume with the original ID.
   Build `AgentRun` and `NativeContinuation` using the existing fingerprint helper.

Keep ordinary nonzero-exit rejection in the base; `read_result` supplies native
diagnostics and can classify the failure more precisely. A resume-capable adapter
must report an actual native ID before the base issues a continuation. A CLI without
native resume may explicitly return no ID and gets no continuation.

Before execution, the base also appends a small record to `invocations.jsonl`:
stdout/stderr starting byte offsets and the expected session ID, if known. The
collector mirrors this file with the two streams. A shared reader exposes each
invocation's byte range; a partial JSON line from an interrupted invocation must
not swallow the first line of the next invocation. This also separates successive
OpenClaw JSON documents without changing the raw output. The Environment uses the
recorded expected ID for live parse context when `AgentRun` is not yet available;
observed native IDs still require verification. This is execution bookkeeping,
not another native session model or an additional adapter hook.

This is one additional inheritance level and two new behavior hooks. Do not add
generic prepare/configure/observe/finalize hook chains, installation backends, or
event buses. Share existing identical helpers when migrating the current adapters.

## The uniform evidence rule

Every CLI declares three distinct evidence sources:

| Source | Meaning | Owner |
|---|---|---|
| `stdout.log` | Original command output; JSONL, JSON, or text according to the CLI | Base captures it unchanged |
| `stderr.log` | Original diagnostic output, kept separate | Base captures it unchanged |
| `native/` | Native session files, including child sessions and supporting session records | Adapter declares roots; collector mirrors them |

`stdout.log` is intentionally not called `transcript.jsonl`: OpenClaw emits a JSON
result, and a CLI event stream is not necessarily the complete conversation.
The two fixed guest stream paths live under an ALE-selected, agent-writable Harness
log directory that the base prepares before launch. The collector maps them to the
two fixed Host filenames above; invocation bookkeeping is stored alongside them.

`NativeLogs.root` is a home-relative directory or directory glob. Each resolved root
is collected recursively. Files retain their original home-relative paths under
`native/`, so multiple roots or child agents cannot collide. The declaration covers
session data, not the complete configuration/authentication home.

`append_globs` identifies files, relative to each resolved root, known to append for
the pinned CLI. All other files are copied as snapshots. It is a transfer hint, not
a parser selection rule; `.jsonl` alone does not prove append-only behavior.

```python
class ClaudeCodeHarness(CliHarness):
    native_logs = (NativeLogs(".claude-config/projects"),)


class CodexCliHarness(CliHarness):
    native_logs = (NativeLogs(".codex-ale/sessions", ("**/*.jsonl",)),)
```

Run-level `logging.collect_native_logs` defaults to `false`: native trees are neither
scanned nor downloaded unless enabled. Standard stdout/stderr and invocation records
are always collected. Existing `logging.native_logs` only controls post-conversion
retention; `debug` does not enable native collection.

When enabled, these declarations collect the entire isolated session trees. Verified
append sources are Codex's JSONL records and Grok's `updates.jsonl`. Claude can rewrite
or compact its JSONL, and Grok rewrites `chat_history.jsonl`; those use snapshots.
OpenClaw also uses snapshots because append behavior has not been established.
The root and child files are still collected when the process fails or never
returns an `AgentRun`. Missing files are rediscovered on the next poll.

| Current CLI | Declared native session scope | Change from the inspected implementation |
|---|---|---|
| Claude Code | `.claude-config/projects` recursively | Collect native root/child records in addition to stream-json; separate stderr |
| Codex CLI | `.codex-ale/sessions` recursively | Remove successful-exit-only root export; keep native paths and children |
| Grok Build | `.grok-ale/sessions` recursively | Collect history and updates directly; current stdout no longer waits for segment concatenation |
| OpenClaw CLI | `.openclaw-ale/agents/*/sessions` recursively | Include agent session directories; remove post-command transcript copy |

Claude uses ALE's isolated `CLAUDE_CONFIG_DIR`, so its actual source here is
`.claude-config/projects`, not the operator's ordinary `.claude` directory.
If a pinned CLI stores referenced conversation attachments outside these roots,
its adapter adds the specific session-data root. Do not silently claim that copying
the main JSONL alone captures those attachments.

`native_logs` has no inherited empty default: an adapter must make a deliberate
declaration. A CLI that genuinely has no native files may explicitly declare `()`
and explain that limitation in its README. The base cannot invent upstream data
or verify completeness merely from the existence of a directory.

Example Host layout:

```text
<episode>/
  trajectory.json
  logs/
    claude-code/
      stdout.log
      stderr.log
      invocations.jsonl
      native/.claude-config/projects/<project>/<session files and subdirectories>
    # A Codex episode instead has codex-cli/native/.codex-ale/sessions/...
```

## What stays in each trajectory parser

The shared collector copies evidence. It does not infer conversation semantics.
The pure function in each CLI's `trajectory.py`:

1. Identifies the exact root session from the CLI's native output/metadata, using
   the requested or returned ID when available. Collection does not wait for that ID.
2. Reads the root and follows that CLI's recorded parent/child relationships.
3. Combines native records and stdout by native identity and ordering, avoiding
   duplicate events. Native sessions usually provide conversation history; stdout
   may provide completion/error events or information absent from native history.
4. Converts messages, tool calls/results, reasoning, and child conversations into
   the existing ATIF models with `TrajectoryBuilder` and `BlobStore`.

Do not select the newest session or attach every discovered file to the root.
Unresolved files remain raw evidence. Neither a universal session manifest nor a
second intermediate trajectory format is needed. Collecting child files makes
their evidence available; it does not by itself implement their ATIF conversion.

## Periodic retrieval and conversion

Use one collector per active episode, with a fixed internal interval, initially
60 seconds. Bound each collection tick and worker shutdown as well as the final
pull, so joining an in-flight read cannot hang teardown. Do not expose a new
settings hierarchy for scheduling or introduce a retry framework.

```text
install and configure
start the collector
launch or resume the CLI
    collector: discover -> pull changes -> parse local files -> replace trajectory
CLI finishes, fails, or is cancelled
stop and join the collector
perform a bounded final pull and conversion
apply final transport links/accounting and native-log retention
cleanup
continue the existing verification/retention/release lifecycle
```

Each tick rediscovers files, including newly created subagent directories. Append
sources transfer only bytes after the last successfully written Host offset, in
bounded chunks up to the observed file size. Snapshot sources are replaced when
their metadata changes. Do not advance an offset before the corresponding bytes
are written locally. Skip conversion when no evidence changed.

The parser initially rebuilds the trajectory from the local files on each update.
This is incremental **transfer and publication**, not a stateful incremental parser.
It avoids per-CLI parser cursors and handles tool calls whose results arrive later.
Periodic conversion runs in a background thread so local parsing does not block the
Gateway's event loop. The thread returns a trajectory; only the episode loop publishes
it. Cancelling the poller discards a pending result. A late worker cannot publish,
perform retention, or modify final transport accounting. Concurrent identical blob
writes use unique temporary files and atomic replacement.

Reads use 256 KiB chunks. A periodic tick retrieves at most 8 MiB per append file,
publishes batches at 16 MiB per source, and rotates its cursor so later files are
not starved. A changed snapshot must be read in full. Each collection attempt has
a 30-second deadline; final collection removes per-file limits but keeps bounded
batch commits, preserving completed progress if the deadline expires.

Use the same `trajectory.json` and existing incomplete metadata throughout. During
execution it is marked `running`; the existing atomic writer prevents readers from
seeing half-written JSON. No separate preview format, live-event schema, or second
parser is introduced. BlobStore already deduplicates content from repeat parsing.

| Condition | Shared behavior |
|---|---|
| A source is not created yet | Discover again on the next tick |
| New child session appears | Include it in the next directory scan |
| JSONL ends in a partial line | Keep the raw bytes; parse the complete prefix while running; inspect/report the final tail at finalization |
| Append source shrinks or is replaced | Preserve the previous mirror for diagnosis, reset the offset, and read the new file; never concatenate generations |
| A snapshot changes while being read | Keep the previous good snapshot and retry; do not publish torn JSON as a complete snapshot |
| Source disappears temporarily | Keep the Host copy already collected |
| A periodic read or parse fails | Log it, retain the last valid trajectory, and retry on the next interval |
| Timeout or cancellation | Stop the poller and attempt bounded final collection; preserve the original execution failure |

Only regular files within the declared session roots are collected. Preserve the
original relative paths, and add explicit root-containment checks; a session
symlink must not make the collector traverse an unrelated configuration directory.
The guest scanner excludes symlinks and Windows junction/reparse points. File metadata
detects ordinary replacement;
it is not a content-integrity scheme. Sources with in-place rewrite semantics use
snapshot mode rather than relying on append offsets.

There is one trajectory publisher at a time: finalization starts only after the poller
has stopped; any abandoned conversion result is discarded. Retain raw evidence
throughout execution. Apply existing minimal/debug
retention only after final conversion; preserve raw inputs when conversion is
incomplete or fails. Cleanup must not remove native state required by a retained
Sandbox's supported continuation.

Cancellation currently abandons the Host wait and does not itself confirm that
guestd has stopped the process; the guest's own timeout may fire later. A confirmed
process exit permits final catch-up against settled files. If cancellation leaves
process exit unconfirmed, the bounded pull is an incomplete snapshot, and the
trajectory must say so. Do not perform destructive native cleanup or claim the
retained session is ready for reuse while execution remains unconfirmed. Keep the
existing provider teardown policy; a new process supervisor is outside this design.

Finalization cannot simply be called in a loop. It appends transport links,
consumes token evidence, sets `trajectory_written`, and may delete logs. Split its
internal path with `final=False/True`: periodic updates only parse and atomically
write; the final call performs those final-only effects once. If final conversion
fails after a valid periodic update, retain that trajectory with failure/incomplete
metadata and raw inputs, rather than replacing useful history with an empty fallback.
Use the existing minimal fallback only when no valid trajectory has been produced.

## Necessary Sandbox support

This is a real prerequisite, not an optional optimization: `GuestClient.call()` holds
one connection lock through a long `exec`, and guestd processes each stream serially.
A timer trying to read files on that same connection waits until the CLI exits.

Keep the existing process execution model and add one fixed file-read connection
per Sandbox. Do not introduce a general connection pool or a detached CLI supervisor.

- Docker opens a second connection through its existing stdio guestd connection
  helper. QEMU opens a second TCP connection to the existing threaded guest server,
  using the same port and framework token. No new external service is required.
- Public file discovery and reads use that connection. Harness code still sees only
  Sandbox, never a Provider or private GuestClient.
- Providers own both connections through startup failure, retention, image save,
  and destruction, and close both appropriately.
- Expose the already-supported guest read offset/length through
  `Sandbox.read_file(path, *, offset=0, length=None)`. Add a public file listing that
  supplies absolute guest path, size, modification time, and available file identity for
  regular files within the declared roots. Extend existing guest stat/list operations;
  do not build a separate file synchronization protocol. Update Sandbox wrappers and
  concrete implementations with the contract.

Both Providers implement the public metadata scan and ranged read operations. The
guest streams full reads in chunks as well. Concurrent reads were exercised with a
real Docker container and with QEMU's client against a local threaded guest server;
a real QEMU VM and Windows execution remain outside this validation run.

## Package structure

```text
ale/run/harnesses/
  base.py                 # CliHarness and its small command/result/log declarations
  logs.py                 # one episode-local CLI log collector
  evidence.py             # invocation boundaries and common evidence readers
  claude_code/
    __init__.py
    adapter.py            # native preparation/result interpretation, install/resources
    settings.py           # validated native settings
    trajectory.py         # pure native-record conversion
    README.md             # native sources, commands, and actual limitations
  codex_cli/
    ...
  grok_build/
    ...
  openclaw_cli/
    ...
  qwen_code/               # example for a later integration, not added in this phase
    adapter.py
    settings.py
    trajectory.py
    README.md
```

Keep the existing npm, shell, resource, and error helpers. Avoid a new utilities
framework or deep inheritance tree. The adapter is simply the small piece of code
that translates ALE's request into one CLI's commands and translates its result
back. Its name does not imply another service or framework layer.

## Focused implementation sequence

1. Implement the common base and evidence declaration. Migrate Claude and Codex
   first to remove the concrete stream/native-source inconsistency, then migrate
   Grok and OpenClaw to the same path. Update imports directly; no compatibility shims.
2. Add concurrent public file reads and the shared collector. Connect periodic
   parsing and finalization to StandardEnvironment, including failure paths.
3. Move native conversion/settings into the per-CLI packages while updating their
   evidence paths. Update the owning living specs and integration guide in the same
   implementation changes, then consider further Harness integrations separately.

Use existing tests and a few targeted cases for changed behavior: stdout/stderr
separation, current-turn result isolation on resume, root/child collection during a
running command, partial-line handling, and final catch-up after interruption. A
real supported Sandbox check must demonstrate that a file update reaches the Host
before the long command exits. Keep existing required platform/contract checks;
do not create a new test platform, broad qualification matrix, or scripted-model tier.

## Reference findings retained

Agent's Last Exam contributes the declaration/shared-poller/byte-offset/final-catch-up
principle. Its actual `tail_hot_artifacts` implementation uses a static file list
and currently polls at 60 seconds; trajectory parsing still happens at the end.
Do not copy its detached process supervisor or multiple pending-file states.

Harbor confirms that shared lifecycle mechanics and native per-agent conversion are
useful boundaries. Its optional live synchronization is relevant, but its large
installed-agent base and capability machinery are not the target structure here.

Source checkpoints: ALE `95fc769`, Agent's Last Exam `1237bd7`, Harbor `bfc6c00`.
The base, collector, Provider file operations, four existing adapter migrations, and
ATIF v1.8 update are implemented in this worktree. No new CLI was added. See
[the ATIF v1.8 comparison](atif-v1.8.md) for the official schema changes.
