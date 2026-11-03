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

Under `[agent].network_mode = "no-network"` (`block` in the RunLock), Docker uses a
per-episode `--internal` bridge with no route off the host. QEMU enforces deny-all in the
runner network namespace, outside the guest OS, and forwards only the episode's
framework-owned service ports.

No route off the host is not the same as no way out: the host's own address on the
bridge answers on every port a host service binds to all interfaces, such as sshd, a
database, or another run's Gateway. Both backends therefore admit only the episode's own
Gateway and proxy ports on that address. Docker applies the rule in the sandbox's network
namespace from a short-lived helper container holding `NET_ADMIN`, a capability the
sandbox itself never has, so not even root inside it can lift the rule; provisioning fails
if the rule cannot be applied. QEMU drops forwarded guest traffic in the runner while the
sandbox is sealed and accepts only replies and the DNATed episode ports. The guest OS has
no firewall of its own: enforcement lives where the agent cannot reach it, and the
framework's `open_egress` for setup and verification is not undone by rules inside the
guest.

This is why the address is read from the network rather than assumed: Docker's
`host-gateway` alias does not work on an internal bridge, and a wrong assumption here
would look like a broken run rather than a broken guarantee.

`allowlist` preserves that deny-all topology. Docker reaches the host proxy over its
internal bridge. QEMU DNATs the episode Gateway and proxy ports through the runner.
Agent and oracle commands receive authenticated proxy variables only after the sandbox
is sealed; direct traffic that ignores them remains blocked.

Every supported Harness mode reaches its model only through the Gateway. Subscription
provider hosts are Host-side upstreams and never widen the Sandbox's Task-declared
egress policy.

Daytona has neither bridge nor runner. It enforces `block` (`networkBlockAll`) and
`allowlist` (its own domain allowlist) outside the sandbox, where the agent cannot lift
them, so allowed traffic goes straight from the cloud to its host instead of through the
Host. Daytona's allowlist matches names with their subdomains, as the proxy does, but also
passes plain HTTP and other ports of an allowed host. It refuses every connection from the
sandbox to the Host, so the Host carries the Gateway in over SSH, and the sandbox can name
only the Gateway, never an address.

`open` is a deliberate escape hatch: an ordinary bridge with normal egress. A task must
declare it, and the declaration is recorded in provenance.

Verified by `tests/integration/test_gateway_isolation.py`, including that a sealed
sandbox reaches no other host port, by the QEMU Provider unit tests for the runner rules,
and by the `demo-netprobe` task, which attempts DNS resolution and a raw TCP connection
from inside a sandbox and fails admission if either succeeds.

## 2. Egress is a decision made in our code

Two host-side services, one token:

- **The model gateway** serves API-key and subscription Harness modes. It swaps in the
  selected Host credential, imposes the model, enforces ceilings by refusing the next
  call, and records every call including the ones that failed upstream. A `GET` for a
  model catalog, at any path ending in `/models`, lists only the imposed model, so a
  CLI that fetches one before its first call sees what its calls will receive.
- **The egress proxy** serves `CONNECT` for hosts a task declared, and refuses
  everything else with a 403 naming what was declared.

Both authenticate the same per-episode token and share one session registry. The Gateway
reads it wherever the dialect's clients send their key, such as a bearer header or the
dialect's own key header; standard proxy clients send it through Basic proxy
authentication.

The Gateway forwards only calls it records and that leave nothing stored with the
provider:

- **The model is imposed wherever the dialect names it**, in the body or in the URL, on
  every call, a CLI's own helper-model calls included: a run that also sampled a second
  model would not measure what it says it does.
- **Nothing that stores state with the provider is forwarded**, such as uploaded files or
  explicitly cached content. Such an object lives under the Host's provider account, not
  the episode, so it would:
  - be visible to, and usable by, any later episode on the same credential, so episodes
    would no longer be independent;
  - outlive the episode, leaving ALE to track and delete it and a crashed run to leave it
    behind;
  - consume storage that the Gateway's call and token records do not measure.

  The provider's automatic prompt caching is unaffected: it exposes nothing to reuse and
  is recorded as cache-read tokens. Each dialect forwards only the methods it lists and
  refuses the rest in the provider's error format, so an unchecked method cannot slip
  through.
- **Provider-side network tools follow the Task's network policy.** These are tools the
  provider runs on its own side that reach the web, such as search grounding. They act
  outside the Sandbox's network policy, so a request that asks for one is forwarded only
  for a Task whose network is `open`.

A policy refusal is recorded as a `refused` call naming the policy and, unlike a limit,
does not end the episode. Each dialect's routes, authentication and policies are in the
[Gateway README](../../packages/ale-run/src/ale/run/gateway/README.md).

