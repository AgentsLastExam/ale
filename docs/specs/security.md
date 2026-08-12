# Security model

What this engine guarantees about a result, how, and — just as importantly — what it
does not guarantee. The last section is the one worth reading first if you are deciding
whether to run untrusted code here.

## What the guarantees are for

Every claim below exists to protect the **integrity of a number**. An agent that reached
undeclared services or read the answer key is not a cheating agent so much as a result
that means nothing. Spending a declared subscription credential is allowed when the Run
records that authentication mode. The threat being modelled is a benchmark that silently
stops measuring what it says it measures.

## 1. A sandbox has only declared ways out

Default is `network.mode: block`. Docker uses a per-episode `--internal` bridge with no
route off the host. QEMU uses guest and runner deny-all rules, forwarding only the
episode's framework-owned service ports.

This is why the address is read from the network rather than assumed: Docker's
`host-gateway` alias does not work on an internal bridge, and a wrong assumption here
would look like a broken run rather than a broken guarantee.

`allowlist` preserves that deny-all topology. Docker reaches the host proxy over its
internal bridge. QEMU DNATs the episode Gateway and proxy ports through the runner.
Agent and oracle commands receive authenticated proxy variables only after the sandbox
is sealed; direct traffic that ignores them remains blocked.

API-key Harness mode reaches its model only through the Gateway. A Harness contract may
instead declare native subscription mode, in which case ALE adds that provider's pinned
authentication and model hosts to the episode proxy policy and records that effective
egress separately from Task-declared hosts.

`open` is a deliberate escape hatch: an ordinary bridge with normal egress. A task must
declare it, and the declaration is recorded in provenance.

Verified by `tests/integration/test_gateway_isolation.py` and by the `demo-netprobe`
task, which attempts DNS resolution and a raw TCP connection from inside a sandbox and
fails admission if either succeeds.

## 2. Egress is a decision made in our code

Two host-side services, one token:

- **The model gateway** serves API-key Harness mode. It swaps in the real credential,
  imposes the model, enforces ceilings by refusing the next call, and records every call
  including the ones that failed upstream.
- **The egress proxy** serves `CONNECT` for hosts a task declared, and refuses
  everything else with a 403 naming what was declared.

Both authenticate the same per-episode token and share one session registry. The Gateway
uses bearer authentication; standard proxy clients send the same token through Basic
proxy authentication.

In Codex/Grok native subscription mode the provider CLI runs in the Sandbox, reads its
staged provider profile, and reaches only the Harness-declared provider hosts through the
egress proxy. Those TLS tunnels are opaque, so Gateway model enforcement, accounting, and
Transport Trace are unavailable and provenance says so. Claude subscription mode instead
uses the model Gateway with an episode token and Host-owned OAuth credential. It retains
Gateway model authority and retry coalescing but records no provider billing claim or
Transport Trace.

Allowlisting is enforced here rather than with `iptables` on the host or a filtering tool
inside the image, for two reasons: it needs no privilege we would otherwise not require,
and it does not depend on tooling in an image we did not build. **Traffic that ignores
the proxy variables has nowhere to go**, so the failure mode is closed.

Matching is on the requested name, with subdomains of a declared host included. It is
deliberately *not* on a resolved address: a task declares `pypi.org` because that is what
it means, and pinning to whatever IP that resolved to once would break the task rather
than tighten it.

## 3. Credential exposure follows the selected Harness contract

In API-key mode the provider key lives in the host's gitignored `.env`. The Sandbox
receives a URL and a bearer token that is worthless anywhere else and is revoked when the
episode ends — verified by `test_a_revoked_session_loses_its_egress`.

Native subscription mode deliberately stages the selected official CLI profile or token
under the episode's agent home. The evaluated agent can read and copy that reusable
credential; ALE does not claim otherwise. This mode is suitable only when the operator
accepts that exposure and is authorized to use the selected subscription. ALE does not
put raw credentials in framework-authored result, trace, or RunLock fields and removes
the episode copy during ordinary Harness cleanup. Agent-authored output and Task-declared
artifacts can contain anything the agent chose to copy, including that credential.

