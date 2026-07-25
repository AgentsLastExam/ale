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
| **Episode** | One complete administration of one task instance by one agent; produces a trace and a verdict. | not a "trial", not a "job" |
| **Run** | One batch invocation of episodes, plus its ledger records. | — |
| **Harness** | The adapter binding an agent to the framework. Two families by loop ownership: **AutonomousHarness** (the agent owns its loop; prompt in, result out; it may run inside the sandbox or outside against exposed interfaces) and **PolicyHarness** (the framework owns the observe/act loop and asks the harness for each step's decision). | not an "agent"; the family is never named after where the agent runs |
| **GuestServer** (`ale-guestd`) | The in-sandbox service, preinstalled in every base image, through which all exec, file transfer and observation flows. Standard library only. | never provider-specific |
| **Gateway** | The host-side service that is the sole controlled egress for model and judge traffic; enforces limits by refusal, isolates credentials, records every call. | never knows about providers |
| **Kit** | A versioned code package injected into a sandbox, shipped with task content. | not a Python dependency of the engine |
| **Verdict** | The result envelope: one status from the taxonomy, a rewards map with a primary key, and diagnostic metrics. | not a bare number |
| **Trace** | The two-layer record of an episode: transport (every model call) and semantic (steps). | not a log file |
| **RunLock** | The provenance record binding a result to everything that produced it. A result without a complete one is invalid. | not optional |
| **Workspace** | The fixed in-sandbox layout `/ale/{input,software,output,work}` (plus `/ale/reference`, verification only). | not the repository, not a uv workspace |

Deliberately retired names: `Workflow` (collides with a task's own business process),
`InstalledHarness` / `StepwiseHarness` / `ProgramHarness` (they described where an agent
runs, which is not the distinguishing axis), `TaskData` (read as "the task's input
files"), `trial` / `job` (imported vocabulary from other frameworks).
