# Pi coding agent

Runs `pi --mode json` with the prompt on stdin, from the official npm package
`@earendil-works/pi-coding-agent` pinned at 1.4.0. `PI_CODING_AGENT_DIR` points at
`.pi-ale/agent`, where ALE writes `models.json` with one custom provider, the episode
Gateway, plus `settings.json`, `mcp.json` and the declared Skills. The Gateway dialect
selects pi's API type (`anthropic-messages`, `openai-completions` or `openai-responses`);
the API key is the `$ALE_GATEWAY_TOKEN` reference pi interpolates from the environment.
`PI_OFFLINE` stops startup network use: version checks, catalog refreshes and telemetry.

`--session-id` opens the session with that ID or creates it, so ALE chooses the ID of
every new session; `--session-dir .pi-ale/sessions` keeps sessions in one flat directory
as `<timestamp>_<session id>.jsonl`. The parser follows the session's active branch and
uses an invocation's own JSON events only for a turn the session lacks. In JSON mode pi
exits 0 even when the model call failed, so the outcome is the last assistant message's
stop reason; `error`, `aborted` and `length` fail the run, as in pi's print mode.

MCP servers use `direct` exposure, so their tools are declared like built-ins and their
results are recorded; pi's default, `codemode`, reaches them only from a script and keeps
no results. The settings are `thinking`, `context_window`, `max_output_tokens` and
`disabled_tools`. Pi has no subagents. Pi trims the prompt it reads, and a prompt that is one
of its registered commands, such as `/mcp`, runs no turn and fails the invocation.
