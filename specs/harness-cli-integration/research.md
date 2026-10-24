# Harness CLI integration research

Research date: 2026-10-23. This is a historical baseline audit, not a replacement for
the living specifications. Findings describe the inspected commits before this
feature's implementation; they do not describe the current worktree.

The implemented design is [Shared CLI Harness and periodic evidence collection](design.md).
It supersedes the original broad audit roadmap. Findings below remain an inventory
of inspected behavior, not a requirement to fix every item before framework work.

## Recommendation

Keep ALE's current `AutonomousHarness`, Sandbox, Gateway, and ATIF boundaries.
Add a small runtime `CliHarness` that shares execution, separate stdout/stderr,
and continuation checks. Require native-session source declarations, and use one
shared collector to retrieve evidence and update trajectory during execution.
Keep native settings and conversion in each CLI's own small package.

Do not pursue a capability registry, expanded qualification framework, software
content checksums, or cross-Sandbox session recovery. Existing same-live-Sandbox
resume and Sandbox retention already cover the continuation scope at issue.
Broader bug fixes and additional CLIs follow the framework discussion separately.

## Baselines and scope

| Repository | Inspected revision | Action |
|---|---|---|
| ALE | `95fc769e456fffbc7e26bf7414993cb7657d7944` | Fetched `origin`; created and bootstrapped `feat/harness-cli-integration` at exactly `origin/main` |
| Agent's Last Exam | `1237bd7eb0703a4996598c9bce8ca74b28a8a0d8` | Pulled `origin/main` with fast-forward only |
| Harbor | `bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0` | Fetched and fast-forwarded local `main`; used HTTPS after SSH authentication failed |

Worktree: `/home/weichen/ale/ale-worktrees/feat/harness-cli-integration`.
It owns its `.venv`. The ALE primary checkout has two local commits beyond the
remote baseline; they were deliberately not carried into this feature branch.
Bootstrap and doctor completed with zero doctor failures. Doctor warns about
the nested `feat/` worktree path and the absent local `.env`; no credentials were
copied or inspected.

The audit covers all four ALE autonomous adapters, their public contracts,
configuration/composition, evidence collection, provenance, and relevant tests.
The reference comparison covers shared infrastructure and selected adapters,
especially Qwen, Gemini, Kimi, Pi, and ACP. It is not a runtime certification of
every adapter in either reference repository.

Repository inventory counts are not equivalent capability counts:

- ALE ships **four** autonomous CLIs: Claude Code, Codex CLI, Grok Build, and
  OpenClaw CLI. `computer-use` is a policy Harness; `oracle` and `nop` are controls.
- Agent's Last Exam has **21** shortcut registrations, including `dummy` and
  non-CLI agents.
- Harbor has **46** factory registrations, including controls, SDK agents,
  external-loop agents, and generic ACP. This does not mean 46 fully qualified
  native CLIs with identical features.

## Existing ALE standard

The normative starting points are [Autonomous Harness Integration](../../docs/specs/autonomous-harness.md),
[Unified episode logging](../../docs/specs/trace.md), and the
[Harness contract](../../packages/ale-core/src/ale/core/harness.py).

| Area | Current requirement | Acceptance implication |
|---|---|---|
| Public interface | `AutonomousHarness`; definition-only construction; episode data in `HarnessSession` or method locals | One instance must safely serve concurrent episodes |
| Native implementation | Official upstream program, exact default version, observed installed version, integrity identity | No silent fork or automatic version drift |
| Configuration | Frozen Pydantic settings with `extra="forbid"`; complete packaged preset | Every accepted field must translate or have documented sentinel behavior |
| Limits | Distinguish `default`, `unlimited`, and positive values | Native limits, Gateway budgets, and Environment deadlines have different owners |
| Preflight | Validate OS, Skills, MCP, authentication, and resume support | Unsupported combinations fail before expensive provisioning |
| Sandbox | Use the public Sandbox contract and `Identity.AGENT` | No direct Docker/QEMU dependencies in adapters |
| Model transport | Use `session.gateway_url`, `session.token`, and `session.model` | Real API keys and subscription credentials stay on the Host |
| Configuration isolation | Native home, config, Skills, MCP, and logs stay under the episode home | No ambient operator agent home or undeclared resources |
| Resources | Consume `EffectiveAgentResources`, a resolved deterministic union | Preserve resource digests, origins, native discovery paths, and MCP semantics |
| Execution | Return typed `AgentRun`; inspect native terminal errors as well as exit code | Exit zero is not sufficient proof of successful completion |
| Resume | Optional; exact native session, original live Sandbox, matching fingerprint | Never use latest-session lookup, transcript replay, or a silent fresh start |
| Evidence | Declare native logs independently of task artifacts; pure host-side parser | Preserve valid partial evidence, report conversion issues, retain failed conversion input |
| Canonical records | ATIF trajectory, transport trace, execution trace, result, verification, RunLock | Conversation, accounting, infrastructure, scoring, and provenance remain separate |
| Qualification | Unit, conformance, real Sandbox, official CLI configuration, and marked live tests | A fake agent or self-authored receipt does not establish native tool support |

