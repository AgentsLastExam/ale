# Grok Build

Runs Grok headless with `--prompt-file`, from the platform build its npm package
`@xai-official/grok` names as an optional dependency. npm ships that build
brotli-compressed, which no Sandbox can undo with the standard library, so the Host
decompresses it and uploads the result, to a cloud Sandbox too. A session is a directory
named by its ID below `.grok-orbit/sessions`, holding `chat_history.jsonl` and
`updates.jsonl`; the parser joins the two and records tool failures from the updates. Other
session directories are left unconverted, named in `parse_issues`, and are not guessed to be
children.

The session records no model usage or response IDs. Grok's own log,
`.grok-orbit/logs/unified.jsonl`, which a resume appends to, records
`shell.handle_prompt.start` as each prompt starts and `shell.turn.inference_done` as each
model call completes, with the call's usage. It names no message, so the parser pairs a
session's records with its prompts and assistant messages in order. When the log records
the session but holds a different number of prompts or calls than the session, the parser
pairs none and reports `unpaired_log_records`. A prompt step takes its start time, and the
step of a call's assistant message the call's completion time and usage: `prompt_tokens`
include cache reads and writes, and `completion_tokens` reasoning. Usage that cannot be
read is reported as `invalid_token_usage`. With no response ID, no step is linked to its
Gateway call. The call that writes the session's title is not in the log and produces no
step. A turn rebuilt from stdout has no times, usage or response IDs.

Grok gives the model an MCP tool's image as data-URL text in the tool's output. It splits
images out only from an output over its MCP output limit, which it first truncates, writing
the whole output to the session's `mcp/<call id>.txt`; the truncated image fails Grok's
own check and is dropped. So the model never sees an MCP screenshot, such as
`cua-desktop`'s. Grok takes every model to accept images and has no setting for it.

Grok's `web_search` is a request of its own carrying xAI's hosted `web_search` tool, sent
to the model `[models] web_search` names, `grok-4.5` unless set. The generated config names
the run's model, so the request goes through the Gateway. Unless the Task's network is
`open`, Orbit passes `--disable-web-search`, which removes it and `web_fetch`. `web_fetch`
is off in Grok by default and fetches from inside the Sandbox when on.
