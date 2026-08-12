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
5. retain native evidence and deterministically convert it to ATIF;
6. bind native continuation to the original Sandbox and effective configuration.

API-key and subscription runs generate the same Sandbox-side Gateway configuration.
Only the Host Gateway's upstream authentication changes. See
[`../../../../../../docs/guides/subscription-auth.md`](../../../../../../docs/guides/subscription-auth.md).

The normative integration contract is
[`../../../../../../docs/specs/autonomous-harness.md`](../../../../../../docs/specs/autonomous-harness.md).
