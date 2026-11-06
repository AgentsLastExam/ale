# Antigravity CLI

Runs Google's Antigravity CLI, `agy`, headless:
`agy --input-format stream-json --output-format stream-json --dangerously-skip-permissions
--print-timeout=0s --model <family> [--effort <level>]`. It uses the documented Gemini
API-key mode: `~/.gemini/antigravity-cli/settings.json` sets `"modelProvider": "gemini"`,
`GEMINI_API_KEY` carries the episode token and `GOOGLE_GEMINI_BASE_URL` points at the
Gateway, which serves it the `gemini` dialect:
`POST /v1beta/models/{model}:streamGenerateContent?alt=sse`. The preset's
`gateway.provider = "gemini"` has the Host Gateway forward those calls with
`ALE_GEMINI_API_KEY`, to `ALE_GEMINI_BASE_URL` or else Google's own API. Google-account
(OAuth) mode, and with it the Claude and GPT-OSS models agy serves through Google, is not
used: its backend cannot be redirected to the Gateway.

`agy` is not on npm. Its `release_assets` pin Google's 1.4.7 release by URL and
SHA-256: a `tar.gz` holding the single executable `antigravity` on Linux (x86_64 and
aarch64) and macOS (arm64), whose member is pinned too, and a bare `.exe` on Windows
(x86_64 and aarch64).
Google's updater manifest names only the latest build, which is why the URL is pinned.
`AGY_CLI_DISABLE_AUTO_UPDATE=true` stops the background updater; `1` does not.

The run's model is the Gemini API model the Gateway sends upstream. In API-key mode agy
selects it by catalog family and effort, so the adapter maps it: `gemini-4-argon`,
`gemini-3.8-flash`, `gemini-3.7-flash` and `gemini-3.6-flash` take `low`, `medium` or
`high`; `gemini-3.1-pro-preview` is the family `gemini-3.1-pro` and takes `low` or
`high`. Any other model, or an effort the family does not offer, fails before launch.
What a model takes, images included, comes from that catalog.
`effort` is the only public setting; `default` omits `--effort`, so agy uses the
family's own default. agy also asks `gemini-3.1-flash-lite-preview` for a conversation
title, which the Gateway answers with the run's model. Its `search_web` tool is a separate
Google Search grounding call, which the Gateway allows only when the Task's network is
open; otherwise the refusal reaches the agent as that tool's error.

agy reads a headless prompt from stdin only as a stream-json message, so the command first
has the Sandbox's Python (on macOS the framework's) wrap `prompt.md` into
`.ale-harness/request.jsonl`, one `{"event": "user", "message": {"content": ...}}` line, and
feeds that to agy. agy chooses the conversation ID and reports it in its `init` and `result`
events; a resume passes it to `--conversation`. A failed turn ends with a `result` whose
`status` is not `SUCCESS`, exits non-zero, and writes an `AGY_ERROR:` JSON line to stderr.

Skills go to `~/.gemini/antigravity-cli/skills/<name>/` and MCP servers to
`~/.gemini/config/mcp_config.json` (`command`/`args`/`env`/`cwd`, or `serverUrl`). agy
offers lazy MCP tools through its `call_mcp_tool` wrapper, which the parser records as
the `mcp__<server>__<tool>` call it made. agy inlines only a small MCP image (a desktop
screenshot capped at 64 px, about 6 KB, but not one at 128 px, about 18 KB): it saves a
larger one to the step's `media_<n>` file and gives the model the path, which `read_file`
then shows the model as an image. The parser records that file as the result's image.

A conversation is `~/.gemini/antigravity-cli/brain/<id>/` plus `conversations/<id>.db`.
The parser follows `.system_generated/logs/transcript_full.jsonl`: a `PLANNER_RESPONSE`
is a model turn with its tool calls, and the records after it are their results in call
order, since they carry no call ID; stdout's `step_update` events name each result's tool
and must agree. Images agy inlined are read from the conversation's `logs/media`.

A subagent is linked by `.system_generated/subagents/<child>.json` in its parent's
directory: its `spawnStepIndex` is the step of the `invoke_subagent` result, which then
references the child's trajectory `<parent trajectory>-subagent-<child id>`. The parent's
task and later messages reach the child as `[Message] ... sender=<parent id>` records,
which become the child's user steps.

A background tool, such as `schedule`, returns a running notice and agy records the step
as RUNNING; the tool's final output arrives only later, in `steps/<n>/output.txt`. When
that file holds output the record lacks, it is kept on the step's result as
`extra.ale.final_output`, and the result's content stays what the model received.

agy also keeps an abridged `transcript.jsonl`, chunked copies of both transcripts, the
`messages/` delivered to a conversation, background `tasks/*.log`, the
`.agents/agents/<name>/agent.md` a `define_subagent` call wrote, and
`conversations/<id>.db`. The parser reads none of them.
