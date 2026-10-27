# ForgeCode

Runs `forge --conversation-id <id>` with the prompt on stdin. ForgeCode ships as
single-file release binaries; its npm package stopped at 2.13.21. ALE pins the official
v2.14.0 assets for Linux (static musl) and Windows. `FORGE_CONFIG` is `.forge-ale`, where
ALE writes `.forge.toml`, `.credentials.json` with the episode token, `.mcp.json` and the
declared Skills. The provider is forge's own compatible provider for the Gateway dialect,
with the model listed in the configuration so forge never asks for `/v1/models`.

Forge would update itself and exit before running the prompt, so updates are off in the
configuration and in the environment. Its cloud services URL points nowhere. Its PostHog
telemetry cannot be turned off: a sealed Sandbox refuses it, and a Task that opens
egress lets it out, prompts included.

Known upstream issue: at every start of an untitled conversation, forge makes one extra
model call, without tools, to title it. On the Anthropic dialect 2.14.0 asks for that
title in the deprecated top-level `output_format` field, which the Anthropic API rejects
with a 400 naming `output_config.format`. Forge ignores the failure and the agent loop is
unaffected, but the conversation stays untitled, so every invocation records one failed
Gateway call. Forge has no setting that turns titling off: it skips only a conversation
that already has a title, and the Gateway does not rewrite requests.

`--conversation-id` continues a conversation or starts one under ALE's ID. Forge keeps
conversations only in SQLite, so after every invocation `forge conversation dump` exports
the whole conversation to `.forge-ale/dumps/<id>/<time>.json`; the parser reads the newest
export and the matching invocation's stdout. Forge exits 0 on most failures: a turn
succeeded only when stdout ends it with `Finished <id>`, and its request limit fails the
invocation. Delegated `task` conversations become subagent trajectories, linked by the
conversation ID in the task's result. MCP tools are `mcp_<server>_tool_<tool>`, both
sanitized; the parser restores the server's declared name from `.forge-ale/.mcp.json`,
while the tool keeps forge's sanitized name. Forge cannot set a stdio MCP server's working
directory, so ALE rejects a server that declares one. On Windows, forge's shell tool runs
`cmd.exe`. The settings are `reasoning_effort`, `max_output_tokens` and
`max_requests_per_turn`.
