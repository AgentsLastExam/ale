# Autonomous Harness Integration

This is the implementation contract for adding an agent program that owns its own
interaction loop. ALE supplies one instruction, a live sandbox, an episode Gateway
session, validated settings, and resolved resources. The harness launches the program
and returns a typed result.

## Required Files

Every shipped autonomous harness must include:

1. an adapter under `ale.run.harnesses`;
2. a frozen Pydantic settings model with `extra="forbid"`;
3. a complete packaged preset at `ale/run/presets/<harness>.toml`;
4. adapter-specific and common conformance tests;
5. version and integrity reporting;
6. explicit Skill, MCP, and resume validation;
7. episode-local configuration and evidence logs;
8. a deterministic host-side evidence parser;
9. provenance integration; and
10. separately marked live acceptance for model-dependent behavior.

There is no central capability registry. If configured behavior is unsupported, the
adapter must raise a typed error before provisioning. It must never ignore a field or
resource.

## Shipped Integrations

ALE ships these official upstream agent programs:

| Harness | Pinned package | Gateway dialect | Public settings |
|---------|----------------|-----------------|-----------------|
| `claude-code` | Claude Code preset pin | Anthropic Messages | `max_turns`, `max_budget_usd`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `append_system_prompt`, `effort` |
| `grok-build` | `@xai-official/grok@0.2.112` | OpenAI Responses by default; Messages, Chat Completions, or Responses configurable | `max_turns`, `reasoning_effort`, `disabled_tools` |
| `codex-cli` | `@openai/codex@0.146.0` | OpenAI Responses | `reasoning_effort`, `web_search` |
| `openclaw-cli` | `openclaw@2026.7.1` | OpenAI Responses | `provider`, `thinking`, `timeout_seconds`, `tool_profile`, `tools_allow`, `tools_deny`, `model_params` |

The npm integrations use the official packages above. ALE does not install a fork or
carry agent-program patches. The base images pin Node.js 24.15.0, which satisfies the
current packages' runtime requirements.

All four adapters support local Skills, canonical stdio and Streamable HTTP MCP
servers, and same-sandbox native resume at their pinned versions. Support is determined
from the pinned program's verified behavior, not a permanent assumption about a product
name. In particular, `openclaw@2026.7.1` supports MCP; a future pin that changes this
must update validation and acceptance together.

## Settings

The preset lists every public setting, even when its value is a sentinel.

- `default` means omit the native option and use the pinned CLI's own default behavior.
  It never means that ALE substitutes a number.
- `unlimited` means no ceiling. The adapter must map it to a documented native no-limit
  representation or reject it.
- Positive numeric limits are concrete native limits.
- `0`, negative values such as `-1`, unknown sentinels, unknown keys, and unsupported
  native options are invalid.

Accepted settings require a total translation: every field either changes native
configuration or has a documented sentinel behavior. Unvalidated `**kwargs` forwarding
is forbidden.

## Lifecycle

### Construct

Construction receives only definition-level values such as requested version and
validated settings. It must not inspect ambient agent homes, allocate episode paths,
generate session IDs, retain a sandbox, access the network, or mutate process-global
state.

One adapter instance may serve concurrent episodes. Any value containing an episode ID,
sandbox ID, home directory, token, config path, log path, observed version, or native
session ID belongs in method-local or session state, never on the adapter instance.

### Validate

`validate_resources()` runs before provider creation. The base implementation rejects
all optional resources. An adapter that supports resources must explicitly validate:

- Skills;
- each accepted MCP transport and field;
- built-in MCP names;
- harness-native restrictions;
- native resume requests.

Unsupported combinations raise `AgentUnsupportedError` or a more specific typed
preflight error.

### Prepare

Preparation runs after task setup and before agent egress is sealed:

1. `install()` verifies or installs the pinned program and returns the observed version.
2. `install_resources()` stages only `EffectiveAgentResources`.
3. ALE validates stdio MCP commands and working directories inside the sandbox.

Use only the provider-independent `Sandbox` API. Run measured and staged agent-owned
content as `Identity.AGENT`. Native config, Skills, MCP files, and temporary state must
live under the episode's `HarnessSession.home`.

Never copy ambient `~/.claude`, `~/.codex`, credentials, undeclared Skills, or undeclared
MCP configuration.

### Launch

`launch()` receives one text instruction, the prepared sandbox, `HarnessSession`, and
the agent deadline. It must:

- run the measured program as the agent identity;
- route every model call to `session.gateway_url`;
- authenticate only with the episode bearer `session.token`;
- use `session.model` as the authoritative model;
- emit declared evidence logs under the episode home;
- return `AgentRun`;
- classify known refusals and native limits with typed errors.

The harness does not create, destroy, or reopen egress on a sandbox. It does not bypass
the Gateway or add resources after sealing.

### Resume

Resume is optional and rejected explicitly by default. Native resume, when implemented,
uses the exact `NativeContinuation` and only the new instruction. It requires the same
live sandbox and matching harness, episode, sandbox, model, settings, and resource
fingerprint.

An adapter must never use a "latest session" selector, replay the transcript, resume in
another sandbox, or silently start a new conversation. This release has one scope only:
the original live sandbox.

