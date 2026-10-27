# Grok Build

Runs Grok headless with `--prompt-file`. A session is a directory named by its ID below
`.grok-ale/sessions`, holding `chat_history.jsonl` and `updates.jsonl`; the parser joins
the two and records tool failures from the updates. Other session directories stay raw
evidence and are not guessed to be children.

Grok's `web_search` is a request of its own carrying xAI's hosted `web_search` tool, sent
to the model `[models] web_search` names, `grok-4.5` unless set. The generated config names
the run's model, so the request goes through the Gateway. Unless the Task's network is
`open`, ALE passes `--disable-web-search`, which removes it and `web_fetch`. `web_fetch`
is off in Grok by default and fetches from inside the Sandbox when on.
