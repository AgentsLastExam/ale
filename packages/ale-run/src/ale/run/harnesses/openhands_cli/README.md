# OpenHands CLI

Runs `openhands --headless --json --override-with-envs --resume <id> --file <prompt>`.
1.16.0 is the CLI's final release: upstream has marked it unmaintained in favor of a
browser application. It ships as single-file PyInstaller binaries for Linux only, and
upstream supports Windows only through WSL, so this Harness is Linux-only.

The model comes from `LLM_MODEL`, `LLM_BASE_URL` and `LLM_API_KEY`, which make the CLI build
its default agent around the episode Gateway; nothing else about the agent is
configurable without replacing that definition, so the Harness has no settings. The SDK
chooses the wire API from the model name: the GPT-5 family goes over OpenAI Responses, any
other OpenAI-routed model over Chat Completions, so the Harness rejects a Gateway dialect
the SDK would not use for the run's model. `OPENHANDS_PERSISTENCE_DIR` is `.openhands-ale`,
holding `mcp.json` and the conversations; Skills go to `~/.openhands/skills`, where the CLI
looks. The SDK clones OpenHands' public skills from a moving branch at every start, so an
empty local repository stands in its cache and none load.

`--resume` continues a conversation or starts one under ALE's ID. A conversation is
`.openhands-ale/conversations/<id>/events/event-<n>-<uuid>.json`, one file per event, each
written once. The parser follows the events and unwraps the frame `--file` puts around
the prompt; the JSON events on stdout supply any turn the conversation lacks. The CLI
exits 0 when the conversation fails, so a `ConversationErrorEvent` fails the invocation.
A `task` delegation runs in a conversation below `subagents/<id>/`. Its observation names
the task, and when the task ends the SDK records the child's model calls under
`task:<task id>` in the parent's `base_state.json`; the child whose own response IDs are
among them, with events during the call, is the one the call spawned. Task IDs restart in
every process, so the time window keeps a later task's metrics from claiming an earlier
child. A child without that link, such as one stopped before its metrics were written,
stays raw evidence. With one MCP server its tools keep their
names; with several, each is `<server>_<tool>`, resolved from `.openhands-ale/mcp.json`.
