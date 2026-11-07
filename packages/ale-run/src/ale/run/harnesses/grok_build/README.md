# Grok Build

Runs Grok headless with `--prompt-file`. A session is a directory named by its ID below
`.grok-ale/sessions`, holding `chat_history.jsonl` and `updates.jsonl`; the parser joins
the two and records tool failures from the updates. Other session directories are left
unconverted, named in `parse_issues`, and are not guessed to be children.

Grok gives the model an MCP tool's image as data-URL text in the tool's output. It splits
images out only from an output over its MCP output limit, which it first truncates, writing
the whole output to the session's `mcp/<call id>.txt`; the truncated image fails Grok's
own check and is dropped. So the model never sees an MCP screenshot, such as
`cua-desktop`'s. Grok takes every model to accept images and has no setting for it.
