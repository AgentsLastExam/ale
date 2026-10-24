# Codex CLI

Pinned default: `0.146.0`. Installation, native configuration, resource translation,
and terminal error handling live in `adapter.py`; settings are in `settings.py`;
pure evidence conversion is in `trajectory.py`.

The CLI receives only the fixed ASCII prompt naming `instruction.md`; the shared base writes the full UTF-8 instruction to that file.

`CliHarness` supplies agent identity, separate stdout/stderr, per-invocation byte
boundaries, and exact same-Sandbox continuation checks. Each invocation appends to
`.ale-harness/stdout.log` and `.ale-harness/stderr.log`; bookkeeping lives beside them.
A failed command does not need a successful post-exit export to leave evidence.

Native session source: `.codex-ale/sessions`. Native transfer is optional and disabled by default
(`logging.collect_native_logs = false`). When enabled, the common collector preserves
home-relative paths below the Host `logs/<harness>/native/` directory.

The pinned official rollout recorder opens its JSONL writer in append mode, writes newline-delimited records, and flushes them. `**/*.jsonl` is therefore transferred by byte offset.

The parser uses command JSON events by default. With native collection enabled it selects the root by exact `session_meta.payload.id`, then uses its tool records. Child session files are collected, but their records are not merged into the parent conversation or represented as complete child trajectories by this parser.

Continuation requires the original live Sandbox and matching effective configuration.
Keeping the Sandbox preserves native state; it does not renew Gateway authorization.