The current shipped matrix is:

| Harness | Default pin at this ALE commit | Gateway dialect | Declared OS | Native resume | Subscription |
|---|---|---|---|---|---|
| Claude Code | `@anthropic-ai/claude-code@2.1.227` | Anthropic Messages | Linux, Windows | Yes | Yes |
| Codex CLI | `@openai/codex@0.146.0` | OpenAI Responses | Linux, Windows | Yes | Yes |
| Grok Build | `@xai-official/grok@0.2.112` | Responses default; Messages and Chat Completions configurable | Linux, Windows | Yes | Yes |
| OpenClaw CLI | `openclaw@2026.7.1` | OpenAI Responses | Linux, Windows | Yes | No |

All four declare Skills, stdio MCP, and Streamable HTTP MCP support. These are
repository declarations and existing acceptance paths, not new live results from
this audit. Resume is an adapter capability; production multi-segment Environment
or CLI orchestration is explicitly outside the current contract.

## Trajectory capabilities and gaps

ALE already has the right shared destination: strict local **Harbor ATIF v1.7**
models and `TrajectoryBuilder`. The schema supports messages, exposed reasoning,
tool calls/results, image references, subagent trees, continuation references,
and observed training metadata. `BlobStore` externalizes payloads; the Gateway
owns transport accounting; correlation uses observed identities. Parser failure
has a truthful minimal fallback and should not itself decide the task score.

Schema support is not proof that every parser preserves each feature:

| Evidence | Current adapter behavior |
|---|---|
| Claude Code | Richest conversion: messages, reasoning, tool results, images/attachments, native IDs, and sidechain embedding when such events are present |
| Codex CLI | Combines stdout and exported root session for custom, discovery, web, MCP, and multi-agent call forms; message ordering and MCP matching have defects described below |
| Grok Build | Uses chat history, stream output, and updates to unwrap tools and identify errors; malformed lines are silently ignored |
| OpenClaw CLI | Converts native message/tool-result records, reasoning, images, and usage; missing cost can become zero |
| Subagents generally | Shared schema exists; Codex call names such as `multi_agent.*` do not by themselves provide child conversations; Grok/OpenClaw lack equivalent child embedding here |
| Transport links | Claude records native IDs; the other three lack equivalent `native_event_ids` emission. Correlation cannot be assumed when upstream/native IDs are unavailable |

A valid one-message ATIF document can still omit most of an execution. The current
proposal addresses source collection and parser ownership directly; it does not
introduce a new capability or evidence-certification system.

## Findings, ordered by impact

### P1: Codex drops conversation content when native tool evidence exists

In [codex_cli.py](../../packages/ale-run/src/ale/run/harnesses/codex_cli.py),
`parse_trajectory()` suppresses stdout messages and reasoning whenever
`native_calls` is nonempty (lines 367 and 370). It then appends all native calls
and only the last message (line 485). `_session_tool_calls()` does not restore
native user or assistant message events.

An offline probe containing an intermediate message, exposed reasoning, one native
tool call, a second user instruction, and a final answer produced only:

```text
user: first request
agent: tool call + result
agent: final answer
```

The intermediate message, reasoning, and second instruction were absent. This
also makes resumed conversations incomplete even when native resume itself works.
Merge evidence by native event identity and order; enrich one ordered conversation
rather than switching the entire parser to a tool-only representation.

