# Autonomous Harness Integration

This is the implementation contract for adding an agent program that owns its own
interaction loop. ALE supplies one instruction, a live Sandbox, a resolved authentication
mode, validated settings, and resolved resources. The Harness launches the program in
that Sandbox and returns a typed result.

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

Native CLI integrations subclass `CliHarness` in a package containing `adapter.py`,
`settings.py`, `trajectory.py`, and `README.md`. The base owns everything that is the same
for every CLI: settings validation, version and integrity, installation, prompt
delivery, the evidence layout, failure classification, and continuation checks. An
adapter supplies only native knowledge: its configuration (`install_resources()`), its
command line (`prepare_command()`), how to read one invocation's stdout
(`read_result()`), which files belong to a session (`is_session_file()`), and its pure
parser. Other autonomous Harnesses subclass `AutonomousHarness` directly.

There is no central capability registry. If configured behavior is unsupported, the
adapter must raise a typed error before provisioning. It must never ignore a field or
resource.

## Shipped Integrations

ALE integrates these official upstream agent programs:

| Harness | Pinned package | Gateway dialect | Public settings |
|---------|----------------|-----------------|-----------------|
| `claude-code` | `@anthropic-ai/claude-code@2.1.227` | Anthropic Messages | `max_turns`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `append_system_prompt`, `effort` |
| `grok-build` | `@xai-official/grok@0.2.112` | OpenAI Responses by default; Messages, Chat Completions, or Responses configurable | `max_turns`, `reasoning_effort`, `disabled_tools` |
| `codex-cli` | `@openai/codex@0.146.0` | OpenAI Responses | `reasoning_effort`, `web_search` |
| `openclaw-cli` | `openclaw@2026.7.1` | OpenAI Responses | `provider`, `thinking`, `timeout_seconds`, `tool_profile`, `tools_allow`, `tools_deny`, `model_params` |

Each integration installs its program's official distribution; ALE does not install a
fork or carry agent-program patches. That is the npm package when upstream publishes
one, as for the four above. Otherwise it is the official release executable, pinned per
OS and architecture by URL and SHA-256 in the adapter's `release_assets`; when upstream
ships it inside a tar or zip archive, only that one member is taken, pinned by its own
SHA-256 too. The Sandbox's own Python downloads it as the agent and installs it only if
every digest matches, so an unverifiable build never runs. Its integrity is the digest of that whole table. A program
upstream distributes only through its own installer is installed by its adapter from one
pinned artifact: a source installer at one commit, reported as
`git:<repository>@<commit>`, or an installer package it verifies and unpacks, reported as
the `sha256:` of the files it runs. Bases provide runtimes, not agent CLI packages; they
pin Node.js 24.15.0. Each run installs and configures the selected CLI before applying the
Task network policy.

`agent.version` selects an exact version, and omitting it uses the adapter's pinned
default. A release-binary adapter accepts only its pinned version, because only that
version's digests are known. An already matching installation may be reused.

Harnesses declare `supported_os` and validate it before Sandbox creation; the CLI also
checks it before image preparation. The default is Linux only. An npm CLI adapter
supports Linux and Windows. A release-binary adapter supports exactly the systems its
table covers, and its README names any system upstream ships no build for. Unsupported
systems raise `AgentUnsupportedError` before installation; an architecture with no
pinned asset raises it during installation.

On Windows, the base also supplies Git for Windows at `C:/Program Files/Git` and
puts its `bin` and `usr/bin` directories on `PATH`. Harnesses use Git Bash to launch
the native Windows CLI packages; WSL is not required. npm installs and release
binaries stay under the agent's `.local` directory, and the inherited Windows tool
search path is preserved.
The built-in `cua-desktop` uses the OS's system Python. Task-provided Skills and MCP
commands must themselves work on Windows.

All four adapters support local Skills, canonical stdio and Streamable HTTP MCP
servers, and same-sandbox native resume at their pinned versions. Support is determined
from the pinned program's verified behavior, not a permanent assumption about a product
name. In particular, `openclaw@2026.7.1` supports MCP; a future pin that changes this
must update validation and acceptance together.

Claude Code, Codex CLI, and Grok Build support `agent.authentication = "auto" |
"api-key" | "subscription"`. Subscription state is isolated from ordinary Host agent
homes: Claude reads `ALE_CLAUDE_CODE_OAUTH_TOKEN` from the ALE checkout's `.env`; Codex
and Grok use `<checkout>/.ale/auth/<harness>/auth.json`. Credentials remain on the Host;
every Sandbox receives only its episode Gateway token. OpenClaw and policy Harnesses
reject explicit subscription authentication.

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

Never copy ambient agent homes, undeclared Skills, undeclared MCP configuration, or a
provider subscription profile into the episode home.

### Launch

`launch()` receives one text instruction, the prepared sandbox, `HarnessSession`, and
the agent deadline. It must:

