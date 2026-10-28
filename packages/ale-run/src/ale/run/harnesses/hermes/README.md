# Hermes Agent

Runs one `hermes chat --query-file <prompt> --format stream-json --yolo` per invocation.
Hermes is distributed only by its installer: upstream refuses wheels and has no PyPI
package. ALE runs the official `install.sh` from commit `bf0203a7` (v0.22.5) with
`--commit` pinned, so the checkout is exact and its Python dependencies install
hash-verified from that commit's `uv.lock`; the installer also fetches its own uv,
CPython, ffmpeg, node and ripgrep, which takes minutes per fresh Sandbox. The checkout
lives inside `HERMES_HOME`, `.hermes-ale`: placed elsewhere, the first run installs the
dependency environment again. The Anthropic dialect also installs the lockfile's
`anthropic` extra. On Windows the same commit's `install.ps1` runs from Git Bash with the
same choices and publishes `hermes.exe` in `.hermes-ale/bin`. Upstream supports native
Windows, so the Harness declares it, but the Windows path is unverified until a Windows
guest runs it.

`config.yaml` makes the episode Gateway the one custom provider, with Hermes' transport for
the Gateway dialect and the key as the `ALE_GATEWAY_TOKEN` variable, and turns off update
checks, the model catalog, shared telemetry, logins adopted from other agents and
installs during the run. Hermes still probes several model-information paths at start;
the Gateway answers them 404 and Hermes continues.

Each turn runs in a background `hermes gateway run` that the first chat starts and that
outlives it with that chat's environment, Gateway token included, so every invocation first
runs `hermes gateway stop` and the chat starts one of its own. A session freezes its route,
the Gateway URL among it, and `--resume` refuses a different `--model` or `--base-url`; a
continuing episode reaches it because its Gateway keeps the kept Sandbox's port.

Hermes keeps sessions in SQLite and chooses a new session's ID itself, which the
invocation's `result` event reports; `--resume` continues that exact session. After every
invocation `hermes sessions export` writes every session of the episode home to
`.hermes-ale/exports/<session id>/<time>.jsonl`. The parser reads the newest export, and
the invocation's stream-json supplies failures and any turn the export lacks. The export
holds every stored row with its `active` and `compacted` flags, and the parser keeps the
rows Hermes' transcript keeps. A row removed by a rewind, or superseded by a newer copy,
has neither flag. An in-place compaction keeps the turns it summarized as `compacted`
rows and stores its summary (`_compressed_summary`) and the messages it carries forward
again. The summary becomes a system step, a message stored more than once appears once,
and a message the summary was folded into follows it. When a compaction instead
continues the conversation in a new session, whose parent ended for `compression`, that
session's messages follow on in the same trajectory.

A session records its origin, never the call that caused it: a `delegate_task` child has
`parent_session_id` and an equal `_delegate_from` in its `model_config`. It becomes a
subagent trajectory, embedded in its spawner's, when its first message is the goal of
exactly one task the parent's calls name (from `tasks`, possibly sent as a JSON string,
or the single `goal`) and no other child of that parent starts with the same goal.
Every other delegated session under the episode's conversation stays raw evidence with an
`unlinked_subagent` issue; branched, reset and tool sessions are not subagents. MCP tools
are `mcp__<server>__<tool>` with other characters replaced by `_`; the declared server
name is restored from `config.yaml`. The settings are `max_turns` and `reasoning_effort`.
