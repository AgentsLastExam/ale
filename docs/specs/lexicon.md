# Lexicon

Normative. Every term below has exactly one meaning. The public Task manifest version
remains `core/v1`.

| Term | Definition | Never |
|---|---|---|
| **Sandbox** | An isolated execution instance supplied for one episode. | never called an Environment |
| **Provider** | A backend that creates and destroys Sandboxes and enforces requested resources. | not a model vendor or Task-selected setting |
| **Environment** | The administration layer that turns one Task into one Episode: provision, setup, agent, verify, teardown. | never means a Sandbox or shell variables |
| **Task folder** | The complete self-contained authored source unit: manifest, instruction, image build context, setup, verification, oracle, and optional agent resources. | not dependent on `domain.yaml`, a repository Kit, sibling Task, or shared domain image |
| **Task source digest** | A digest of the Task folder's canonical paths, bytes, executable bits, and symlink targets, pruning only the four exact stage asset roots and generated state. | not a hash of `task.yaml` alone or of asset bytes |
| **TaskSpec** | The frozen, serializable effective specification of one selected Task instance after instruction rendering and variant application. | not the folder on disk |
| **Task** | One self-contained Task folder bound to its effective TaskSpec and standard behavior. | not a collection or loader |
| **Task collection** | A directory or source that contains several independent Task folders for selection. | not a Task and not an execution contract |
| **Variant** | An additional named parameter/resource/timeout instance of one Task. The top-level Task is always `base`. | not allowed to change image, setup, verification, network, artifacts, Skills, or MCP |
| **Image declaration** | Required `image.kind: container|vm` plus optional `image.ref` for a solver or dedicated verifier. | not a Provider selection or build flag |
| **Task image** | The final container or VM image prepared from one fixed local Dockerfile or matching-kind ref. | not a shared Image Tree node |
| **Prepared Task image** | Immutable kind-aware output consumed by a sandbox request. | not mutable sandbox state |
| **ALE base image** | A foundational CLI, GUI, or Ubuntu VM GUI OCI image providing the sandbox contract and guest service. | not a domain image or Task-specific dependency bundle |
| **VM materializer** | ALE-owned versioned conversion from final VM OCI rootfs to bootable qcow2. | never Task-authored boot or partition code |
| **Task assets** | Optional ignored files directly below a Task's `image/assets`, `setup/assets`, `verify/assets`, or `oracle/assets`, synchronized explicitly with a same-named HF dataset. | not a manifest declaration, central cache tree, content hash, or implicit runtime download |
| **Image assets** | Task-local `image/assets` bytes consumed through the ordinary `image/` Docker context. | not a named BuildKit context or runtime mount |
| **Verify assets** | Task-local `verify/assets` bytes uploaded only with the verify stage and read by verifier code through ordinary relative paths. | never baked into the solver image or published during agent execution |
| **Oracle assets** | Task-local `oracle/assets` bytes uploaded only when the oracle harness runs and read through ordinary relative paths. | never uploaded for an evaluated solver |
| **Setup** | Trusted root work that creates irreducibly per-episode state after the Task image starts. | not package installation, fixed compilation, or fixed input download |
| **Framework Verification Library** (`ale_verify`) | The engine-owned Python package staged for verify; it composes checks, direct Judges, aggregates, stats, and the final reward map. | not a repository Kit, Host verification service, Gateway client, or plugin |
| **Judge Invocation** | One attributable LLM or agent Judge execution with its resolved configuration, attempts, usage, verdict, and evidence links. | not a Task manifest declaration or reusable profile |
| **Verification Record** | The sandbox-owned `verification.json` derivation of rewards: criteria, stats, aggregates, Judge invocations, diagnostics, and failure. | not the Episode Result or solver trajectory |
| **Artifact snapshot** | Immutable regular-file/directory solver evidence captured after Harness cleanup and before verification, with exact source path, type, mode, and content identity. | not post-verifier bytes or an implicit workspace copy |
| **Separate verifier** | A fresh verifier sandbox using reused solver content, a Task-local verifier build, or a resolved external image, with independently requested resources and exact artifact restoration. | not a second setup run or a copy of the whole solver filesystem |
| **Retained sandbox** | An ALE-managed, sanitized debug sandbox returned with a durable Provider handle and cleanup command when run policy requests `keep`. | not Task-controlled or an unmanaged leaked container |
| **Episode** | One complete administration of one selected Task instance by one agent. | not a trial or job |
| **Run** | One batch invocation of Episodes plus its Run Status Projection. | — |
| **Harness** | The adapter binding an agent program to ALE. An Autonomous Harness owns its loop; a Policy Harness drives a framework-owned Task environment. | not an agent identity or Task setting |
| **Harness Preset** | Complete version-controlled configuration automatically selected for one Harness. | not an implicit constructor default |
| **Skill Source** | An explicitly declared local directory containing one Skill or immediate Skill collection. | never ambient host agent state |
| **MCP Server** | A validated stdio or Streamable HTTP server definition made available to an agent. | not a vendor-native config file |
| **Effective Agent Resources** | The deduplicated union of Task-, preset-, Run-, and CLI-declared Skills and MCP servers. | never winner-takes-all configuration |
| **CUA Desktop MCP** (`cua-desktop`) | ALE's built-in screen-control MCP server selected at Run level. | not selected by a Task or automatically injected by an image |
| **Native Continuation** | Opaque state that resumes one exact native agent session in its original live Sandbox. | not Run resume, transcript replay, or cross-Sandbox restoration |
| **Limit Termination** | The recorded layer, limit, configured value, and cause that stopped an Episode. | not an untyped process exit |
| **GuestServer** (`ale-guestd`) | The in-sandbox service through which execution, file transfer, and observation flow. | never Provider-specific |
| **Gateway** | The host-side service through which API-key-mode solver traffic is controlled, metered, credentialed, and recorded; it also relays Claude subscription traffic without retaining a Transport Trace or provider billing claim. | not used by Codex/Grok native subscription traffic or verification Judges |
| **ATIF Trajectory** | The Harbor ATIF v1.7 document containing the complete ordered agent-visible interaction ALE can observe. | not setup, verification internals, or a native transcript |
| **Transport Trace** | The append-only Gateway record of API-key-mode solver model calls, refusals, accounting, and trajectory links. | not native subscription traffic or full conversation storage |
| **Execution Trace** | The append-only record of framework phases, commands, Task-stage output, policies, and diagnostics. | not agent-owned tool activity |
| **Episode Result** | The sole terminal outcome: status, complete named rewards when verified, stats, bounded failure, phase timing, and timestamps. | not a scalar reward |
| **Blob** | An immutable episode-local content-addressed payload referenced by path, media type, size, and digest. | not ordinary short inline text or a Task artifact |
| **Native Log** | Optional Harness-native evidence retained according to Run policy and used to construct canonical trajectory. | not canonical while unparsed and not a Task artifact |
| **Run Status Projection** | The rebuildable Run-level view of queued, running, phase, terminal, interrupted, rewards, and bounded failure state. | not an Episode evidence store |
| **RunLock** | Schema-2 provenance binding declaration, prepared image, actual Provider observation, Task, data, agent, framework, and effective configuration to a result. | not a qcow2 byte-hash cache |
| **Workspace** | The image-declared agent home. Tasks write their own absolute paths; ALE does not add a hidden prefix. | not a repository or fixed framework directory layout |
