# Droid

Runs Factory's `droid exec --output-format stream-json` with `--file` reading the prompt.
The official npm package `droid`, pinned at 0.249.0, carries the platform binary as an
exact-version optional dependency, so Linux and Windows install the same way. `FACTORY_HOME_OVERRIDE` moves all of
Droid's state to `.droid-ale/.factory`, where ALE writes `settings.json` with one BYOK
custom model, the episode Gateway, and `mcp.json`; Skills go to `.droid-ale/.factory/skills`.

The Gateway dialect selects the BYOK provider: `anthropic`, `openai` (Responses) or
`generic-chat-completion-api`. The API key is the `${ALE_GATEWAY_TOKEN}` reference, which
Droid resolves from the environment per request. No Factory account is involved:
`droid exec` skips Factory sign-in for BYOK models. Droid still tries to reach Factory's
API and telemetry hosts, which a sealed Sandbox refuses; a Task that opens egress lets
those requests, session sync included, leave the Sandbox.

`--session-id` resumes the session with that ID or starts a new one under it, so ALE
chooses the ID of every new session. The session is
`.droid-ale/.factory/sessions/<cwd>/<session id>.jsonl`. The parser follows it, skipping
the context Droid injects before each prompt (`llm_only` messages) and turning the notices
it shows its user (`user_only`) into system steps. Stream-json supplies the per-invocation
usage, a failure the session does not record, and every turn the session lacks.

Task subagents need Factory's daemon and a signed-in Factory user, so ALE always removes
the `Task` tool; `disabled_tools` removes more, and `max_output_tokens` caps each reply.
Droid cannot set a stdio MCP server's working directory, so ALE rejects a server that
declares one. On Windows, Droid's `Execute` tool runs PowerShell.
