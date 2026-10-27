# Kimi Code

Pinned to the npm package `@moonshot-ai/kimi-code@2.3.2`. Its public settings are
`max_context_size`, `capabilities`, `thinking_effort` and `max_steps_per_turn`.

Runs Kimi Code's print mode, `kimi --prompt --output-format stream-json`, with
`KIMI_CODE_HOME=.kimi-ale` and `--skills-dir .kimi-ale/skills`, so neither the
operator's nor the project's Skill directories are discovered. The model, its Gateway
endpoint and its native controls are written to `.kimi-ale/config.toml`; the episode
token is read from `ALE_GATEWAY_TOKEN` on each request and never written. MCP servers go
to the user-level `.kimi-ale/mcp.json`. The workspace is not marked trusted, so a
project-level `.kimi-code/mcp.json` stays disabled.

Each Gateway dialect selects Kimi's own provider for it: `kimi` for Chat Completions, the
default, `anthropic` for Messages and `openai_responses` for Responses. The preset's
Gateway provider is `moonshot`, Moonshot's own API. Kimi requires every model to declare `max_context_size`; non-default
`thinking_effort` values require the `thinking` or `always_thinking` capability.

Print mode accepts the prompt only as an argument. The script reads the recorded
`prompt.md` into that one argument, keeping its trailing newlines, and on Windows turns
Git Bash path conversion off for the call. The prompt is then bounded by 128 KiB per
argument on Linux and about 32,000 characters for the whole Windows command line; a
longer one fails at launch instead of being truncated. Kimi chooses the session ID
and reports it in its final `session.resume_hint` line; a resume passes it to
`--session`. A failed turn prints nothing on stdout and exits 1 with the cause on stderr.

A session is `.kimi-ale/sessions/<work directory bucket>/<session id>/`. The parser
follows `agents/main/wire.jsonl` in native order. A subagent becomes an embedded
trajectory when the caller's wire holds a `subagent.spawned` record naming the calling
tool call, that call has a result, and the subagent's own `agents/<id>/wire.jsonl` is
present. Images larger than 4 KiB are read from `agents/<id>/blobs`. Any other wire stays
raw evidence and is named in `parse_issues`.

The Grep tool needs ripgrep. The bases do not provide it and Kimi downloads it on first
use, which fails once egress is sealed; the agent can still search through Bash.
