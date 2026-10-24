# OpenClaw CLI

Pinned default: `2026.7.1`. Installation, native configuration, resource translation,
and terminal error handling live in `adapter.py`; settings are in `settings.py`;
pure evidence conversion is in `trajectory.py`.

The CLI receives `--message-file <home>/instruction.md`. The shared base writes the full UTF-8 task instruction to that file.

`CliHarness` supplies agent identity, separate stdout/stderr, per-invocation byte
boundaries, and exact same-Sandbox continuation checks. Each invocation appends to
`.ale-harness/stdout.log` and `.ale-harness/stderr.log`; bookkeeping lives beside them.
A failed command does not need a successful post-exit export to leave evidence.

Native session source: `.openclaw-ale/agents/*/sessions`. Native transfer is optional and disabled by default
(`logging.collect_native_logs = false`). When enabled, the common collector preserves
home-relative paths below the Host `logs/<harness>/native/` directory.

Native write semantics have not been established as append-only, so all native files use snapshots.

With native collection disabled, only CLI response summaries are available; the trajectory explicitly records that tool history is absent. With collection enabled the parser reads the exact root session file, preserving messages and tool results. Other agents' session files are retained without inventing parent/child relationships.

Continuation requires the original live Sandbox and matching effective configuration.
Keeping the Sandbox preserves native state; it does not renew Gateway authorization.