In subscription mode the provider CLI still runs in the Sandbox, but it receives only
the episode token and model Gateway URL. The Host Gateway reads checkout-local auth,
injects provider-required headers, and retains model enforcement, accounting, limits,
retry coalescing, and Transport Trace. Provider billing and account quota remain
provider-owned observations.

Allowlisting is enforced here rather than with `iptables` on the host or a filtering tool
inside the image, for two reasons: it needs no privilege we would otherwise not require,
and it does not depend on tooling in an image we did not build. **Traffic that ignores
the proxy variables has nowhere to go**, so the failure mode is closed. Daytona's own
allowlist takes this role there, for the same two reasons (section 1).

Matching is on the requested name, with subdomains of a declared host included. It is
deliberately *not* on a resolved address: a task declares `pypi.org` because that is what
it means, and pinning to whatever IP that resolved to once would break the task rather
than tighten it.

## 3. Provider credentials stay on the Host

In API-key mode the provider key lives in the host's gitignored `.env`, which `ale` reads
once when it starts, for every command; a variable already set in the environment (an
explicit `KEY=… ale run`, or a CI secret) takes precedence. Every credential and endpoint
ALE reads carries an `ALE_` prefix, so a checkout's values and the variables the
operator's own tools read, such as `ANTHROPIC_BASE_URL` for their own Claude Code, never
override each other.

The upstream follows the model provider. A Harness preset names it with
`gateway.provider`, and the Gateway sends
`ALE_<PROVIDER>_API_KEY` to `ALE_<PROVIDER>_BASE_URL`, or to the provider's own API when
that URL is unset. Both names derive from the provider so that a key is only ever paired
with its own provider's endpoint, and a run that changes provider cannot carry another
provider's key along. `--base-url` and `--api-key-env` (`gateway.base_url`,
`gateway.api_key_env`) override this for one run; an endpoint other than the provider's
must name the variable holding its key, by name rather than value, so the key stays out of
shell history and process listings.

The Sandbox receives a URL and a bearer token that is worthless anywhere else and is
revoked when the episode ends — verified by `test_a_revoked_session_loses_its_egress`.

Subscription access and refresh credentials remain in checkout-local Host state:
`ALE_CLAUDE_CODE_OAUTH_TOKEN` in `.env`, and `.ale/auth/<harness>/auth.json` for Codex and
Grok. They go only to the provider's own subscription endpoint, never to
`ALE_<PROVIDER>_BASE_URL`. The Gateway replaces the episode token with current provider
authorization only after authenticating the session. Framework-authored result, trace,
and RunLock fields contain neither raw credential nor profile path.

Verification is a separate trusted phase after solver exit. `[verification.llm]` and
`[verification.agent]` name a model provider, model and reasoning effort at run level, and
the LLM Judge also its dialect; their keys and endpoints resolve by the same provider rule
as the Gateway's. Each episode opens one Gateway session per configured Judge holding that
key. The verifier receives only the Gateway's address and the session's token, which ALE
injects only for the OS-specific verification entry, registers for output/transcript
redaction, and revokes when the episode ends. The key itself never enters a Sandbox and is
absent from provisioning, setup, the solver and verification environments, staged
configuration, result, record, and RunLock.

This deliberately trusts reviewed Task verifier code with the episode's Judge sessions; a
malicious Task author is outside this threat model.

