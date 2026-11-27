# Pi coding agent

Runs `pi --mode json` with the prompt on stdin, from the official npm package
`@earendil-works/pi-coding-agent` pinned at 1.4.0. `PI_CODING_AGENT_DIR` points at
`.pi-orbit/agent`, where Orbit writes `models.json` with one custom provider, the episode
Gateway, plus `settings.json`, `mcp.json` and the declared Skills. The Gateway dialect
selects pi's API type (`anthropic-messages`, `openai-completions` or `openai-responses`);
the API key is the `$ORBIT_GATEWAY_TOKEN` reference pi interpolates from the environment.
`PI_OFFLINE` stops startup network use: version checks, catalog refreshes and telemetry.

`--session-id` opens the session with that ID or creates it, so Orbit chooses the ID of
every new session; `--session-dir .pi-orbit/sessions` keeps sessions in one flat directory
as `<timestamp>_<session id>.jsonl`. The parser follows the session's active branch and
uses an invocation's own JSON events only for a turn the session lacks. In JSON mode pi
exits 0 even when the model call failed, so the outcome is the last assistant message's
stop reason; `error`, `aborted` and `length` fail the run, as in pi's print mode.

A step takes the time pi wrote its session entry, once the message had ended. Each
assistant message is one model call: its step lists the message's `responseId`, the
provider's response ID, after the entry ID, so Orbit links it to its Gateway call, and
carries the message's `usage` as metrics. `prompt_tokens` adds `cacheRead` and
`cacheWrite` to pi's `input`, which leaves them out, and `completion_tokens` is pi's
`output`, which includes reasoning. Usage that cannot be read is reported as
`invalid_token_usage`. A turn rebuilt from JSON events keeps the usage and response IDs but
has no entry times: its prompt takes the message's creation time, and its assistant steps
stay untimed, since pi creates an assistant message when its model call begins.

MCP servers use `direct` exposure, so their tools are declared like built-ins and their
results are recorded; pi's default, `codemode`, reaches them only from a script and keeps
no results. The settings are `thinking`, `context_window`, `max_output_tokens`,
`disabled_tools` and `input`, the model's `input` in `models.json`. Unset, pi takes the
model to be text-only and replaces each image, an MCP screenshot included, with a note
that the model does not support images. Pi has no subagents. Pi trims the prompt it
reads, and a prompt that is one of its registered commands, such as `/mcp`, runs no turn
and fails the invocation.
