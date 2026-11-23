# OpenCode

Pinned to the npm package `@opencode/cli@2.3.2`, OpenCode 2; the older `opencode-ai`
package carries only OpenCode 1. Its postinstall links the platform build from the
matching `@opencode/cli-<platform>` package. Each invocation is
`opencode run --standalone --format json --auto` with the prompt on stdin. `--standalone`
gives the invocation a server of its own on the loopback, which ends with it; without it
the CLI would start, or join, a background service that outlives the invocation. `--auto`
approves every permission OpenCode would ask for. A question the model asks is cancelled
with a note to carry on, and any other request for input is cancelled and fails the
invocation.

An invocation fails when OpenCode prints an `error` event: for a turn that failed or was
interrupted, and for a request for input it cancelled, even when the session records the
turn as succeeded. A retried model call is a `retry` event and fails nothing.

`OPENCODE_CONFIG_DIR` is `.opencode-orbit`, holding the declared Skills, in place of
`~/.config/opencode`. Orbit's configuration, `.opencode-orbit/orbit.json`, reaches OpenCode
as `OPENCODE_CONFIG_CONTENT`, which outranks a workspace's own `opencode.json` and
`.opencode`. It defines one custom provider, `orbit`, the episode Gateway, whose key
OpenCode reads from `ORBIT_GATEWAY_TOKEN` and stores nowhere, and a policy that makes every
other provider unusable, OpenCode's keyless hosted models included. Sessions, logs and
snapshots stay where OpenCode keeps them, under `~/.local/share/opencode`.

The Gateway dialect selects OpenCode's package for a compatible endpoint: Chat Completions
(`openai-compatible`, the default), Responses (`openai-compatible/responses`), Anthropic
Messages (`anthropic-compatible`) or Gemini (`google`, through `/v1beta`). The model is
`orbit/<model>`, and `orbit/<model>#<level>` with a `reasoning_effort`; a model whose name
contains `#`, OpenCode's variant separator, fails before the agent starts.

At start OpenCode would refresh its model catalog, check for updates and probe the
loopback for Ollama (port 11434), LM Studio (1234) and vLLM (8000), which may be a Task's
own services; the catalog and updater are off, the three discovery plugins disabled, and
sharing a session is disabled. OpenCode downloads ripgrep 15.1.0 on its first search; Orbit
installs that release into the `bin` directory `opencode debug paths` names, before the
network is sealed, accepting only the archive digests OpenCode itself pins and each
`rg`'s own.

`--session` continues the session with that ID or creates it, so Orbit chooses a new
session's ID, `ses_` and a UUID, as OpenCode requires the `ses` prefix. Each invocation
runs through `.opencode-orbit/invoke.sh`, which Orbit writes, since a Windows desktop
agent's command line may not exceed 1,024 characters. OpenCode keeps sessions in SQLite
only, so after every invocation the script has `opencode session export` write the session,
and every session it spawned at any depth, to
`.opencode-orbit/exports/<root session id>/<time>/<session id>.json`. The parser reads the
newest export, uses an invocation's JSON events only for a turn the export lacks, and adds
an invocation's error to a turn whose session records no failure. A `subagent` call's
metadata names the child session, whose export names its parent; the child becomes a
subagent trajectory embedded in its spawner's, any other session in the export gets an
`unlinked_subagent` issue, and another root's exports are named in `parse_issues`. A
synthetic message, such as the notice of a finished subagent, is a system step.

OpenCode titles a new session with a model request of its own. When an invocation ends
before that request completes, the Gateway records it as a failed call and the next
invocation makes it again.

MCP servers are written with `codemode: false`, so their tools are on the model's tool
list as `<server>_<tool>`, with each name's characters outside `[A-Za-z0-9_-]` as `_`; the
parser restores the declared server name from `orbit.json` and keeps the tool's name as
OpenCode spells it. Under OpenCode's default Code Mode a script calls them through
`execute`, and the session keeps no result of each call. A remote server is Streamable
HTTP, OpenCode's only remote transport, with `oauth: false` so a 401 starts no sign-in.
Two servers whose names OpenCode spells alike, such as `a.b` and `a_b`, are refused, and
so is a server value containing `{env:` or `{file:`, which OpenCode would substitute.

The settings are `reasoning_effort`, `context_window`, `max_output_tokens`, `input`,
`max_steps` and `web_search`. A `reasoning_effort` declares that one level as the model's
reasoning capability and selects the variant OpenCode builds from it, such as
`reasoning_effort` for Chat Completions or a thinking budget for an older Claude model;
a level OpenCode builds no variant of for the model fails the invocation.
`context_window`, `max_output_tokens` and `input` are the model's `limit.context`,
`limit.output` and `capabilities.input`; unset, OpenCode assumes 200,000, asks for 32,000
and sends images. `max_steps` is the `build` agent's `steps`, after which OpenCode asks for
a final answer without tools.

OpenCode has no provider-side search. Its `websearch` tool calls a search service from the
Sandbox, under the Task's network policy, and on its first use asks the user to pick that
service, which fails a headless invocation. So `web_search = "off"`, the default, removes
the tool; `exa`, `firecrawl`, `parallel`, `tavily` or `tinyfish` selects that service,
with its key in the Task variable `EXA_API_KEY`, `FIRECRAWL_API_KEY`, `PARALLEL_API_KEY`,
`TAVILY_API_KEY` or `TINYFISH_API_KEY` (TinyFish works without one); `random` keeps to
one it can use until that one is rate limited. `webfetch` and the `fetch` of
Code Mode, which `execute` still offers for the built-in tools, also fetch from the
Sandbox.
