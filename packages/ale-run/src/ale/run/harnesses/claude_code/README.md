# Claude Code

Runs `claude --print --output-format=stream-json` with the prompt on stdin. ALE pins its
own `CLAUDE_CONFIG_DIR` at `.claude-config`, so the native session lives in
`.claude-config/projects/<project>/<session id>.jsonl`, with subagent sessions below
`<session id>/subagents/`.

The parser follows the native session, which holds the prompts, and places the
stream-only `result` events after the record that preceded them. A subagent session
`agent-<id>.jsonl` names the call that spawned it in the `toolUseId` of the
`agent-<id>.meta.json` beside it.
