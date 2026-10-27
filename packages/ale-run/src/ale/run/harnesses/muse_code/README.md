# Muse Code

Pinned to Muse Code `1.4.13-R6996.1`, Meta's release binaries for Linux x86_64 and
aarch64 and Windows x86_64. Its public settings are `reasoning_effort`, `max_model_steps`,
`reminders` and `web_tools`.

Runs Meta's Muse Code headless: `muse exec --json --prompt-file`, with `--yolo`, so
neither Muse's approval nor its own sandbox stands between the agent and the ALE
Sandbox, and `--no-foreign-personal-context`, so other agents' personal Skills stay out.
The program is Meta's single-file release binary for the Sandbox's OS and architecture,
pinned by SHA-256 from the release manifest; upstream ships no Windows ARM build.

Muse keeps settings under `XDG_CONFIG_HOME` and sessions under `XDG_DATA_HOME`. The
command names their defaults, `.config` and `.local/share` in the agent home, so both
stay in the episode on every system. `settings.json` turns telemetry off and holds the
MCP servers; Skills go to `.config/muse/skills`. Muse ignores unknown settings keys, so
the generated file is checked by the Docker integration run, not by a validator.

Muse speaks only Meta's own Responses API (namespace tools, dotted tool names), so the
Gateway dialect is `openai-responses` and the Gateway provider is `meta`. The episode token is
`META_API_KEY`. Muse sends that bearer only to Meta's endpoint unless `settings.json`
names another in `endpoint_transport` and pins `auth = "bearer"`; a `--base-url` flag is
not enough, so the Gateway URL is set there. Before each run Muse fetches
`GET /muse-code/models` from the Gateway's origin; the list's content is not used.

A launch passes a new UUID to `--session-id`, and a resume passes the same ID again. The
session is `.local/share/muse/sessions/<year>/<month>/<day>/<session id>/session.jsonl`.
The parser unwraps retained frames, drops repeated records, and follows the `run` events
in native order: one agent step per model response, with its usage. A tool's failure is a
`task` failure linked by `tool:<call id>`. A run Muse starts itself has no prompt; what it
delivers, such as a subagent's result, is a system step.

A subagent's start is attested with the call that dispatched it and its child session,
stored in the parent's `subagent/<child id>/`. It becomes an embedded trajectory when
that call has a result; any other session there is reported as an `unlinked_subagent`.
Reminder agents, on unless `reminders = false`, are the exception: Muse runs them itself,
with no call of the model to link, and stores their sessions in the same place. The parent names each one in a `memory_reminder_child_session_linked` run event,
so the parser leaves those sessions as raw evidence named in `unconverted_native_files`
without reporting them as unlinked. The step ceiling fails the run with "did not reach a
terminal state"; ALE reports it as the `max_model_steps` Harness limit.
