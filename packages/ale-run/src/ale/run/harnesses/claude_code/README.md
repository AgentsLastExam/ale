# Claude Code

Runs `claude --print --output-format=stream-json` with the prompt on stdin. ALE pins its
own `CLAUDE_CONFIG_DIR` at `.claude-config`, so the native session lives in
`.claude-config/projects/<project>/<session id>.jsonl`, with subagent sessions below
`<session id>/`.

The parser follows the native session, which holds the prompts, and places the
stream-only `result` events after the record that preceded them. A subagent is converted
only when its parent tool call is identifiable; otherwise it stays raw evidence with a
parse issue.
