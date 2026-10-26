# Codex CLI

Runs `codex exec --json`, or `codex exec resume <thread id>`, with `-` reading the prompt
from stdin. The rollout lives in `.codex-ale/sessions/**/rollout-<time>-<thread id>.jsonl`.

The parser follows the rollout. Its user messages include context Codex injects, so a
turn begins only at the exact prompt ALE recorded for it. MCP calls take their real
server name and structured result from the matching stdout event, matched on server,
tool, and arguments.

A spawned thread has a rollout of its own whose `session_meta` names its parent in
`parent_thread_id`. It becomes a subagent trajectory referenced from the parent's call
whose result names its thread ID: a `multi_agent_v1` `spawn_agent` call, or a code-mode
`exec` script that calls `multi_agent_v1__spawn_agent` and prints what it returned. A
script that spawns without printing the ID leaves the thread unlinked. The thread's turns
begin at its `user_message` events, which Codex records for each input the parent sent
but not for the context it injects. Stdout reports only the root thread, so a subagent's
MCP calls keep the sanitized server name the rollout records.
