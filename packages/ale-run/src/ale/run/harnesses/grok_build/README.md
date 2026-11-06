# Grok Build

Runs Grok headless with `--prompt-file`. A session is a directory named by its ID below
`.grok-ale/sessions`, holding `chat_history.jsonl` and `updates.jsonl`; the parser joins
the two and records tool failures from the updates. Other session directories are left
unconverted, named in `parse_issues`, and are not guessed to be children.

Grok takes every model to accept images and has no setting for it.
