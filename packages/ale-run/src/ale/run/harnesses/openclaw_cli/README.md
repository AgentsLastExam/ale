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

OpenClaw 2026.7.1 holds about 250–400 MB while it works and briefly reaches about 700 MB
as it starts and runs tools. The Linux base image's desktop already uses about 650 MB, so
in a 1 GiB Sandbox the kernel OOM-kills the CLI part-way through a turn; run it in at
least 2 GiB. ALE sets `OPENCLAW_NO_RESPAWN=1` because OpenClaw's launcher otherwise runs
the CLI as a second, detached Node process and exits 1 without a message when that child
dies from a signal. An OOM kill now reports exit code 137.

OpenClaw 2026.7.1 puts OpenAI's hosted `web_search` in a request only when the model's
base URL is `api.openai.com` itself, and no setting extends that to another URL, so behind
the Gateway it never does. Its `web_search` is then a local function whose backends
(Brave, Gemini, Perplexity and others) each need a key of their own, which a Sandbox never
holds, so it reports that no provider is available, in `open` Tasks too. `x_search` would
call xAI from the Sandbox with an xAI key and is not offered without one; `web_fetch`
fetches from inside the Sandbox. All of them stay under the Sandbox's network policy.
