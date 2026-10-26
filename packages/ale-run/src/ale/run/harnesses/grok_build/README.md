# Grok Build

Runs Grok headless with `--prompt-file`. A session is a directory named by its ID below
`.grok-ale/sessions`, holding `chat_history.jsonl` and `updates.jsonl`; the parser joins
the two and records tool failures from the updates. Other session directories stay raw
evidence and are not guessed to be children.
