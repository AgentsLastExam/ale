# Harnesses

A Harness adapts one agent program to ALE. Autonomous Harnesses launch the program
inside the Task Sandbox and convert its native evidence into ATIF. Policy Harnesses run
an agent-owned rollout against ALE's stepwise Sandbox environment.

| Harness | Family | Gateway dialect | Native resume | Subscription |
|---|---|---|---|---|
| `claude-code` | autonomous | Anthropic Messages | yes | yes |
| `codex-cli` | autonomous | OpenAI Responses | yes | yes |
| `grok-build` | autonomous | configured Responses dialect | yes | yes |
| `openclaw-cli` | autonomous | OpenAI Responses | yes | no |
| `computer-use` | policy | Anthropic Messages | no | no |
| `oracle`, `nop` | built in | none | no | no |

Every autonomous Harness keeps provider-specific configuration local, but follows the
same lifecycle:

1. validate and install the pinned program;
2. translate declared Skills and MCP servers;
3. send model calls through the episode Gateway token;
4. launch as the image-declared agent user;
5. periodically collect CLI output and deterministically convert it to ATIF;
6. bind native continuation to the original Sandbox and effective configuration.

API-key and subscription runs generate the same Sandbox-side Gateway configuration.
Only the Host Gateway's upstream authentication changes. See
[`../../../../../../docs/guides/subscription-auth.md`](../../../../../../docs/guides/subscription-auth.md).

The normative integration contract is
[`../../../../../../docs/specs/autonomous-harness.md`](../../../../../../docs/specs/autonomous-harness.md).


The four native CLI packages each contain `adapter.py`, `settings.py`, `trajectory.py`,
and a README naming their source files and limitations. `base.py` owns shared CLI
execution and same-Sandbox continuation checks; each adapter implements only command
preparation and native result interpretation in addition to installation/resources
and its pure trajectory parser.

The base writes the complete UTF-8 instruction to `instruction.md`. Grok and OpenClaw
receive its path through their native prompt-file options; Claude and Codex receive a
fixed ASCII instruction to read that file. The task text never travels through a
command argument, environment variable, or stdin.

`logs.py` incrementally collects separate stdout/stderr and invocation boundaries.
Native session directories are an optional source (`logging.collect_native_logs`,
default `false`). Enabling them collects each adapter's declared directories using
verified append behavior or snapshots. The parser records its evidence scope and
limitations; stdout-only collection does not claim complete native conversation history.
Collected session files outside a parser's coverage are reported in `parse_issues`,
which preserves their raw evidence even under minimal retention.
