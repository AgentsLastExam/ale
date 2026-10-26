# OpenClaw CLI

Runs `openclaw agent --local --agent main` with `--message-file`. ALE always uses agent
`main`, so the session is `.openclaw-ale/agents/main/sessions/<session id>.jsonl`. The
parser follows that file; without it, each turn keeps only the reply text the CLI
printed. Any other transcript there, such as a subagent's, is named in `parse_issues`;
the session index, runtime traces, locks and cached prompts beside them are not.
