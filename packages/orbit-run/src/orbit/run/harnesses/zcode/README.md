# ZCode

Z.ai publishes no CLI package. Its only official distribution is the desktop release, whose
installers carry the CLI as one JavaScript bundle, byte-identical on every platform. Orbit
installs from the Linux `.deb` of release 3.16.3, accepted only by the sha512 the release's
`latest.yml` publishes (pinned in code, never fetched at run time), and keeps just
`resources/glm/zcode.cjs` and `resources/config/provider/zcode-builtin.json`, each accepted
only by its own sha256. It runs on the adapter's Node.js on every system.
The integrity is the digest of those two files with the Node.js version; the CLI reports
version 0.17.0. Its public settings are `disallowed_tools`, `workflow_mode`,
`toolsearch_mode` and `input_format`.

The run is `zcode --mode yolo --output-format stream-json --cwd <home>`, whose launcher runs
`zcode.cjs` on that Node.js. Headless ZCode takes the prompt only as an argument, so it
receives the recorded prompt file's exact bytes as one, with Git Bash path conversion off
on Windows. The prompt is then bounded by
128 KiB per argument on Linux, 1 MiB for the whole command line and environment on macOS,
and about 32,000 characters for the whole Windows command line; a longer one fails at launch
instead of being truncated. The model route is a personal provider file named by
`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`. ZCode reads only a literal API key there, so the file
holds the episode token, readable by the agent account alone and never mirrored. Each
Gateway dialect selects ZCode's `anthropic-messages`, `openai-chat-completions` or
`openai-responses` API. The preset speaks `anthropic` to the `zai` provider, Z.ai's own
Anthropic endpoint, so it uses the Z.ai key and never the Anthropic one. MCP servers go to
`.zcode/cli/config.json` with `features.mcp` on; Skills to `.zcode/skills`. ZCode inlines
an MCP tool's image only up to 200 KiB of base64: it saves a larger one, such as a
full-size `cua-desktop` screenshot, under `.zcode/cli/artifacts` and gives the model the
path, which its `Read` tool then shows the model as an image. ZCode looks up whether a
model takes images in its own catalog of model names and sends no image to a model the
catalog does not list as taking them, `claude-sonnet-4-5` among them. `input_format`,
such as `{ supports_image = true }`, is laid over the catalog's `inputFormat` for the run's
model by a rule in the personal provider file. ZCode sends telemetry only when OTLP
variables are set, and the command unsets them.

ZCode's `WebSearch` is a request of its own carrying the provider's `web_search` server
tool, which ZCode encodes only for Anthropic Messages. ZCode 0.17.0 offers it only to a
model whose `supportsNativeWebSearch` property is true, and its built-in rules grant that
by vendor URL, never to the Gateway's. The provider file therefore sets the property for
the run's model: true when the Task's network is `open` and the dialect is `anthropic`,
otherwise false. `WebFetch` fetches from inside the Sandbox and stays under its network
policy.

ZCode chooses the session ID and reports it in its final `result`; a resume passes it to
`--resume`. The conversation is in ZCode's SQLite store, `.zcode/cli/db/db.sqlite`, whose
schema the release does not promise to keep; the parser reads the pinned version's
`session`, `message`, `part` and `model_usage` tables from a private copy that takes the
write-ahead log along. Context ZCode injects as model-only user messages is not a turn.
Images are data URLs in `.zcode/cli/artifacts/<session id>/`. A failed turn has no record in
the store; its `turn.failed` event on stdout is added at the end of the turn. A turn the
store lacks is rebuilt from its prompt and stdout's `result`, with no times or usage.
Resume checks the store for the exact top-level session, because a session's own files
exist only for attachments or subagents; the check runs on the framework's Python.

Each assistant message is one model call's step with its tool calls, their outcomes and
usage. It takes the time the call finished, which `model_usage` records in Unix
milliseconds, the last attempt's when it was retried: the message's `time.created` is when
the request was sent, and its `time.completed` comes after its tool calls ran. The usage is
its `step-finish` part's `tokens`, which keep the AI SDK's totals: `input` already counts
the cache reads and writes and `output` the reasoning, as the Gateway does. Usage that
cannot be read is reported as `invalid_token_usage`. The store records no response ID, so
no step is linked to a Gateway call.

A subagent's `.zcode/cli/agents/<session id>/<agent id>/metadata.json` names the parent
session, the spawning call and the child session, whose row names the same parent. It
becomes an embedded trajectory when that call has an outcome; a child session that cannot
be linked is named in `parse_issues`.