The one credential a Sandbox can receive beyond episode tokens is one its Task declares in an
`env` table, such as a token for a service the Task itself uses
([Task authoring](task-authoring.md#variables)). It comes from the checkout's `.env` only,
reaches only the commands its table names, is redacted from every record, and can neither
refer to an `ALE_` variable nor equal a Host credential, so no model or Judge key leaves the
Gateway this way. `[environment].env` reaches the agent, and the agent can read it.

## 4. The agent cannot change what it is measured under

The agent runs as the account the image declares, and so does the oracle that stands in
for it during validation. Framework work runs as root on Linux and Administrator on
Windows. Windows guestd stays in the interactive session but launches agent commands
with the desktop's limited token. Only the Host's SMBIOS `ale-sudo` marker, supplied for
`request.sudo`, permits elevated agent commands. The Provider verifies both tokens before
Task code runs; image builds request elevation explicitly. Material visibility still
follows phase ordering. See [Permissions](sandbox-image.md#permissions).

This is what stops an agent rewriting the network policy that isolates it, the clock its
timeouts are measured against, or the guest service driving its own sandbox. None of that
needs malice to matter: a result obtained under conditions the subject could alter is not
a measurement.

The oracle sharing the agent's limits is the load-bearing half. `ale validate` is the only
check a task gets before publication, and an oracle with more privilege would pass exactly
the tasks a real agent then fails on access alone.

A task may declare `[agent].sudo` when it genuinely needs to install software or change
system configuration. The grant is verified rather than assumed — writing a sudoers rule
succeeds in an image with no `sudo` binary — and recorded in provenance, because an
episode run that way was less isolated and the two should not be compared without that
being visible.

A VM guest service listens on TCP, which the agent can reach over loopback and every Host
account can reach through the forwarded port. Each connection must therefore first present
the sandbox's token, which the framework's first connection to the freshly booted guest
claims; anything else is refused before an operation runs. The claim is sound because
nothing but trusted image services runs in a cold-booted guest before that connection.
Linux keeps the claimed token in root-only `/run/ale/guestd-token`, so a restarted guest
service stays claimed while a reboot or a saved disk carries none; Windows keeps it in
memory. Container guest services are `docker exec` sessions with no listener.

Daytona's in-sandbox daemon runs commands for anyone who connects, as root. The Provider
blocks every non-root user from it and proves that as the agent before handing the
sandbox out; an agent granted `[agent].sudo` can lift the block, as it can any rule.

The verifier writes its result to Harbor's `/logs/verifier` (`C:\logs\verifier`), a path the
agent can create beforehand: with sudo on Linux, as any account on Windows. Before every
verification attempt ALE therefore removes a link or file at either level instead of
following it, makes the parent the framework's and writable by it alone, and recreates the
verifier directory empty and accessible only to the framework. A result the agent planted
is gone, and nothing it left running can write or redirect one.

Verified by `tests/integration/test_identity.py`: the agent cannot read the guest service,
cannot write system paths, and cannot elevate unless its task asked; by
`tests/unit/test_guest_authentication.py` for the claimed TCP guest service; and by
`test_an_agent_cannot_plant_or_redirect_a_result` for the verifier directory.

## 5. Answers are absent, not hidden

`tests/` and `solution/` are copied into a sandbox only for their own phases. While the evaluated agent works they are not access-controlled
— they are not there. In separate mode only the immutable declared artifact snapshot is
restored; the solver filesystem is not transferred wholesale.

This is why there is no "secret" flag on a Task file. A flag is a promise about behaviour
that some future code path could forget to check; a file that was never uploaded cannot
be read by anything.

Verified by `test_agent_never_sees_verification_material`, whose setup probe exits
non-zero if any scoring path exists during setup.

## 6. Limits end episodes rather than stalling them

API-key and subscription token and model-call ceilings are enforced by the Gateway
refusing the next call with an error the client does not retry, a 429 unless a dialect's
clients retry that regardless, and an agent phase that fails after a refusal ends as
`budget_exceeded` for every Harness and authentication mode. Some subscription endpoints expose less request-side
control; in particular, Codex rejects `max_output_tokens`, so ALE accounts the completed
response and refuses a later call after the ceiling. Harness-native limits and phase
deadlines remain additional layers. Teardown is cancellation-shielded, so a killed phase
still reclaims its sandbox.

Every terminal state is one typed status, and failures never enter score aggregates. A
`task_error` — a crashed or silent verifier — is deliberately distinct from a zero score,
because conflating them makes a broken task look like a hard one.

Verified by `tests/integration/test_limits.py`, which asserts prompt typed termination
and that no container outlives its episode.

Judge failure follows the same rule. Missing credentials/configuration, provider
failures, timeouts, refusals and invalid structured responses raise exceptions. Unhandled
exceptions fail the episode; ALE never invents a zero reward. A Task may explicitly handle
a Judge failure according to its own scoring policy.

## 7. A result carries what produced it

`RunLock` records the explicit Task name, complete
Task-folder source digest, source and commit when available, exact locally built image
content, concrete Provider, requested and effective resources, GPU allocation and sandbox
observations, agent version and integrity, harness family, one optional Task-asset
repository/path/commit/dirty observation, the name and value source of each Task variable,
verification topology and image/resources, sandbox lifecycle outcomes, config hash, seed, and
the engine's own commit.

A reportable result is one that can be published, such as on a leaderboard, because it can
be reproduced exactly. Its Task must be committed in a git repository with no uncommitted
changes under the Task path: the lock records the repository's origin URL without any
credentials in it (or its path when it has none), the commit and the Task path. The engine commit, effective resources
and committed assets must also be recorded. A run whose lock cannot back a reportable
result says why; `--require-reportable` turns that into a non-zero exit for official runs. Final Task-image publication is not part of
the current contract; future delivery may add it without changing the self-contained
Task source contract.

## 8. Agent resources are declared, not discovered

ALE never copies ambient host agent state. Host Skills, MCP configuration, and
subscription profiles are outside the Task resource model.

A Task's Skills and MCP servers are in its image, which the RunLock identifies. Only what
the harness preset, Run file, and CLI declare is added from the Host, and it replaces a Task's
resource of the same name for that run, as in Harbor. Same-name operator resources with
different digests fail before provisioning. Operator stdio MCP descriptors and adjacent code
are staged below the agent home; `{mcp}` resolves to that private resource directory.

Local stdio MCP commands execute inside the sandbox and are checked after files and
Skills are staged. Remote MCP is limited to unauthenticated Streamable HTTP endpoints
whose host satisfies the Task network policy. Native Harness subscription credentials do
not make arbitrary remote MCP authentication implicit; that remains a separate contract.

`cua-desktop` follows the same opt-in MCP path. It is not automatically injected.

## 9. Native continuation follows the retained solver

A native continuation names the harness and the exact native session ID. Resume sends
only the new instruction and selects that exact session, in the episode's own Sandbox or
in a retained solver a later episode adopted. Continuation fails when the native state is
absent or belongs to another harness. There is no "latest session" selector, transcript
replay fallback, or copying of native state between Sandboxes.

A retained solver records the episode whose agent state it holds. Reverification and
continuation attach only for that episode, and a continuing episode claims the solver
before its agent starts, so an earlier record never leads to a Sandbox a later agent
changed.

## 10. Root agent judges do not redefine solver output

An agent judge runs as framework/root so it can inspect and test the completed sandbox.
That privilege means it can mutate any guest byte, so the integrity boundary is on the
host: declared solver artifacts, native logs, hashes, and canonical solver trajectory are
finalized before verify. Task code controls the order of checks and Judge calls. Post-judge
sandbox bytes are never republished as solver artifacts. A task that needs original evidence must retain it
before a Judge changes the live workspace.

## 11. Debug retention is explicit and sanitized

Run configuration, not Task content, decides whether solver and verifier sandboxes are
destroyed or retained. Default is destroy. Before a kept sandbox receives a durable
handle, required evidence capture finishes, every stage of its Task (setup, tests and
the solution), the verifier directory and known credentials are removed, and episode
capabilities are revoked.
A later episode may hand the solver to another evaluated agent, which must not find
verification there. Subscription credentials stay in the
checkout-local Host profile and therefore require no retained-Sandbox cleanup.

Every Docker sandbox, QEMU runner and Daytona sandbox is labeled with ALE ownership,
episode, role and requested retention (Docker and QEMU also with allocated GPU identity).
Daytona's organization may be shared, so listing and destroying act only on sandboxes that
carry ALE's labels. Retained resources remain discoverable
through `ale sandbox list` and removable only through a validated ALE-managed handle.
Live labels also prevent a retained GPU from being assigned to another ALE sandbox.
Retention does not change rewards or verification records.

---

## What is **not** defended against

Stating these plainly is part of the model. Treating an unlisted gap as covered is how
security claims rot.

**A malicious task author.** A Task folder's setup and verification entries run as trusted
framework-controlled code, and a Task may declare `[agent].sudo` for its agent. Tasks
are reviewed content, not untrusted input; the identity model protects a result from its
*agent*, not a host from its Task. The gates that exist (`ale lint`, `ale validate`, CI, PR review) are
aimed at *broken* Tasks, not hostile ones. Judge keys stay on the Host Gateway; a
malicious verifier can only spend its episode's Judge session while that episode runs.

**Container escape.** A container sandbox uses Docker's default isolation. A kernel
exploit reaches the host. Use the VM backend for stronger isolation, and still do not put
anything on that host you would mind losing. On Daytona, Daytona itself sees everything in
the sandbox and in the image pushed to its registry.

**Traffic analysis, or what is said over an allowlisted tunnel.** Once `CONNECT`
succeeds the tunnel is opaque by design — the point is to reach a declared host, not to
inspect the conversation. An allowlisted host is a trusted host.

**Exfiltration through the model service.** An agent can put anything it likes in a
prompt. The Gateway records API-key and subscription calls but does not police their
content. A Transport Trace proves routing and accounting; it is not a content policy.

**A compromised image.** The exact locally built Task-image content is recorded in
provenance, so a change is *detectable*. Nothing verifies that its base or authored
Dockerfile was benign to begin with.

**Processes the agent leaves running.** ALE does not stop processes the agent started,
because a Task may ask the agent to deploy a service that verification then checks. In
shared verification such a process keeps running while verification has open egress for
its Judges, so it can still change the Sandbox before or during scoring. A Task that needs a
strict boundary uses separate verification, which restores only the declared artifacts into
a fresh Sandbox.

**Resource exhaustion by a co-tenant.** CPU, memory, writable storage, and ALE-managed
physical GPUs are admitted per sandbox or rejected. Network bandwidth and resources
consumed by processes outside ALE are not reserved; concurrent workloads can still starve
each other.

**Side channels between concurrent episodes.** Sandboxes are separate containers on
separate bridges, but they share a kernel, a page cache and a clock.
