# Claude Code

Pinned default: `2.1.227`. Installation, native configuration, resource translation,
and terminal error handling live in `adapter.py`; settings are in `settings.py`;
pure evidence conversion is in `trajectory.py`.

The CLI receives the fixed instruction-file prompt on stdin. The complete task instruction is written to `instruction.md` by the shared base.

`CliHarness` supplies agent identity, separate stdout/stderr, per-invocation byte
boundaries, and exact same-Sandbox continuation checks. Each invocation appends to
`.ale-harness/stdout.log` and `.ale-harness/stderr.log`; bookkeeping lives beside them.
A failed command does not need a successful post-exit export to leave evidence.

Native session source: `.claude-config/projects`. Native transfer is optional and disabled by default
(`logging.collect_native_logs = false`). When enabled, the common collector preserves
home-relative paths below the Host `logs/<harness>/native/` directory.

Native transcript files can be compacted or rewritten by the pinned CLI, so collection replaces snapshots rather than tailing by byte offset.

The parser uses stream-json by default. With native collection enabled it selects the exact root session and supplements the stream with native messages. Child files are converted only when their recorded parent tool call is identifiable; unresolved children remain raw evidence and produce a parse issue.

Continuation requires the original live Sandbox and matching effective configuration.
Keeping the Sandbox preserves native state; it does not renew Gateway authorization.
