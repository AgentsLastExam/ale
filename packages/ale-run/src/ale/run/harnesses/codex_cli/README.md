# Codex CLI

Runs `codex exec --json`, or `codex exec resume <thread id>`, with `-` reading the prompt
from stdin. The rollout lives in `.codex-ale/sessions/**/rollout-<time>-<thread id>.jsonl`.

The parser follows the rollout. Its user messages include context Codex injects, so a
turn begins only at the exact prompt ALE recorded for it. The model's MCP calls take
their real server name and structured result from the matching stdout event, matched on
server, tool, and arguments.

In code mode the model calls one `exec` tool, whose JavaScript calls the other tools
through `tools.<name>(...)`. The rollout keeps each `exec` call and its output, but not
the script's own calls: legacy history, which `codex exec` always writes, drops shell
commands entirely. Codex's rollout trace records them, so ALE sets
`CODEX_ROLLOUT_TRACE_ROOT` to `.codex-ale/rollout-traces` and mirrors it. Each
`codex exec` process, a resume included, writes one bundle there,
`trace-<trace id>-<thread id>`, which the threads it spawns share. `trace.jsonl` orders
its events and `payloads/` holds their data.

A script's call is a `tool_call_started` event under an `exec-<uuid>` ID. Its requester
is the code cell that `code_cell_started` tied to the `exec` call. The event names the
payloads with the call's arguments and with the value the script got back; for an MCP
call, Codex's record of running it keeps the real server name. Each call becomes its own
step with `llm_call_count: 0`, because no model inference chose it. The step follows the
model call that last preceded it in the trace and names its `exec` call in
`extra.ale.caller`; a script that yielded can keep calling tools after the model's next
call. The parser reads only payload paths of the form `payloads/<n>.json` in the bundle
being parsed. A thread that ran `exec` with no trace is reported as
`rollout_trace_unavailable`, and a call whose preceding model call the rollout lacks as
`unplaced_script_calls`.

The trace also keeps every model request and response in full, which the parser never
reads, so it grows with the square of the turn count; with `native_logs = "minimal"`
ALE deletes it after a clean parse. Tracing adds an `x-codex-inference-call-id` header to
each model request, which the Gateway forwards and leaves out of the request digest.

A spawned thread's rollout names its parent in `session_meta.parent_thread_id`. Its
spawning call is the parent's call whose result names the thread ID: `spawn_agent`, or a
code-mode `exec` script that calls `multi_agent_v1__spawn_agent` and prints what it
returned. The thread's turns begin at its `user_message` events, which Codex records for
each input from the parent but not for injected context. Stdout reports only the root
thread, so a subagent's MCP calls keep the rollout's sanitized server name.

Codex 0.146.0 enables OpenAI's hosted web search by default. The generated config always
sets `web_search`: `"live"` when the `web_search` setting is on, as it is by default, and
the Task's network is `open`, otherwise `"disabled"`. A model Codex drives through
Responses Lite, such as `gpt-5.6-luna`, gets no hosted search even then: Codex's search for
it, `web__run` inside `exec`, posts to the provider's `/alpha/search`, which the Gateway
does not serve, so ALE does not offer it.
