# Codex CLI

Runs `codex exec --json`, or `codex exec resume <thread id>`, with `-` reading the prompt
from stdin. The rollout lives in `.codex-ale/sessions/**/rollout-<time>-<thread id>.jsonl`.

The parser follows the rollout. Its user messages include context Codex injects, so a
turn begins only at the exact prompt ALE recorded for it. The model's MCP calls take
their real server name and structured result from the matching stdout event, matched on
server, tool, and arguments.

In code mode the model calls one `exec` tool, whose JavaScript calls the other tools
through `tools.<name>(...)`. The rollout keeps each `exec` call and its output, but of
the script's own calls only the end record of an MCP call (`mcp_tool_call_end`) or a
patch (`patch_apply_end`), under an `exec-<uuid>` ID that no model call carries. Each
becomes its own step, in rollout order, with `llm_call_count: 0` because no model
inference chose it. Codex records no link to the script that made the call, so the step
names none; it follows that script's `exec` call unless an earlier script that yielded
is still running. Legacy history, which `codex exec` always writes, keeps no record of
shell commands, so a script's `exec_command` calls appear only in its source and in what
it printed. Stdout lists them without the script that ran them.

A spawned thread's rollout names its parent in `session_meta.parent_thread_id`. Its
spawning call is the parent's call whose result names the thread ID: `spawn_agent`, or a
code-mode `exec` script that calls `multi_agent_v1__spawn_agent` and prints what it
returned. The thread's turns begin at its `user_message` events, which Codex records for
each input from the parent but not for injected context. Stdout reports only the root
thread, so a subagent's MCP calls keep the rollout's sanitized server name.