- run the measured program as the agent identity;
- in every authentication mode, route every model call to `session.gateway_url` and
  authenticate only with the episode bearer `session.token`;
- use `session.model` as the authoritative model;
- emit declared evidence logs under the episode home;
- return `AgentRun`;
- classify known refusals and native limits with typed errors.

`CliHarness` writes each prompt as UTF-8 to `.ale-harness/prompt.md` and the CLI reads
that file through stdin or its native prompt-file option, so the prompt never passes
through argv, an environment variable, or shell quoting and reaches the model exactly as
sent. Every invocation, launch or resume, appends to `.ale-harness/stdout.log` and
`.ale-harness/stderr.log` and records its prompt, requested session ID, and stream offsets
in `.ale-harness/invocations.jsonl`. Result interpretation sees only the current
invocation's output, so it cannot reuse the success or session ID of an earlier turn.

The CLI starts from a non-login shell, POSIX `sh` on Linux and Git Bash on Windows, with
the Sandbox's own `PATH` behind the agent's npm prefix. Profile scripts are not sourced,
so the measured program's environment is the image's declared one on both systems.

A failed launch retains the process exit code and a bounded, credential-redacted diagnostic
from native error events and stderr. Stderr warnings must not displace the native failure
reason; ordinary assistant messages and tool output are not failure diagnostics. A terminal
native error is a failure even when the process exits zero. Recovered transport errors and
individual tool failures do not invalidate a subsequently successful native turn.

The Harness does not create, destroy, or reopen egress on a Sandbox or add resources
after sealing. API-key and subscription modes use the same metered Gateway path. The
Host selects API-key or subscription upstream authentication, while model authority,
limits, retry coalescing, and Transport Trace stay common.

### Resume

Resume is optional and rejected explicitly by default. Native resume, when implemented,
uses the exact `NativeContinuation`. An instruction becomes the session's next user
message; `None` asks the agent to carry on without new task input. No shipped CLI can
continue a headless session without a message, so `CliHarness` then sends the fixed
`Continue from where you left off.`, recorded like any other prompt. Resume requires the
same live sandbox, a session file the adapter recognizes by its exact ID, and matching
harness, episode, sandbox, model, settings, and resource fingerprint.

An adapter must never use a "latest session" selector, replay the transcript, resume in
another sandbox, or silently start a new conversation. This release has one scope only:
the original live sandbox.

If the native program can create child sessions, collected evidence must select the
requested root session by exact native ID. A substring match or "first/latest session"
lookup is invalid.

### Collect and Parse

