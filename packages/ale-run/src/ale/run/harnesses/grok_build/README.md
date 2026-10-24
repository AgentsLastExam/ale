# Grok Build

Pinned default: `0.2.112`. Installation, native configuration, resource translation,
and terminal error handling live in `adapter.py`; settings are in `settings.py`;
pure evidence conversion is in `trajectory.py`.

The CLI receives `--prompt-file <home>/instruction.md`. No task text is placed in argv, environment variables, or stdin.

`CliHarness` supplies agent identity, separate stdout/stderr, per-invocation byte
boundaries, and exact same-Sandbox continuation checks. Each invocation appends to
`.ale-harness/stdout.log` and `.ale-harness/stderr.log`; bookkeeping lives beside them.
A failed command does not need a successful post-exit export to leave evidence.

Native session source: `.grok-ale/sessions`. Native transfer is optional and disabled by default
(`logging.collect_native_logs = false`). When enabled, the common collector preserves
home-relative paths below the Host `logs/<harness>/native/` directory.

Only `**/updates.jsonl` is declared append-only. Native chat history can be compacted and replaced through a temporary file, so chat history uses snapshots.

With native collection disabled, the parser retains stdout text and reports that native tool/conversation records are unavailable. With collection enabled it selects the exact session directory and combines chat_history.jsonl with updates.jsonl. Other session directories remain raw evidence; they are not guessed to be children.

Continuation requires the original live Sandbox and matching effective configuration.
Keeping the Sandbox preserves native state; it does not renew Gateway authorization.
