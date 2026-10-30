# Qwen Code

Pinned to the npm package `@qwen-code/qwen-code@0.26.1`. Its public settings are
`max_session_turns`, `reasoning_effort`, `context_window_size` and `modalities`.

Runs Qwen Code headless, `qwen --output-format stream-json --approval-mode yolo`, with
the prompt on stdin. `QWEN_HOME` and `QWEN_RUNTIME_DIR` are both `.qwen-ale`. The model
route, MCP servers and native controls are written to `.qwen-ale/system-settings.json`,
named by `QWEN_CODE_SYSTEM_SETTINGS_PATH`, so they override a workspace's own
`.qwen/settings.json`; an empty system defaults file replaces any machine-wide one. The
route reads the episode token from `ALE_GATEWAY_TOKEN` through `envKey` and takes
precedence over `OPENAI_*` variables, which the command also unsets. Usage statistics,
telemetry and the models.dev refresh are off. `QWEN_CODE_NO_RELAUNCH` keeps Qwen from
relaunching itself with a heap of half the host's total memory, which ignores the
Sandbox's memory limit; Node's own default heap follows the limit. Everything else,
including Qwen's managed memory and its deferred tools, keeps the pinned program's
defaults.

The Gateway dialect selects the protocol: `openai` for Chat Completions, the default,
`openai-responses` (the `openai` group with `wireApi = "responses"`) and `anthropic`.
The preset's Gateway provider is `qwen`, Qwen's own compatible-mode API.
A DashScope key needs `ALE_QWEN_BASE_URL` set to DashScope's compatible-mode host, such
as `https://dashscope-intl.aliyuncs.com/compatible-mode`.

Qwen's `web_search` is a request of its own that has DashScope run the search on its
side. Qwen 0.26.1 offers it only when the model's base URL is an https DashScope host, so
behind the Gateway it is off in every Task, `open` ones included. ALE does not offer it
yet: that would take Qwen's explicit search backend (`WEB_SEARCH_BASE_URL`,
`WEB_SEARCH_API_KEY`, `WEB_SEARCH_MODEL`) aimed at the Gateway, a Gateway session that
serves `/responses`, and DashScope upstream. `web_fetch` fetches from inside the Sandbox
and stays under its network policy.

A launch passes a new UUID to `--session-id`; a resume passes it to `--resume=`. The
session is `.qwen-ale/projects/<project>/chats/<session id>.jsonl`. Qwen appends a blank
line to a prompt read from stdin, so the parser removes it from each prompt. Calls Qwen
defers, MCP tools and `agent` among them, are reviewed with `tool_search` and made
through `tool_call`; the parser records such a call as the call it made and notes the
wrapper. A failed turn has no record in the session; its stdout `result` is added at the
end of the turn.

A subagent writes `subagents/<session id>/agent-<type>-<call id>.jsonl` beside a
`.meta.json` naming the session, the parent agent and the spawning call. It becomes an
embedded trajectory when that call has a result. Other transcripts stay raw evidence
named in `parse_issues`.

The turn ceiling exits 53 with no `result` on stdout; ALE reports it as the
`max_session_turns` Harness limit.
