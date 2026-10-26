# Codex CLI

Runs `codex exec --json`, or `codex exec resume <thread id>`, with `-` reading the prompt
from stdin. The rollout lives in `.codex-ale/sessions/**/rollout-<time>-<thread id>.jsonl`.

The parser follows the rollout. Its user messages include context Codex injects, so a
turn begins only at the exact prompt ALE recorded for it. MCP calls take their real
server name and structured result from the matching stdout event, matched on server,
tool, and arguments.

A spawned thread's rollout names its parent in `session_meta.parent_thread_id`. Its
spawning call is the parent's call whose result names the thread ID: `spawn_agent`, or a
code-mode `exec` script that calls `multi_agent_v1__spawn_agent` and prints what it
returned. The thread's turns begin at its `user_message` events, which Codex records for
each input from the parent but not for injected context. Stdout reports only the root
thread, so a subagent's MCP calls keep the rollout's sanitized server name.
