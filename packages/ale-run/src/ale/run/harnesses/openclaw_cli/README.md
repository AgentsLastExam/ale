# OpenClaw CLI

Runs `openclaw agent --local --agent main` with `--message-file`. ALE always uses agent
`main`, so the session is `.openclaw-ale/agents/main/sessions/<session id>.jsonl`. The
parser follows that file; without it, each turn keeps only the reply text the CLI
printed.

A `sessions_spawn` result names the session key of the child it started, and the session
index, `sessions.json`, maps that key to the child's session ID and to the key of the
session that spawned it. A child linked both ways becomes a subagent trajectory
referenced from that call; its first user message is the task as OpenClaw delegated it.
Any other transcript there is named in `parse_issues`; the session index, runtime
traces, locks and cached prompts beside them are not.