If the native program can create child sessions, collected evidence must select the
requested root session by exact native ID. A substring match or "first/latest session"
lookup is invalid.

### Collect and Parse

Declare evidence paths in `Harness.logs`, relative to the agent home. ALE collects them
regardless of task artifact policy before releasing the sandbox.

`parse_trajectory()` is pure host-side code. For identical collected bytes it returns an
identical schema-valid ATIF v1.7 trajectory. It preserves valid events before malformed
or truncated input, returns bounded parse issues under `extra.ale`, and never writes
Gateway transport records.

MCP records correlate the native tool-use ID with its tool result and retain the logical
server, tool, request arguments, and success result or error. A selected tool with no
matching result is not recorded as a successful call.

The parser must preserve every native agent-visible call form used by the pinned program,
including discovery, custom, web, MCP, and multi-agent calls. It may flatten a native
orchestration wrapper into its observed child calls when the native log does not emit a
separate wrapper result; that mapping must be deterministic and covered by acceptance.

### Cleanup

`cleanup()` runs after evidence collection and before sandbox release. It must be
idempotent and tolerate partial preparation, launch failure, cancellation, and success.
It removes only harness-owned episode-local temporary state. Sandbox destruction belongs
to the Environment.

## Resources

Task and Run resources form one deterministic union. The harness receives resolved,
deduplicated resources, not host search paths.

- Skills are installed only into the native episode-local discovery directory.
- MCP descriptors are translated into one strict native config.
- `cua-desktop` is an ordinary opt-in Run-level built-in MCP server.
- A harness with no MCP implementation rejects any MCP declaration.
- Remote MCP authentication is not copied from the host.

ALE's `cua-desktop` adapter pins its MCP-visible server metadata, ordered tool schemas,
and successful result semantics to AgentsLastExam commit
`783b93ab969e0298014e4996c86d025302ee21ba`. Its sandbox-local GUI backend may differ;
the MCP client contract may not.

## Limits

Keep ownership explicit:

- Gateway limits own model calls, exact input tokens, output tokens, total tokens, and
  provider cost.
- Harness settings own native controls such as Claude `max_turns` and
  `max_budget_usd`.
- Environment timeouts own setup, agent, and verify wall time.

Do not emulate a Gateway limit in an adapter or hide a native limit under a Gateway
field. Termination records and provenance name the enforcing layer, limit, configured
value, and observed value.

The Gateway supports Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses.
The preset supplies the default dialect and upstream provider endpoint; a harness may
accept a documented compatible override. Harnesses always use an episode-local Gateway
bearer token; provider credentials remain host-side.

For finite token limits, the Gateway uses the dialect's exact input-token endpoint
before forwarding. It accounts for every upstream call made by the agent program,
including CLI-owned discovery or compaction calls. If the CLI disconnects from a
streaming response early, the Gateway still drains the upstream response and commits
usage before releasing the reservation.

OpenAI Chat Completions has no provider-independent exact input-token endpoint. With that
dialect, finite input-token, total-token, and cost limits are rejected when the Gateway
session opens. Finite model-call and output-token limits remain valid. Streaming requests
force `stream_options.include_usage=true`, and legacy `max_tokens` is normalized to
`max_completion_tokens` before forwarding.

When an adapter exposes native reasoning or thinking settings, explicit non-default
values must also declare any native model capability required by the pinned program.
Unsupported model/version combinations must fail visibly; the adapter must not silently
fall back to a different reasoning level.

## Verification

A new adapter is complete only when:

- its current official documentation, release, package name, runtime requirements,
  settings, resource mechanisms, and native resume semantics have been researched;
- the official program accepts the generated native configuration;
- `AutonomousHarnessConformance` checks pass;
- unknown settings and unsupported resources fail before provisioning;
- concurrent episodes have distinct paths, config, logs, and native state;
- every model request uses the Gateway session;
- cleanup is idempotent;
- the evidence parser is deterministic;
- preset, settings, resources, versions, limits, and termination are present in
  provenance;
- every behavior that depends on model discovery or tool choice passes a separately
  marked live test using the pinned agent program and a real model through the Gateway;
- the live trajectory audit agrees across Gateway transport, ATIF trajectory, execution
  trace, native evidence when retained, artifacts, result, and RunLock.

A shipped integration therefore follows this acceptance order:

1. strict settings, preset, native translation, parser, and error unit tests;
2. official CLI configuration validation;
3. shared deterministic conformance and sandbox integration;
4. a real Skill plus MCP task through the Gateway;
5. a real tool-surface smoke task that exercises every safely callable visible tool and
   records unavailable or unsafe tools honestly;
6. direct launch plus two native resumes in the same sandbox;
7. independent LLM audit of transport, ATIF, native evidence, artifacts, verifier, and
   RunLock.

A scripted probe, fake harness, oracle, hand-authored transcript, or agent-authored
receipt may test plumbing but cannot complete that live gate. The tool-surface report is
scored from its explicit tool lists, not trusted summary counters, and every claimed pass
must have an observed call and result in the collected evidence.

For native resume, live acceptance directly calls `launch()` and then `resume()` twice
inside one sandbox. Production multi-segment Environment or CLI orchestration is not
part of this contract.
