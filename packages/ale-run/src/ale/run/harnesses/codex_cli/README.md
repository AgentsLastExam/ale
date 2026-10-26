# Codex CLI

Runs `codex exec --json`, or `codex exec resume <thread id>`, with `-` reading the prompt
from stdin. The rollout lives in `.codex-ale/sessions/**/rollout-<time>-<thread id>.jsonl`.

The parser follows the rollout. Its user messages include context Codex injects, so a
turn begins only at the exact prompt ALE recorded for it. MCP calls take their real
server name and structured result from the matching stdout event, matched on server,
tool, and arguments. Child threads are collected but not merged into the parent.