### P1: Codex can attach another MCP server's result to a call

`_session_tool_calls()` matches stdout MCP records by tool name and arguments
(line 759), without checking server identity or a shared call identity.
Two servers exposing `lookup({})` with opposite completion order reproduced:

```text
native call-a on server_a -> canonical server_b, B result
native call-b on server_b -> canonical server_a, A result
```

This changes recorded behavior. Require exact IDs where available; otherwise
use the full server/tool/arguments identity only when unambiguous. Ambiguity must
be reported and retain source evidence instead of guessing.

### P1: Damaged evidence can be silently lost, then deleted

[Grok's `_jsonl()`](../../packages/ale-run/src/ale/run/harnesses/grok_build.py)
(line 573) skips JSON decode errors. `_parse_extra()` reports no events only when
both chat and stream are empty. A valid prefix followed by a malformed line
therefore produces no parse issue.

[StandardEnvironment](../../packages/ale-run/src/ale/run/environments/standard.py)
deletes native logs in minimal mode when the parser returned and `agent_run`
exists (line 806). It does not inspect parse issues or completeness. The combined
probe confirmed that the malformed Grok file was deleted after conversion.
Other parsers that return issues can also have their raw logs removed on this path.

Separate successful parsing from complete conversion. Preserve raw evidence for
missing, malformed, ambiguous, unsupported, or otherwise incomplete conversion.
Bound both issue detail length and issue count. An OpenClaw probe with 1,000 bad
lines returned 1,000 issues; the written bounded-issues requirement is not enforced.

### P1: Cleanup runs before collection and can prevent all evidence capture

The written lifecycle says collection precedes cleanup. In
`_capture_solver_evidence()` (line 646), `cleanup()` executes first, and an error
is re-raised before the collection loop. A fake cleanup failure reproduced zero
calls to `artifacts.collect_file`.

The four current autonomous adapters inherit no-op cleanup, so this is a latent
integration-contract trap rather than evidence that their ordinary runs currently
fail cleanup. A new adapter following the written cleanup contract can trigger it.
Collect and persist evidence first; record cleanup failure independently and still
complete the remaining teardown work.

### P1: Rich native evidence export depends on successful process completion

Codex copies its root `session.jsonl` only after checking process/native success
(line 286). Grok exports history only on success and writes active stdout to
`.grok-ale/segment.jsonl`, a path absent from `Harness.logs`. OpenClaw copies its
native transcript in the shell after the CLI exits. Killing the process group can
prevent post-command copying or concatenation.

These are control-flow findings, not live timeout reproductions. The failure mode
is that useful native state exists in the Sandbox but the declared collection
surface does not reach it. The current design replaces post-success exports with
direct periodic collection of declared native session roots, independently of a
successful `AgentRun`. Verify interruption behavior using the existing Sandbox tests.

### P2: The common conformance gate is much weaker than the written standard

[AutonomousHarnessConformance](../../packages/ale-core/src/ale/core/testkit/harness.py)
checks truthy name/version/integrity, safe log paths, empty resources, session
fields, and equality of two parser results. `integrity="unpinned"` passes the
truthiness check. The shipped common test parses an empty directory and does not
exercise its cleanup or continuation helpers.

There are valuable adapter unit and live tests elsewhere; they are not absent.
However, a new adapter can pass this reusable gate while losing messages or
silently dropping data. This remains an audit observation. The current scope uses
existing tests plus focused cases for changed execution/collection behavior; it
does not establish a broader conformance or qualification framework.

### P2: Version identity and limit metadata need stronger ownership

`AgentConfig.version` and constructors accept `latest`; a probe returned
`npm:@openai/codex@latest` as integrity. Installation subsequently compares the
observed version and should reject a mismatch, but this is too late for the
preflight contract. Validate exact version syntax before provisioning.

Current integrity strings are package coordinates, not verified content hashes;
Claude's string even omits the package name. This is descriptive, not a proposal
to add content verification. Existing version pinning remains; checksum work is
outside the revised scope.

`agent_provenance()` recognizes native limits with a fixed
`{"max_turns", "max_budget_usd"}` allowlist. OpenClaw's timeout is present in
settings but not classified there. New native limits should not require adding
another agent-specific branch to provenance. Bind classification to validated
adapter settings or a small adapter-owned metadata method.

### P2: Accounting availability is inconsistent with the trace contract

OpenClaw initializes cost to `0.0` and writes that total whenever any usage counter
is nonzero. A native message with input/output usage and no cost produced
`total_cost_usd=0.0`. Unknown cost must stay unknown.

Codex uses the last `turn.completed` usage value for final metrics; resumed
multi-turn aggregation needs an explicit native-semantics test. More broadly,
the trace specification assigns aggregate accounting to transport, while Codex
and OpenClaw write native totals to ATIF. Decide whether those are optional native
projections with explicit provenance or remove the duplicate accounting surface.
Keep Gateway accounting authoritative and never treat missing evidence as zero.

### P2: Harbor schema parity depends on an unpinned adjacent checkout

Harbor now defaults to **ATIF v1.8**, including audio. ALE still targets v1.7.
The existing tools/images/subagents/continuation acceptance test passes against
the updated Harbor models. The exact schema-fixture equality test fails.

This is schema drift, not proof that ALE v1.7 exports are unusable. Pin the schema
source revision and distinguish pinned-schema checks from compatibility tests
against current Harbor. Adopt v1.8 deliberately if typed audio is needed; do not
regenerate the fixture without updating ALE models and contract tests together.

### P2: Adapter ownership is clear at package level, crowded at file level

The four adapter files contain 1,185 / 834 / 723 / 675 lines respectively. They mix
settings, installation, config rendering, command construction, native continuation,
error handling, log readers, and semantic conversion. Size alone is not a defect;
the observed converter bugs show why independently testable parsing matters.

Repeated mechanics include Skill upload/chmod, JSONL loading, parse-issue handling,
continuation checks, and package installation. Native config and event semantics
remain different and should stay local. CLI construction currently uses a manual
match in `cli/tasks.py`; names also appear in help and manually parameterized tests.
There is no equivalent of Harbor's custom import-path integration route.

## Lessons from Agent's Last Exam

Useful principles:

1. Its deployer lifecycle separates execution from host-side `parse_artifacts()`.
   ALE already preserves this principle and should retain it.
2. Adapter-local directories colocate config, deployer, documentation, and optional
   trajectory helpers. This is a better model for growing ALE adapters than another
   monolithic file or one generic converter.
3. `hot_artifacts` and executor-side incremental pulling preserve progress while a
   remote run is still active. Move the principle into ALE's Sandbox abstraction,
   rather than copying executor-specific transport code.
4. The Qwen integration enriches stdout from native chats and local telemetry,
   distinguishes background children with incomplete output, and reports tool/result
   completeness. This directly addresses ALE's current conversion blind spots.
5. Qwen documents a real, unmodified CLI test against a local model endpoint and
   stdio MCP fixture. This is reference context only; no equivalent intermediate
   testing tier is proposed for this work.

The actual periodic implementation is `tail_hot_artifacts`, using byte offsets
and final catch-up. It currently polls at 60 seconds and uses a static file list.
Trajectory conversion still happens at the end; periodic conversion in ALE is a
new proposal, not a feature already supplied by that reference.

Patterns not to copy:

- `factory.build_config()` silently filters unknown keys. ALE's fail-closed settings
  are better for reproducible evaluation.
- The shared trajectory is `ALE-v1.0`, with separate environment steps and defaults
  such as parse-time timestamps and generated call IDs. It is not a drop-in ATIF
  converter and does not guarantee deterministic preservation of observed identity.
- Some older adapters use forks, ambient environment changes, direct credentials,
  or free-form extra arguments. For example, its Gemini config points to a
  `cua-verse/gemini-cli` fork. That conflicts with ALE's official-program and
  Host-Gateway boundaries.
- Native telemetry may enrich conversation evidence, but must not introduce another
  authoritative model-usage ledger alongside ALE's Gateway.

Reference sources:
[deployer contract](https://github.com/rdi-berkeley/agents-last-exam/blob/1237bd7eb0703a4996598c9bce8ca74b28a8a0d8/ale_run/base_interface/agent_deployer.py),
[factory](https://github.com/rdi-berkeley/agents-last-exam/blob/1237bd7eb0703a4996598c9bce8ca74b28a8a0d8/ale_run/orchestration/factory.py),
[Qwen guide](https://github.com/rdi-berkeley/agents-last-exam/blob/1237bd7eb0703a4996598c9bce8ca74b28a8a0d8/ale_run/agents/qwen_code/README.md),
[Qwen evidence enrichment](https://github.com/rdi-berkeley/agents-last-exam/blob/1237bd7eb0703a4996598c9bce8ca74b28a8a0d8/ale_run/agents/qwen_code/trajectory.py).

## Comparison with Harbor

| Dimension | Harbor at the inspected commit | Implication for ALE |
|---|---|---|
| Catalog and extension | 46 factory entries, custom import paths, generic ACP registry integration | Easier to try a new agent without editing core dispatch; ALE is behind here |
| Discoverability | Typed `AgentCapabilities`, option schemas, auth inspection and capability docs | Additional machinery exists there; no corresponding registry or matrix project is proposed here |
| Option translation | `AgentOptions` plus `Annotated` CLI/env bindings | Reuse the single-source principle for simple flags; keep complex native config explicit |
| Installation | Shared system-dependency support; ACP binary/npx/uvx distribution paths | ALE's npm helper covers its current four, not every future installation family |
| Trajectories | ATIF models, per-agent converters, subagent handling, v1.8 audio | Reuse schema and mapping lessons; audit converter assumptions before porting |
| Progress visibility | Optional live session-log sync and conversion, gated by environment/agent support | ALE's end-of-run collection lacks equivalent resilience and visibility |
| Continuation | Native resume, selected ATIF/native load support, handoff, multi-step use | ALE already has native resume and Sandbox retention; extra loading/handoff workflows are not a deficiency in ALE's chosen continuation scope |
| Model connections | Declared API formats and provider auth routing; Gemini native GenerateContent supported | ALE has only three Gateway dialects, so some new CLIs require transport work first |
| Credentials/accounting | Resolved provider credentials can be forwarded into the agent runtime | Does not satisfy ALE's mandatory Host-only credential and metered-Gateway invariant |
| Windows | Inspected documentation lists Windows support for `nop`/`oracle`; ordinary CLI defaults are not Windows-capable | ALE's four explicit Linux/Windows adapters are a useful existing advantage, subject to their qualification |

Harbor also has costs and weaker defaults that ALE should not inherit:

- `BaseInstalledAgent` is 1,295 lines and carries legacy flag descriptors alongside
  the new options model. Claude and Codex adapters exceed 2,000 lines each. It is
  not evidence that a large base class makes integration simple.
- Unspecified installed-agent versions can use latest, and version detection can be
  best effort. ALE should keep stricter reportability requirements.
- Qwen's root-session selection uses the latest event timestamp/mtime, and its JSONL
  reader skips malformed lines. Those exact choices conflict with ALE's current
  evidence-selection and issue-reporting standard.
- ATIF, resume, Skills, MCP, and base-URL support are optional capabilities. A Harbor
  registration does not establish that a program can meet ALE's full contract.

ACP is a serious second path for reducing duplicated integration code, but not a
shortcut around model transport. ACP standardizes client-to-agent interaction;
it does not guarantee that the agent's model requests use ALE's Gateway, that
auxiliary requests obey the chosen model, or that all native reasoning/child
sessions are observable. Evaluate one pinned native ACP-capable program later,
record bridge provenance, and apply the same evidence/authentication gates.

Reference sources:
[factory](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/agents/factory.py),
[capabilities](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/agents/capabilities.py),
[options](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/agents/options.py),
[ACP guide](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/docs-mintlify/agents/acp.mdx),
[live trajectory sync](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/trial/sync_trajectory.py),
[ATIF model](https://github.com/harbor-framework/harbor/blob/bfc6c005e9a572ba22fd7e2d3b414bf014ae1ab0/src/harbor/models/trajectories/trajectory.py).

## Revised structure proposal

For a substantial new adapter, use a small local package:

```text
ale/run/harnesses/qwen_code/
  __init__.py       # public Harness/Settings exports
  adapter.py        # lifecycle, launch, resources, native configuration
  settings.py       # strict supported settings and native limit semantics
  trajectory.py     # ordered native evidence -> ATIF
  README.md         # pinned program contract and known evidence limitations
ale/run/presets/qwen-code.toml
```

Keep native conversion separate from execution and settings. Add one runtime CLI
base and one common collector; do not add a second trajectory format or deep class
hierarchy. The concrete interfaces, native source paths, periodic lifecycle, and
necessary concurrent Sandbox reads are specified in [design.md](design.md).

The earlier proposal for an expanded executable contract, capability matrix, and
qualification layers is withdrawn from this scope. The base should make ordinary
integration consistent by construction, without adding a separate compliance system.

## Delivery order

1. Complete the small base-class and evidence-collection design before expanding
   integrations. This was the initial design stage; implementation followed later.
2. Implement the shared CLI path, concurrent file reads, and periodic trajectory
   updates together with migration of the current four CLI adapters.
3. Revisit specific converter defects and additional CLIs separately, using the new
   package structure. No expanded test framework or capability registry is required.

Deferred candidate notes, based on reference code rather than new upstream release
qualification; these are not the current implementation schedule:

| Candidate | Why it is useful | Main gate |
|---|---|---|
| Qwen Code | Both references have native integration; old repository has especially useful evidence tests | Existing Chat Completions/Responses routing, stdout/native joins, subagents, native MCP |
| Kimi Code | Both references have an official CLI path; Harbor declares Chat Completions/Messages support | Verify exact package/version, native capabilities, isolated config and trajectory completeness |
| OpenCode | Exercises provider-config translation and a mature session interface | Force all selected provider/auxiliary traffic through ALE and preserve exact session identity |
| Pi | Useful multi-provider CLI with structured sessions | Harbor uses a separately pinned `pi-mcp-adapter`; extension provenance and support must be explicit, or MCP must be rejected |
| Hermes | Broadens installation/runtime coverage and has reference adapters | Pin Python/source dependencies and verify model/telemetry/tool configuration |
| Gemini CLI / Antigravity | Important coverage, but not an adapter-only addition | Native Google GenerateContent and/or authentication requires Gateway work; do not copy the old Gemini fork |
| Cursor / Droid / other hosted CLIs | Potentially valuable vendor coverage | First prove endpoint override, Host-only authentication, accounting, and reliable native logs |

These are engineering priorities, not a claim that any candidate is already ALE
compatible. Recheck official releases and documentation at implementation time.

## Verification performed

The following existing ALE selection passed: **127 tests**.

```sh
uv run pytest \
  tests/unit/test_cli_harnesses.py \
  tests/unit/test_claude_code.py \
  tests/unit/test_agent_config.py \
  tests/unit/test_agent_resources.py \
  tests/unit/test_trajectory.py \
  tests/unit/test_trajectory_fallback.py \
  tests/conformance/test_autonomous_harness.py \
  tests/conformance/test_shipped_autonomous_harnesses.py \
  tests/conformance/test_trajectory_parser.py -q
```

Additional temporary, assert-based offline probes confirmed:

- missing Codex intermediate messages, reasoning, and a resumed user message;
- cross-server Codex MCP result misassociation;
- Grok malformed-line suppression and deletion of its raw evidence in minimal mode;
- 1,000 unbounded OpenClaw parse issues for 1,000 malformed lines;
- zero log collection calls after cleanup raises;
- unobserved OpenClaw total cost becoming zero;
- acceptance of `latest` before provisioning.

No probe altered production code or persisted credentials.

The normal Harbor conformance command initially failed before running its checks:
updated Harbor requires uv >=0.13.5, while this host has uv 0.11.31. Without changing
global tooling or repository files, the existing installed environments were used:

```sh
UV_NO_CONFIG=1 UV_NO_SYNC=1 uv run pytest \
  tests/conformance/test_harbor_atif.py -q --tb=short
```

Result: **one passed, one failed**. The rich v1.7 trajectory acceptance test passed;
exact schema equality failed against Harbor's v1.8 model. This checks current
source with already installed dependencies, not a fresh Harbor dependency install.

During this baseline audit, no real-model, Docker/QEMU/Windows acceptance, paid cloud run, or full Harbor/old-ALE
test suite was executed. Failure-path process-group behavior and candidate CLI
qualification were not established by this audit. Subsequent implementation and
validation are recorded in [design.md](design.md) and [validation.md](validation.md).