Verification is a separate trusted phase after solver exit. `[verification.llm]` and
`[verification.agent]` name an environment variable, model, endpoint, and reasoning
effort at run level. ALE resolves the key only for `verify/run.sh`, injects it only into
that command environment, registers the value for output/transcript redaction, and
removes the sandbox after verification. The key is absent from provisioning, setup, the
solver environment, staged configuration, result, record, and RunLock.

LLM and Agent Judges call their configured endpoints directly from the active
verification sandbox, whether shared with the solver or separate. There is no
Verification Service, Host callback, or Judge Gateway session.
This deliberately trusts reviewed Task verifier code with the configured Judge
credential; a malicious Task author is outside this threat model.

## 4. The agent cannot change what it is measured under

The agent runs as an unprivileged account the image declares, and so does the oracle that
stands in for it during validation. The framework's own work — installing the guest
service, staging content, running the task's stages, collecting artifacts — runs as root.

This is what stops an agent rewriting the network policy that isolates it, the clock its
timeouts are measured against, or the guest service driving its own sandbox. None of that
needs malice to matter: a result obtained under conditions the subject could alter is not
a measurement.

The oracle sharing the agent's limits is the load-bearing half. `ale validate` is the only
check a task gets before publication, and an oracle with more privilege would pass exactly
the tasks a real agent then fails on access alone.

A task may declare `resources.sudo` when it genuinely needs to install software or change
system configuration. The grant is verified rather than assumed — writing a sudoers rule
succeeds in an image with no `sudo` binary — and recorded in provenance, because an
episode run that way was less isolated and the two should not be compared without that
being visible.

Verified by `tests/integration/test_identity.py`: the agent cannot read the guest service,
cannot write system paths, and cannot elevate unless its task asked.

## 5. Answers are absent, not hidden

`verify/` and `oracle/`, including their optional `assets/` directories, are copied into a sandbox
only for their own phases. While the evaluated agent works they are not access-controlled
— they are not there. In separate mode only the immutable declared artifact snapshot is
restored; the solver filesystem is not transferred wholesale.

This is why there is no "secret" flag on an asset. A flag is a promise about behaviour
that some future code path could forget to check; a file that was never uploaded cannot
be read by anything.

Verified by `test_agent_never_sees_verification_material`, whose setup probe exits
non-zero if any scoring path exists during setup.

## 6. Limits end episodes rather than stalling them

In API-key mode token, cost and model-call ceilings are enforced by the Gateway refusing
the next call with a 429. Subscription traffic is not subject to those limits (Claude's
Gateway use is relay-only), so only native Harness limits and phase deadlines apply;
unavailable Gateway limits and cost are recorded honestly. Teardown is
cancellation-shielded, so a killed phase still reclaims
its container.

Every terminal state is one typed status, and failures never enter score aggregates. A
`task_error` — a crashed or silent verifier — is deliberately distinct from a zero score,
because conflating them makes a broken task look like a hard one.

Verified by `tests/integration/test_limits.py`, which asserts prompt typed termination
and that no container outlives its episode.

Judge failure follows the same rule. Missing credentials/configuration, provider
failures, timeouts, refusals, malformed JSON, incomplete rubric coverage, and off-menu
choices remain typed failed episodes. ALE never records scorer failure as an evaluated
agent reward of zero.

## 7. A result carries what produced it

`RunLock` records the explicit Task name, complete
Task-folder source digest, source and commit when available, exact locally built image
content, concrete Provider, requested and effective resources, GPU allocation and sandbox
observations, agent version and integrity, harness family, one optional Task-asset
repository/path/commit/dirty observation, verification topology and image/resources,
sandbox lifecycle outcomes, config hash, seed, and the engine's own commit.

A run whose lock cannot back a reportable result says so. `--require-reportable` turns
that statement into a non-zero exit for CI. Final Task-image publication is not part of
the current contract; future delivery may add it without changing the self-contained
Task source contract.

## 8. Agent resources are declared, not discovered

ALE never copies ambient host agent state. Host Skills and MCP configuration are outside
the resource model. A subscription profile is copied only when the Run's resolved
Harness authentication mode explicitly selects that provider; it is not a Task resource.

