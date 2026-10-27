# ZCode

Z.ai publishes no CLI package. Its only official distribution is the desktop release,
whose installers carry the CLI as one JavaScript bundle, byte-identical on every
platform. The adapter downloads the Linux `.deb` of release 3.16.3 with the Sandbox's own
Python, accepts it only by the sha512 the release's `latest.yml` publishes (pinned in
code, never fetched at run time), and unpacks just `resources/glm/zcode.cjs` and
`resources/config/provider/zcode-builtin.json` into `.local/lib/zcode`, each accepted only
by its own sha256. The base Node runs it on Linux and Windows. The integrity is the
digest of those two files; the CLI reports version 0.17.0. Its public settings are
`disallowed_tools`, `workflow_mode` and `toolsearch_mode`.

The run is `node zcode.cjs --mode yolo --output-format stream-json --cwd <home>`. Headless
ZCode takes the prompt only as an argument, so it receives the recorded prompt file's
exact bytes as one, with Git Bash path conversion off on Windows. The prompt is then
bounded by 128 KiB per argument on Linux and about 32,000 characters for the whole Windows
command line; a longer one fails at launch instead of being truncated. The model route is a personal provider file named
by `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`. ZCode reads only a literal API key there, so the
file holds the episode token, readable by the agent account alone and never mirrored.
Each Gateway dialect selects ZCode's `anthropic-messages`, `openai-chat-completions` or
`openai-responses` API. The preset speaks `anthropic` to the `zai` provider, Z.ai's own
Anthropic endpoint, so it uses the Z.ai key and never the Anthropic one. MCP servers go to
`.zcode/cli/config.json` with `features.mcp` on; Skills to `.zcode/skills`. ZCode sends
telemetry only when OTLP variables are set, and the command unsets them.

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
`session`, `message` and `part` tables from a private copy that takes the write-ahead log
along. Each assistant message is one model step with its tool calls, their outcomes and
usage. Context ZCode injects as model-only user messages is not a turn. Images are data
URLs in `.zcode/cli/artifacts/<session id>/`. A failed turn has no record in the store; its
`turn.failed` event on stdout is added at the end of the turn. Resume checks the store for
the exact top-level session, because a session's own files exist only for attachments or
subagents.

A subagent's `.zcode/cli/agents/<session id>/<agent id>/metadata.json` names the parent
session, the spawning call and the child session, whose row names the same parent. It
becomes an embedded trajectory when that call has an outcome; a child session that cannot
be linked is named in `parse_issues`.
