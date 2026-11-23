# Grok Build

Runs Grok headless with `--prompt-file`. A session is a directory named by its ID below
`.grok-orbit/sessions`, holding `chat_history.jsonl` and `updates.jsonl`; the parser joins
the two and records tool failures from the updates. Other session directories are left
unconverted, named in `parse_issues`, and are not guessed to be children.

Grok gives the model an MCP tool's image as data-URL text in the tool's output. It splits
images out only from an output over its MCP output limit, which it first truncates, writing
the whole output to the session's `mcp/<call id>.txt`; the truncated image fails Grok's
own check and is dropped. So the model never sees an MCP screenshot, such as
`cua-desktop`'s. Grok takes every model to accept images and has no setting for it.

Grok's `web_search` is a request of its own carrying xAI's hosted `web_search` tool, sent
to the model `[models] web_search` names, `grok-4.5` unless set. The generated config names
the run's model, so the request goes through the Gateway. Unless the Task's network is
`open`, Orbit passes `--disable-web-search`, which removes it and `web_fetch`. `web_fetch`
is off in Grok by default and fetches from inside the Sandbox when on.
