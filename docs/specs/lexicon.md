# Lexicon

Normative. Every term below has exactly one meaning in this codebase; ambiguous use is
a review defect. New names must be narrow rather than broad and must not collide here.

| Term | Definition | Never |
|---|---|---|
| **Sandbox** | An isolated execution instance (container or virtual machine) that the framework provisions for an episode. | never called an "environment" |
| **Provider** | The local backend that supplies sandboxes (`docker`, `qemu`). | not a cloud account, not a model vendor |
| **Environment** | The administration layer: how one task becomes one episode (provision → agent → verify → verdict). The main extension point for a domain. | never means a sandbox, never means shell variables |
| **TaskSpec** | The complete, frozen, serializable specification of one task instance. | not the folder on disk |
| **Task** | A `TaskSpec` bound to behaviour (setup, score, validate). | — |
| **Taskset** | A loader that yields tasks; named variants expand here. | — |
| **Variant** | A named parameterisation of one task, expanded by the taskset into its own task with its own identity. | not a separate task folder |
| **Episode** | One complete administration of one task instance by one agent; owns canonical trajectory, transport, execution, result, provenance, and referenced payload artifacts as applicable. | not a "trial", not a "job" |
| **Run** | One batch invocation of episodes, plus its Run Status Projection. | — |
| **Harness** | The adapter binding an agent to the framework. Two families by loop ownership: **AutonomousHarness** (the agent owns its loop; prompt in, result out; it may run inside the sandbox or outside against exposed interfaces) and **PolicyHarness** (the framework owns the observe/act loop and asks the harness for each step's decision). | not an "agent"; the family is never named after where the agent runs |
| **Harness Preset** | The complete version-controlled TOML configuration automatically selected for one autonomous harness. | not an implicit constructor default |
| **Skill Source** | An explicitly declared local directory containing one `SKILL.md` Skill or an immediate collection of Skills. | never ambient host agent state |
| **MCP Server** | A strictly validated vendor-neutral stdio or Streamable HTTP server definition made available to an agent. | not a vendor-native config file |
| **Effective Agent Resources** | The deduplicated union of Task-, preset-, Run-, and CLI-declared Skills and MCP servers for one episode. | never a winner-takes-all config layer |
| **CUA Desktop MCP** (`cua-desktop`) | ALE's built-in screen-control MCP server, selected at Run level and recorded through the ordinary MCP resource path. | not selectable by Task manifests or automatically injected by a harness or GUI image |
| **Native Continuation** | Opaque episode state that resumes one exact native agent session in its original live sandbox. | not Run ledger resume, transcript replay, or cross-sandbox restoration |
| **Limit Termination** | The recorded layer, limit name, configured value, and cause that stopped an episode. | not an untyped process exit |
| **GuestServer** (`ale-guestd`) | The in-sandbox service, preinstalled in every base image, through which all exec, file transfer and observation flows. Standard library only. | never provider-specific |
| **Gateway** | The host-side service that is the sole controlled egress for model and judge traffic; enforces limits by refusal, isolates credentials, records every call. | never knows about providers |
| **Kit** | A versioned code package injected into a sandbox, shipped with task content. | not a Python dependency of the engine |
| **ATIF Trajectory** | The episode's Harbor ATIF v1.7 document containing the complete agent-visible ordered interaction ALE can observe. | not framework lifecycle, setup, verification internals, or a native transcript |
| **Transport Trace** | The append-only Gateway-owned JSONL record of model calls, refusals, replay accounting, and trajectory links. | not full conversation storage |
| **Execution Trace** | The append-only JSONL record of framework phases, framework-owned commands, task-stage output, policies, and diagnostics. | not agent-owned tool activity |
| **Episode Result** | The sole terminal outcome: status, complete named rewards when verified, diagnostic metrics, bounded failure, phase timing, and timestamps. | not a scalar or aggregated reward |
| **Blob** | An immutable episode-local content-addressed payload under `blobs/<media-class>/`, referenced with path, media type, byte size, and digest. | not ordinary short inline text, not a task artifact |
| **Native Log** | Optional harness-native evidence retained under `logs/<harness>/` according to retention policy and used to construct the canonical trajectory. | not canonical while unparsed; not a task artifact |
| **Run Status Projection** | The run-level `ledger.db` view of queued, running, phase, terminal, interrupted, reward-map, and bounded-failure state. It is rebuildable from episode artifacts. | not an episode evidence store or source of full payloads |
| **RunLock** | The provenance record binding a result to everything that produced it. A result without a complete one is invalid. | not optional |
| **Workspace** | The agent's home directory, `/home/<user>`, derived from the account the image declares. Everything the agent touches is under it. There is no framework-wide layout: a task names its own absolute destinations. | not the repository, not a uv workspace, not a fixed set of directories |
| **Image manifest** | How a disk image declares what a container image declares with `ale.*` labels: a file at `/etc/ale/image.json` read through the guest service. | not a task manifest |

Deliberately retired names: `work_dir` (a run-level scratch directory; it was a second
answer to a question the image already answered by declaring its agent account, and two
answers can disagree — the workspace is the home), `Workflow` (collides with a task's own business process),
`InstalledHarness` / `StepwiseHarness` / `ProgramHarness` (they described where an agent
runs, which is not the distinguishing axis), bare `Trace`, `Semantic Trace`, `Verdict`,
`primary reward`, `primary_reward`, and `events.jsonl` (replaced by the orthogonal
canonical run artifacts), `TaskData` (read as "the task's input files"), `trial` /
`job` (imported vocabulary from other frameworks).
