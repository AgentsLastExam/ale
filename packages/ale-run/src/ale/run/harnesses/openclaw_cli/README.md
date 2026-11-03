# OpenClaw CLI

Runs `openclaw agent --local --agent main` with `--message-file`. ALE always uses agent
`main`, so the session is `.openclaw-ale/agents/main/sessions/<session id>.jsonl`. The
parser follows that file; without it, each turn keeps only the reply text the CLI
printed.

A `sessions_spawn` result names the session key of the child it started, and the session
index, `sessions.json`, maps that key to the child's session ID and to the key of the
session that spawned it. A child's first user message is the task as OpenClaw delegated
it. Any other transcript there is named in `parse_issues`; the session index, runtime
traces, locks and cached prompts beside them are not.

The CLI starts in `[agent].workdir` when a Task sets one, but its tools resolve paths against
its own workspace, `.openclaw-ale/workspace`, where its Skills live.

OpenClaw 2026.7.1 holds about 250–400 MB while it works and briefly reaches about 700 MB
as it starts and runs tools. The Linux base image's desktop already uses about 650 MB, so
in a 1 GiB Sandbox the kernel OOM-kills the CLI part-way through a turn; run it in at
least 2 GiB. ALE sets `OPENCLAW_NO_RESPAWN=1` because OpenClaw's launcher otherwise runs
the CLI as a second, detached Node process and exits 1 without a message when that child
dies from a signal. An OOM kill now reports exit code 137.