`Harness.logs` declares, relative to the agent home, the files and directories that
hold a Harness's own record of a run. A CLI declares `.ale-harness` and its native
session directories, including child-session locations. The Environment mirrors them
(see [Standard Environment](standard-environment.md#evidence-capture)) and the parser
reads the mirror at the same relative paths; it never sees the Sandbox.

`parse_trajectory()` is pure host-side code. For identical collected bytes it returns an
identical schema-valid ATIF v1.8 trajectory. It preserves valid events before malformed
or truncated input, returns bounded parse issues under `extra.ale`, and never writes
Gateway transport records. A parser that raises does not decide the episode: ALE records
a minimal truthful trajectory, the instruction and final message, with
`incomplete_reason: conversion_failed` and a `parse_issues` entry, keeps the native logs
regardless of retention policy, and continues to artifact capture and verification.

A CLI trajectory is the native session's conversation in native order. Stdout adds only
what the session lacks, such as terminal results and failures, at the point it occurred.
When the session is unavailable, each turn is rebuilt from its recorded prompt and its
own stdout, and a `parse_issues` entry says so; nothing is invented. Mirrored session
files the parser does not use are named in `parse_issues` and so survive minimal
retention. ALE converts once, after the final log copy, and then adds transport links,
exact token enrichment, and retention.

A spawned child session becomes a subagent trajectory (see [Trace](trace.md)) only when
the native evidence identifies the call that spawned it. Otherwise it stays raw evidence
with an `unlinked_subagent` issue, because a guessed link would misstate who delegated
what.

MCP records correlate the native tool-use ID with its tool result and retain the logical
server, tool, request arguments, and success result or error. A selected tool with no
matching result is not recorded as a successful call.

The parser must preserve every native agent-visible call form used by the pinned program,
including discovery, custom, web, MCP, and multi-agent calls. It may flatten a native
orchestration wrapper into its observed child calls when the native log does not emit a
separate wrapper result; that mapping must be deterministic and covered by acceptance.

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

- Gateway limits own model calls, provider-reported input tokens, output tokens, and
  total tokens in both API-key and subscription modes.
- Harness settings own native controls such as Claude `max_turns`.
- Environment timeouts own setup, agent, and verify wall time.

Do not emulate a Gateway limit in an adapter or hide a native limit under a Gateway
field. Termination records and provenance name the enforcing layer, limit, configured
value, and observed value.

Adapters do not parse Gateway refusals. The Gateway session records the first limit that
refused a call, and the Environment attributes an agent phase that then fails, whatever
error the agent program reported, to that limit: the episode ends `budget_exceeded` with a
`gateway` termination. Refusals carry `x-should-retry: false` so SDKs stop retrying them.
Adapters classify only their native limits.

The Gateway supports Anthropic Messages, OpenAI Chat Completions, OpenAI Responses, and
Gemini `generateContent` for API-key mode; each dialect's routes and request handling
are in the [Gateway README](../../packages/ale-run/src/ale/run/gateway/README.md). The
preset names the default dialect and its model provider (`gateway.provider`), whose
`ALE_<PROVIDER>_API_KEY` and `ALE_<PROVIDER>_BASE_URL` select the key and upstream (see
[Security](security.md#3-provider-credentials-stay-on-the-host)); a Harness may accept a
documented compatible dialect override. Subscription mode uses the
same Sandbox-side Gateway configuration; only the Host Gateway's upstream authentication
changes, so model authority, limits,
accounting, retry coalescing, and Transport Trace are common to both modes. The ChatGPT
Codex backend rejects `max_output_tokens`, so ALE accounts the completed response and
refuses the next call once an output ceiling is reached.

The Gateway does not pre-count input tokens or reserve worst-case token budgets.
It completes the current request, accounts provider-reported usage, and refuses the next
request after an input-token, output-token, or total-token ceiling is reached.
Input tokens include prompt-cache reads and writes, so a cache-heavy agent cannot stay
under an input or total ceiling by caching.
Model-call limits remain exact because they require no estimation. This policy applies
equally to every dialect.

It accounts for every upstream call made by the agent program, including CLI-owned
discovery or compaction calls. If the CLI disconnects from a streaming response early,
the Gateway still drains the upstream response and commits usage. Beyond imposing the
model, a request is changed only where its dialect requires it, for example so usage is
reported; the Gateway README lists each change. A
request without an output ceiling is forwarded without one; the Gateway never invents a
default that would truncate reasoning or output.

A streaming response can stay silent for minutes when the provider buffers part of it (a
`tool_use` input is held back until the whole parameter is generated). After 15 s of upstream
silence the Gateway writes an SSE comment line to the client; comment lines are ignored by
event-stream parsers, so the events, the accounted usage and any replayed body are unchanged.
A dialect whose clients reject comment lines gets none.
The Claude Code adapter also raises the CLI's HTTP idle timeout and stream watchdog to
600 s, well past the quiet spans this has caused, so the keep-alive is not the only thing
standing between the CLI and a response the Gateway is still receiving.

When an adapter exposes native reasoning or thinking settings, explicit non-default
values must also declare any native model capability required by the pinned program.
Unsupported model/version combinations must fail visibly; the adapter must not silently
fall back to a different reasoning level.

## Verification

A new adapter is complete only when:

- its current official documentation, release, package name, runtime requirements,
  settings, resource mechanisms, and native resume semantics have been researched;
- the official program accepts the generated native configuration;
- `AutonomousHarnessConformance` checks pass, and a `CliHarness` adapter also passes the
  shared CLI suite in `tests/conformance/test_shipped_autonomous_harnesses.py`, which
  discovers every shipped adapter;
- unknown settings and unsupported resources fail before provisioning;
- concurrent episodes have distinct paths, config, logs, and native state;
- every API-key and subscription model request uses the Gateway session;
- the evidence parser is deterministic;
- preset, settings, resources, versions, limits, and termination are present in
  provenance;
- every behavior that depends on model discovery or tool choice passes a separately
  marked live test using the pinned agent program, a real model, and the selected
  authentication path;
- the live trajectory audit agrees across available transport evidence, ATIF trajectory,
  execution trace, native evidence when retained, artifacts, result, and RunLock.

A shipped integration therefore follows this acceptance order:

1. strict settings, preset, native translation, parser, and error unit tests;
2. official CLI configuration validation;
3. shared deterministic conformance and sandbox integration;
4. a real Skill plus MCP task through the selected authentication path;
5. a real tool-surface smoke task that exercises every safely callable visible tool and
   records unavailable or unsafe tools honestly;
6. direct launch plus two native resumes in the same sandbox;
7. independent LLM audit of available transport evidence, ATIF, native evidence,
   artifacts, verifier, and RunLock.

A scripted probe, fake harness, oracle, hand-authored transcript, or agent-authored
receipt may test plumbing but cannot complete that live gate. The tool-surface report is
scored from its explicit tool lists, not trusted summary counters, and every claimed pass
must have an observed call and result in the collected evidence.

For native resume, live acceptance directly calls `launch()` and then `resume()` twice
inside one sandbox. Production multi-segment Environment or CLI orchestration is not
part of this contract.