Only the effective union declared by the Task, harness preset, Run file, and CLI is
staged. Task paths cannot escape `tools/skills/` or `tools/mcp/`. Same-name resources
with different digests fail before provisioning. Task stdio MCP descriptors and adjacent
code are staged below the agent home; `{mcp}` resolves to that private resource directory.

Local stdio MCP commands execute inside the sandbox and are checked after files and
Skills are staged. Remote MCP is limited to unauthenticated Streamable HTTP endpoints
whose host satisfies the Task network policy. Native Harness subscription credentials do
not make arbitrary remote MCP authentication implicit; that remains a separate contract.

`cua-desktop` follows the same opt-in MCP path. It is not automatically injected.

## 9. Native continuation stays in one live sandbox

A native continuation binds the harness, model, validated settings, effective resource
digest, episode ID, sandbox ID, and exact native session ID. Resume sends only the new
instruction and selects that exact session.

Continuation fails when the sandbox was destroyed, the native state is absent, any bound
input changed, or the request names another episode or sandbox. There is no "latest
session" selector, transcript replay fallback, or configurable disk/global resume scope.

## 10. Root agent judges do not redefine solver output

An agent judge runs as framework/root so it can inspect and test the completed sandbox.
That privilege means it can mutate any guest byte, so the integrity boundary is on the
host: declared solver artifacts, native logs, hashes, and canonical solver trajectory are
finalized before verify. Deterministic criteria accepted before agent launch are
immutable, only the active agent verdict may be added afterward, and post-judge sandbox
bytes are never published as solver artifacts.

## 11. Debug retention is explicit and sanitized

Run configuration, not Task content, decides whether solver and verifier sandboxes are
destroyed or retained. Default is destroy. Before a kept sandbox receives a durable
handle, Harness cleanup and required evidence capture finish, known verification
credentials are removed, and episode capabilities are revoked. Harness cleanup normally
removes a staged subscription profile, but credential confidentiality is not a retention
gate; a failed removal is recorded and the operator is warned rather than converting an
otherwise valid result into `retention-failed`.

Every Docker sandbox and QEMU runner is labeled with ALE ownership, episode, role,
requested retention, and allocated GPU identity. Retained resources remain discoverable
through `ale sandbox list` and removable only through a validated ALE-managed handle.
Live labels also prevent a retained GPU from being assigned to another ALE sandbox.
Retention does not change rewards or verification records.

---

## What is **not** defended against

Stating these plainly is part of the model. Treating an unlisted gap as covered is how
security claims rot.

**A malicious task author.** A Task folder's `setup/run.sh` and `verify/run.sh` run as root
in the sandbox, and a Task may declare `resources.sudo` for its agent. Tasks are reviewed
content, not untrusted input; the identity model protects a result from its *agent*, not a
host from its Task. The gates that exist (`ale lint`, `ale validate`, CI, PR review) are
aimed at *broken* Tasks, not hostile ones. A malicious verifier can also read and
exfiltrate a Judge credential injected for its direct provider call.

**Container escape.** A container sandbox uses Docker's default isolation. A kernel
exploit reaches the host. Use the VM backend for stronger isolation, and still do not put
anything on that host you would mind losing.

**Traffic analysis, or what is said over an allowlisted tunnel.** Once `CONNECT`
succeeds the tunnel is opaque by design — the point is to reach a declared host, not to
inspect the conversation. An allowlisted host is a trusted host.

**Exfiltration through the model service.** An agent can put anything it likes in a
prompt. The Gateway records API-key calls but does not police their content. Subscription
traffic has no retained Transport Trace: Codex/Grok use an opaque TLS tunnel, while the
Claude relay still sees and controls requests in memory.

**A compromised image.** The exact locally built Task-image content is recorded in
provenance, so a change is *detectable*. Nothing verifies that its base or authored
Dockerfile was benign to begin with.

**Resource exhaustion by a co-tenant.** CPU, memory, writable storage, and ALE-managed
physical GPUs are admitted per sandbox or rejected. Network bandwidth and resources
consumed by processes outside ALE are not reserved; concurrent workloads can still starve
each other.

**Side channels between concurrent episodes.** Sandboxes are separate containers on
separate bridges, but they share a kernel, a page cache and a clock.
