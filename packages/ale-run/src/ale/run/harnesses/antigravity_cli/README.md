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
aarch64), whose member is pinned too, and a bare `.exe` on Windows (x86_64 and aarch64).
Google's updater manifest names only the latest build, which is why the URL is pinned.
`AGY_CLI_DISABLE_AUTO_UPDATE=true` stops the background updater; `1` does not.

The run's model is the Gemini API model the Gateway sends upstream. In API-key mode agy
selects it by catalog family and effort, so the adapter maps it: `gemini-4-argon`,
`gemini-3.8-flash`, `gemini-3.7-flash` and `gemini-3.6-flash` take `low`, `medium` or
`high`; `gemini-3.1-pro-preview` is the family `gemini-3.1-pro` and takes `low` or
`high`. Any other model, or an effort the family does not offer, fails before launch.
`effort` is the only public setting; `default` omits `--effort`, so agy uses the
family's own default. agy also asks `gemini-3.1-flash-lite-preview` for a conversation
title, which the Gateway answers with the run's model. Its `search_web` tool is a separate
Google Search grounding call, which the Gateway allows only when the Task's network is
open; otherwise the refusal reaches the agent as that tool's error.

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
and must agree. Images a tool returned are read from the conversation's `logs/media`.

A subagent is linked by `.system_generated/subagents/<child>.json` in its parent's
directory: its `spawnStepIndex` is the step of the `invoke_subagent` result, which then
references the child's trajectory `<parent trajectory>-subagent-<child id>`. The parent's
task and later messages reach the child as `[Message] ... sender=<parent id>` records,
which become the child's user steps.

A background tool, such as `schedule`, returns a running notice and agy records the step
as RUNNING; the tool's final output arrives only later, in `steps/<n>/output.txt`. When
that file holds output the record lacks, it is kept on the step's result as
`extra.ale.final_output`, and the result's content stays what the model received.

agy keeps copies beside each transcript: an abridged `transcript.jsonl` with arguments
as JSON text and long fields cut, both split under `logs/chunks/`, each tool's
`steps/<n>/output.txt` and images, the `messages/` delivered to the conversation with
their `read.json`, background `tasks/*.log`, and `.agents/agents/<name>/agent.md` for a
`define_subagent` call. Each is consumed only when the converted records hold all of its
content.

`conversations/<id>.db` is consumed with its conversation while its `steps` table holds no
more steps than the transcript. That table repeats the step records with agy's internal
state: nanosecond timestamps, request and call IDs, encrypted thought signatures, UI
`render_info`, permissions and task details. `gen_metadata` holds each generation's usage,
latency and model, and in its last row the full model request (system prompt, tool
declarations, message history), which the Gateway's transport trace records.
`executor_metadata` and `trajectory_metadata_blob` hold agy's feature flags, permission
rules and project; `parent_references` and `battle_mode_infos` are empty. A database
with more steps than the transcript holds a turn the transcript lost and stays raw
evidence.
