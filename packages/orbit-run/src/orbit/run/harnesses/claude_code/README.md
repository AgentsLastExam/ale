# Claude Code

Runs `claude --print --output-format=stream-json` with the prompt on stdin. ALE pins its
own `CLAUDE_CONFIG_DIR` at `.claude-config`, so the native session lives in
`.claude-config/projects/<project>/<session id>.jsonl`, with subagent sessions below
`<session id>/subagents/`.

Claude Code sends the model every image, an MCP tool's included: it has no notion of a
model without image input, and no setting for one.

The parser follows the native session, which holds the prompts, and places the
stream-only `result` events after the record that preceded them. A subagent session
`agent-<id>.jsonl` names the call that spawned it in the `toolUseId` of the
`agent-<id>.meta.json` beside it.

WebSearch runs Anthropic's `web_search` server tool in a request of its own, so unless
the Task's network is `open` ALE adds it to `--disallowedTools`. WebFetch fetches from
inside the Sandbox and stays under its network policy.

Claude Code 2.1.227 sends a failed request again ten times, for a 408, 409, 429 or 5xx
or a failed connection, waiting from 0.5 s doubling to 32 s: about three minutes in all. It
would wait what `retry-after` names, up to 60 s, but the Gateway passes a failure on without
it. It does not try a quota again, such as a credit balance too low.
