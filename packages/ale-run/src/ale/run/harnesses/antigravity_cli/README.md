# Antigravity CLI

Runs Google's Antigravity CLI, `agy`, headless:
`agy --input-format stream-json --output-format stream-json --dangerously-skip-permissions
--print-timeout=0s --model <family> [--effort <level>]`. It uses the documented Gemini
API-key mode: `~/.gemini/antigravity-cli/settings.json` sets `"modelProvider": "gemini"`,
`GEMINI_API_KEY` carries the episode token and `GOOGLE_GEMINI_BASE_URL` points at the
Gateway, which receives `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`.
Google-account (OAuth) mode, and with it the Claude and GPT-OSS models agy serves through
Google, is not used: its backend cannot be redirected to the Gateway.

`agy` is not on npm. Its `release_assets` pin Google's 1.4.7 release by URL and
SHA-256: a `tar.gz` holding the single executable `antigravity` on Linux (x86_64 and
aarch64), whose member is pinned too, and a bare `.exe` on Windows (x86_64 and aarch64).
Google's updater manifest names only the latest build, which is why the URL is pinned.
`AGY_CLI_DISABLE_AUTO_UPDATE=true` stops the background updater; `1` does not.

The run's model is the Gemini API model the Gateway sends upstream. In API-key mode agy
selects it by catalog family and effort, so the adapter maps it: `gemini-4-argon`,
`gemini-3.8-flash`, `gemini-3.7-flash` and `gemini-3.6-flash` take `low`, `medium` or
`high`; `gemini-3.1-pro-preview` is the family `gemini-3.1-pro` and takes `low` or
`high`. Any other model, or an effort the family does not offer, fails before launch.
`effort = "default"` omits `--effort`, so agy uses the family's own default. agy also
asks `gemini-3.1-flash-lite-preview` for a conversation title, which the Gateway answers
with the run's model. Its `search_web` tool is a separate Google Search grounding call,
which the Gateway refuses, so that tool reports an error to the agent.

agy reads a headless prompt from stdin only as a stream-json message, so the command
first has the Sandbox's Python wrap `prompt.md` into `.ale-harness/request.jsonl`, one
`{"event": "user", "message": {"content": ...}}` line, and feeds that to agy. agy chooses
the conversation ID and reports it in its `init` and `result` events; a resume passes it
to `--conversation`. A failed turn ends with a `result` whose `status` is not `SUCCESS`,
exits non-zero, and writes an `AGY_ERROR:` JSON line to stderr.

Skills go to `~/.gemini/antigravity-cli/skills/<name>/` and MCP servers to
`~/.gemini/config/mcp_config.json` (`command`/`args`/`env`/`cwd`, or `serverUrl`). agy
offers lazy MCP tools through its `call_mcp_tool` wrapper, which the parser records as
the `mcp__<server>__<tool>` call it made.

A conversation is `~/.gemini/antigravity-cli/brain/<id>/` plus `conversations/<id>.db`.
The parser follows `.system_generated/logs/transcript_full.jsonl`: a `PLANNER_RESPONSE`
is a model turn with its tool calls, and the records after it are their results in call
order, since they carry no call ID; stdout's `step_update` events name each result's tool
and must agree. Images a tool returned are read from the conversation's `logs/media`. A
subagent's conversation becomes an embedded trajectory, linked from the result of the
step that spawned it as `.system_generated/subagents/<id>.json` records. The database,
the abridged `transcript.jsonl`, per-step outputs and messages stay raw evidence and are
named in `parse_issues`.
