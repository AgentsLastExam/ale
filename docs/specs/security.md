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

Under `no-network` (`block` in the RunLock), the agent's or the verifier's, Docker uses a
per-episode `--internal` bridge with no route off the host. QEMU enforces deny-all in the
runner network namespace, outside the guest OS, and forwards only the episode's
framework-owned service ports.

No route off the host is not the same as no way out: the host's own address on the
bridge answers on every port a host service binds to all interfaces, such as sshd, a
database, or another run's Gateway. Both backends therefore admit only the episode's own
Gateway and proxy ports on that address, sealed or not; a sealed sandbox reaches nothing
else, and one whose egress is open for a framework phase reaches the Host's other addresses
as it reaches any other host. Docker applies the rule in the sandbox's network
namespace from a short-lived helper container holding `NET_ADMIN`, a capability a runc
sandbox never has, so not even root inside it can lift the rule; provisioning fails
if the rule cannot be applied. QEMU drops forwarded guest traffic in the runner while the
sandbox is sealed and accepts only replies and the DNATed episode ports. The guest OS has
no firewall of its own: enforcement lives where the agent cannot reach it, and the
framework's `open_egress` for setup and an open phase is not undone by rules inside the
guest.

A Docker sandbox whose runtime keeps a network stack of its own, a Kata microVM's kernel or
gVisor's netstack, is outside the reach of the namespace's netfilter and of networks
attached after it boots, so the same helper confines it with tc filters on the frames it
exchanges through its namespace: they seal it on a network of its own and admit only the
episode's ports on the Host's address, and only from the sandbox's own addresses, since
root in such a sandbox may write any frame. The filters are the boundary, outside the
sandbox; what the framework sets inside it only makes blocked traffic fail at once, and
root in the sandbox that undoes it slows its own failures and still reaches nothing
([Kata](../../packages/orbit-run/src/orbit/run/providers/README.md#docker-on-kata-containers),
[gVisor](../../packages/orbit-run/src/orbit/run/providers/README.md#docker-on-gvisor)).

This is why the address is read from the network rather than assumed: Docker's
`host-gateway` alias does not work on an internal bridge, and a wrong assumption here
would look like a broken run rather than a broken guarantee.

`allowlist` and `blocklist` preserve that deny-all topology. Docker reaches the host proxy
over its internal bridge. QEMU DNATs the episode Gateway and proxy ports through the runner.
Once the sandbox is sealed, guestd relays a port on the guest's loopback to the proxy with the
episode's egress token, and every command, the agent's and a verifier's, receives proxy
variables naming that relay; on a desktop the agent account's system proxy names it too, for
the programs that take their proxy from there, a browser above all. The proxy still decides
every connection, and direct traffic remains blocked
([Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#egress-relay)).

Each phase applies its own policy: the agent's for its turns and the oracle, the verifier's
for `tests/` and `validation/` ([Task authoring](task-authoring.md#resources-and-network)).
A verifier of its own is created with the verifier's. A solver that a shared verifier runs
in is created with the least open provisioning either phase needs, a list (`allowlist` or
`blocklist`) before `block` before `open`, with the proxy whenever that is a list, because a
Provider fixes at creation what it can seal and relay to; each phase then seals or opens it
and gives the episode's egress session its own policy, and the trace records the policy
each phase applied. A Provider that cannot apply that mode refuses the
Task before any Sandbox exists, never running it open. Setup and the Harness's first
installation have the network: they are the Task's trusted preparation and ours.

Every supported Harness mode reaches its model only through the Gateway. Subscription
provider hosts are Host-side upstreams and never widen the Sandbox's Task-declared
egress policy.

Daytona has neither bridge nor runner. It seals `block` and both lists with its own switch
outside the sandbox, where the agent cannot lift it. It refuses every connection from
the sandbox to the Host, so the Host carries the Gateway, and an allowlist's proxy, in over
SSH, and the sandbox can name only those, never an address. A list's hosts are reached through
that proxy, as on every other Provider.

Orchard seals a macOS VM outside the guest as well, with a packet filter on its Mac
between the VM and the Mac's network, which admits only the episode's ports on the Mac's
address and the control channel the Mac opens into the VM. Its rules change while the VM
runs; a change that restarts the VM fails the episode instead, because the restarted VM no
longer holds what the agent or the framework left running in it
([Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#restricted-networking)).
A Mac without that filter refuses restricted Tasks before provisioning.

A Sandbox that spans several [nodes](task-authoring.md#nodes) keeps these guarantees on
every node and adds one way out of its own: its nodes reach one another on a network that
reaches nothing else, sealed or not. Its rules live outside the agent's reach on every node,
so root in a node, even one that may change its own network for nested containers, still
reaches only the other nodes. A faster fabric the nodes share, such as InfiniBand, passes
no network filter of their machines; its boundary is the fabric's own isolation, which the
Provider states
([Slurm](../../packages/orbit-run/src/orbit/run/providers/README.md#slurm-nodes)).

`open` is a deliberate escape hatch: an ordinary bridge with normal egress. A Task must
declare it for its agent, while a verifier is open unless the Task seals it;
the RunLock records both policies, `sandbox.network` and `verification.network`.

Verified by `tests/integration/test_gateway_isolation.py`, including that a sealed
sandbox reaches no other host port and that a blocklist refuses its hosts by name, by
address and by server name, by the QEMU Provider unit tests for the runner rules,
by `tests/integration/test_allowlist_relay.py` for the relay and a desktop's system proxy
under either list, by `tests/integration/test_verifier_network.py` for a verifier's own
policy in the agent's Sandbox, by `tests/unit/test_egress_proxy.py` for the egress session
and the proxy's decisions, by the Daytona, Firecracker, Orchard, Slurm and Fleet conformance
suites, and by the `demo-netprobe` task, which attempts
DNS resolution and a raw TCP connection from inside a sandbox and fails admission if either
succeeds.

## 2. Egress is a decision made in our code

Two host-side services, each with its own per-episode sessions:

- **The model gateway** serves API-key and subscription Harness modes. It swaps in the
  selected Host credential, imposes the model, enforces ceilings by refusing the next
  call, and records every call including the ones that failed upstream. A `GET` for a
  model catalog, at any path ending in `/models`, lists only the imposed model, so a
  CLI that fetches one before its first call sees what its calls will receive.
- **The egress proxy** serves `CONNECT` for what the phase's list admits: under an
  allowlist the declared hosts, refusing the rest with a 403 naming them; under a blocklist
  every public host but the declared ones, refusing those with a 403 that does not name
  them, since a Task may block a site because it holds the answer.

They share one session registry, and neither accepts the other's sessions. The Gateway
authenticates an episode's model sessions, the agent's and its Judges', wherever the
dialect's clients send their key, such as a bearer header or the dialect's own key header.
The proxy authenticates only the episode's egress session, which the Sandbox's relay sends
through Basic proxy authentication and the Gateway refuses. Its policy is that of the phase
that runs, the agent's during its turns and the oracle's, the verifier's while `tests/` or
`validation/` runs, and it is suspended at every other moment: what the agent left running
reaches no host once its turn ends, the agent never reaches a verifier's hosts, and the
model token its CLI holds opens no tunnel.

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
  - outlive the episode, leaving Orbit to track and delete it and a crashed run to leave it
    behind;
  - consume storage that the Gateway's call and token records do not measure.

  The provider's automatic prompt caching is unaffected: it exposes nothing to reuse and
  is recorded as cache-read tokens. Each dialect forwards only the methods it lists and
  refuses the rest in the provider's error format, so an unchecked method cannot slip
  through.
- **Provider-side network tools follow the Task's network policy.** These are tools the
  provider runs on its own side that reach the network, such as hosted web search, URL
  fetch or a remote MCP server. They act outside the Sandbox's network policy, so they
  are used only in a Task whose network is `open`, and two layers keep them off in every
  other Task:
  - each Harness turns them off in its program's own configuration, so the program never
    offers the model a search it cannot have; its README names them and the switch;
  - each dialect refuses a request that asks for one, whoever wrote it, such as an agent
    using its episode token directly.

  The Gateway refuses such a request rather than forwarding it without the tool. A
  configured Harness never sends one, so a request that does comes from a changed CLI
  default or from the agent itself, and a refusal makes that visible in the trace where
  a quiet rewrite would hide it. A Judge's calls are exempt: a Judge is the Task's
  evaluator rather than the agent it evaluates, and refusing its tools only fails verdicts.

A policy refusal is recorded as a `refused` call naming the policy and, unlike a limit,
does not end the episode. Each dialect's routes, authentication and policies are in the
[Gateway README](../../packages/orbit-run/src/orbit/run/gateway/README.md).

In subscription mode the provider CLI still runs in the Sandbox, but it receives only
the episode token and model Gateway URL. The Host Gateway reads checkout-local auth,
injects provider-required headers, and retains model enforcement, accounting, limits,
retry coalescing, and Transport Trace. Provider billing and account quota remain
provider-owned observations.

Both lists are enforced here rather than with `iptables` on the host or a filtering tool
inside the image, for two reasons: it needs no privilege we would otherwise not require,
and it does not depend on tooling in an image we did not build. **Traffic that bypasses
the proxy has nowhere to go**, so the failure mode is closed. Daytona's own
allowlist takes this role there, for the same two reasons (section 1). A blocklist is not
enforced by dropping its hosts' addresses at each Provider either: a name's addresses are
neither stable nor its own, since one CDN address serves many sites and a name resolves to
different addresses in different regions, so such a list would block bystanders and miss
the host, and not every Provider can drop an address (Daytona has no such list). The proxy
judges every connection the same way on every Provider.

Matching is on the requested name: `pypi.org` is that host, and `*.pypi.org`
that host and every name below it. An address or CIDR entry matches a client that asks for that address, and
no name pattern admits an address. The proxy connects from the Host, so an entry reaching
loopback or link-local addresses, cloud metadata included, fails when the Task loads. An
allowlist is deliberately *not* matched on a resolved address: a task declares `pypi.org`
because that is what it means, and pinning to whatever IP that resolved to once would break
the task rather than tighten it.

A blocklist admits what its author did not name, so the name alone would let a client
through by another spelling of a blocked host: one of its addresses, or a name that a
wildcard DNS service resolves into a blocked range. Under a blocklist the proxy therefore
also:

- resolves the name itself and refuses it when any of its addresses is blocked, or is not
  public (loopback, link-local, private and shared ranges), and connects to an address it
  checked rather than resolving again. The tunnel leaves from the Host, so without this a
  blocklist would reach the Host's own networks, which `public` on Daytona or a Mac never
  does; a Task that needs a private host names it in an allowlist;
- closes a connection whose source and destination are one address, which reached the Host
  itself, on any of its addresses;
- reads the server names the client's TLS ClientHello asks for, reassembled from as many
  records as carry it, before forwarding it, and closes the tunnel when any is blocked or the
  ClientHello cannot be read. A client that validates `github.com`'s certificate at one of
  its addresses names `github.com` there. The server's bytes flow meanwhile, so a protocol
  whose server speaks first is not held up.

## 3. Provider credentials stay on the Host

In API-key mode the provider key lives in the host's gitignored `.env`, which `orbit` reads
once when it starts, for every command; a variable already set in the environment (an
explicit `KEY=… orbit run`, or a CI secret) takes precedence. Every credential and endpoint
Orbit reads carries an `ORBIT_` prefix, so a checkout's values and the variables the
operator's own tools read, such as `ANTHROPIC_BASE_URL` for their own Claude Code, never
override each other.

The upstream follows the model provider. A Harness preset names it with
`gateway.provider`, and the Gateway sends
`ORBIT_<PROVIDER>_API_KEY` to `ORBIT_<PROVIDER>_BASE_URL`, or to the provider's own API when
that URL is unset. Both names derive from the provider so that a key is only ever paired
with its own provider's endpoint, and a run that changes provider cannot carry another
provider's key along. `--base-url` and `--api-key-env` (`gateway.base_url`,
`gateway.api_key_env`) override this for one run; an endpoint other than the provider's
must name the variable holding its key, by name rather than value, so the key stays out of
shell history and process listings.

The Sandbox receives a URL and a bearer token that is worthless anywhere else and is
revoked when the episode ends — verified by `test_a_revoked_session_loses_its_egress`.

Subscription access and refresh credentials remain in checkout-local Host state:
`ORBIT_CLAUDE_CODE_OAUTH_TOKEN` in `.env`, and `.orbit/auth/<harness>/auth.json` for Codex
and Grok. They go only to the provider's own subscription endpoint, never to
`ORBIT_<PROVIDER>_BASE_URL`. The Gateway replaces the episode token with current provider
authorization only after authenticating the session. Framework-authored result, trace,
and RunLock fields contain neither raw credential nor profile path.

Verification is a separate trusted phase after solver exit. `[verification.llm]` and
`[verification.agent]` name a model provider, model and reasoning effort at run level, and
the LLM Judge also its dialect; their keys and endpoints resolve by the same provider rule
as the Gateway's. Each episode opens one Gateway session per configured Judge holding that
key. The verifier receives only the Gateway's address and the session's token, which Orbit
injects only for the OS-specific verification entry, registers for output/transcript
redaction, and revokes when the episode ends. The key itself never enters a Sandbox and is
absent from provisioning, setup, the solver and verification environments, staged
configuration, result, record, and RunLock.

This deliberately trusts reviewed Task verifier code with the episode's Judge sessions; a
malicious Task author is outside this threat model.

The one credential a Sandbox can receive beyond episode tokens is one its Task declares in an
`env` table, such as a token for a service the Task itself uses, or one its Run gives the
agent in `[agent.env]`, such as an experiment tracker's key
([Task authoring](task-authoring.md#variables)). It comes from the checkout's `.env` only,
reaches only the commands its table names, is redacted from every record, and can neither
refer to an `ORBIT_` variable nor equal a Host credential, so no model or Judge key leaves
the Gateway this way. `[environment].env` and `[agent.env]` reach the agent, which can read
what it receives and, with network, send it anywhere, so give it only keys scoped to the
experiment.

## 4. The agent cannot change what it is measured under

The agent runs as the account the image declares, and so does the oracle that stands in for
it during calibration. Framework work runs as root on Linux and macOS and Administrator on
Windows. Windows guestd stays in the interactive session but launches agent commands with
the desktop's limited token. Only the Host's SMBIOS `orbit-sudo` marker, supplied for
`request.sudo`, permits elevated agent commands. The Provider verifies both tokens before
Task code runs; image builds request elevation explicitly. Material visibility still follows
phase ordering. See [Permissions](sandbox-image.md#permissions).

This is what stops an agent rewriting the network policy that isolates it, the clock its
timeouts are measured against, or the guest service driving its own sandbox. None of that
needs malice to matter: a result obtained under conditions the subject could alter is not
a measurement.

The oracle sharing the agent's limits is the load-bearing half. `orbit calibrate` runs every
[calibration case](task-authoring.md#calibration) as the oracle, and an oracle with more
privilege would pass exactly the tasks a real agent then fails on access alone.

A task may declare `[agent].sudo` when it genuinely needs to install software or change
system configuration. The Provider makes the grant at every sandbox start and, without the
declaration, revokes whatever way up the image gives the agent, so an image never decides
the agent's privileges ([Permissions](sandbox-image.md#permissions)). The grant is verified
rather than assumed — writing a sudoers rule succeeds in an image with no `sudo` binary —
and recorded in provenance, because an episode run that way was less isolated and the two
should not be compared without that being visible.

A QEMU VM's guest service listens on TCP, which the agent can reach over loopback and every
Host account can reach through the forwarded port. Each connection must therefore first
present the sandbox's token, which the framework's first connection to the freshly booted
guest claims; anything else is refused before an operation runs. The claim is sound because
nothing but trusted image services runs in a cold-booted guest before that connection. Linux
keeps the claimed token in root-only `/run/orbit/guestd-token`, so a restarted guest service
stays claimed while a reboot or a saved disk carries none; Windows keeps it in memory.
Container guest services are `docker exec` sessions with no listener, and a macOS VM's are
`--stdio` sessions over Orchard's authenticated exec, which listen on nothing.

A platform service inside the Sandbox that runs commands for whoever connects is a way up
past `[agent].sudo`: an agent that reaches Daytona's daemon, which runs them as root, is root
whatever its Task declared. The Provider therefore blocks every non-root account from it and
proves that as the agent before handing the sandbox out. An agent that may elevate gains
nothing from it, and with `[agent].sudo` can lift the block, as it can any rule.

The verifier writes its result to Harbor's `/logs/verifier` (`C:\logs\verifier`, macOS
`/opt/orbit/logs/verifier`), a path the agent can create beforehand: with sudo on Linux and
macOS, as any account on Windows. Before every
verification attempt Orbit therefore removes a link or file at either level instead of
following it, makes the parent the framework's and writable by it alone, and recreates the
verifier directory empty and accessible only to the framework. A result the agent planted
is gone, and nothing it left running can write or redirect one.

Verified by `tests/integration/test_identity.py`: the agent cannot read the guest service,
cannot write system paths, and cannot elevate unless its task asked; by
`tests/unit/test_guest_authentication.py` for the claimed TCP guest service; and by
`test_an_agent_cannot_plant_or_redirect_a_result` for the verifier directory.

## 5. Answers are absent, not hidden

`tests/`, `validation/`, `solution/` and `calibration/` are copied into a sandbox only for
their own phases, and of `solution/` and `calibration/` only the case an oracle runs. While the evaluated agent works
they are not access-controlled — they are not there. In separate mode only the immutable
declared artifact snapshot is restored; the solver filesystem is not transferred wholesale.
A snapshot verifier boots from the solver's disk once every stage of the Task has been
removed from it.

This is why there is no "secret" flag on a Task file. A flag is a promise about behaviour
that some future code path could forget to check; a file that was never uploaded cannot
be read by anything.

`validation/` runs only in a round's verifier, a Sandbox apart from every solver, between
two of the agent's turns, or in calibration's validation check, and the next turn's solver
boots from the snapshot saved before it ran, so it is in no solver at any time. A Task
without `validation/` validates with `tests/` there instead, so `tests/` too is in no solver
while the agent works. A round's reward and status, and the feedback text of the stage that
validated, are the only channel from either to the agent; the final run of `tests/` reaches
it not at all. What the feedback reveals is the Task author's decision, so a Task whose test
set must stay unseen has `validation/`, whose feedback keeps out what `tests/` checks.

The one exception is a Task's own: `[verifier.validation].visibility = true` gives the agent
the stage that validates, `validation/` or else `tests/`, at a fixed path outside every
directory the agent owns, readable by it and writable only by the framework: a read-only
mount of the Task folder's where the leaf mounts Host directories, else a copy written once
setup is done. It is for a validation set the agent may study, as a researcher reads
the validation code and data, and makes `tests/` visible where it validates. A verifier
always runs its own copy, staged from the Host, so an agent with `[agent].sudo` that changes
the one it reads changes nothing that scores it.

The agent's Gateway session is open from its creation through its first turn and then only
while a later turn runs, so between turns and after the last the Gateway refuses calls with
the agent's episode token: code run during validation, and a `tests/` that runs the agent's
program, cannot use the agent's model session outside its time limit, whatever credential
files a Harness leaves on disk. A Judge's session is open only while a verifier runs, so a
Judge's token that reaches the agent, in feedback or from a verifier's process, is refused
in its later turns. Before a solver is saved or kept, between turns included, Orbit removes
the credential files every Harness declares, whichever Harness ran. A refused call is traced
as refused, `session_suspended`. The round's verifier has the network of the verifier of
validation rounds, `[verifier.validation]`'s or else `[verifier]`'s, open unless the Task
seals it, and, under shared or snapshot verification, runs on the agent's
disk, so a `validation/` that runs the agent's program runs it with that network, as
`tests/` would, and with `[agent].sudo` what the agent left there can write the round's
feedback and reward; what reaches the agent is still only the round's reward, status and
feedback.

Verified by `test_agent_never_sees_verification_material`, whose setup probe exits
non-zero if any scoring path exists during setup, and by
`test_validation_is_never_in_a_solver_and_tests_never_in_a_validation_verifier` and
`test_tests_is_in_no_solver_while_the_agent_works_when_it_validates`, in every verification
mode; the Gateway's refusal and the credential removal by
`test_the_agents_session_is_open_only_while_its_turns_run`,
`test_the_judges_sessions_are_open_only_while_a_verifier_runs` and
`test_every_harness_credential_files_go_before_a_rounds_save`, and, with each real CLI, by
`test_cli_runs_a_scripted_episode_through_the_gateway`, which finds the episode token in no
file of the agent's home but those its Harness declares.

## 6. Limits end episodes rather than stalling them

API-key and subscription token and model-call ceilings are enforced by the Gateway
refusing the next call with an error the client does not retry, a 429 unless a dialect's
clients retry that regardless, and an agent phase that fails after a refusal ends as
`budget_exceeded` for every Harness and authentication mode. Some subscription endpoints expose less request-side
control; in particular, Codex rejects `max_output_tokens`, so Orbit accounts the completed
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
exceptions fail the episode; Orbit never invents a zero reward. A Task may explicitly handle
a Judge failure according to its own scoring policy.

## 7. A result carries what produced it

`RunLock` records the Task name, the warnings loading the Task gave, complete Task-folder
source digest, source and commit when available, exact locally built image content, for the
solver and a separate or snapshot verifier the Provider selected and the leaf that started
the Sandbox with its container runtime, requested and effective resources, GPU allocation and
sandbox observations, agent version and integrity, harness family, one optional Task-asset
repository/path/commit/dirty observation, the name and value source of each Task variable
and of each variable the Run gives the agent, the text the Run puts around the instruction,
the agent's and each verifier's network policies, verification topology and image/resources,
the verifier of validation rounds' among them where it has its own,
sandbox lifecycle outcomes, config hash, seed, and the engine's own commit.
Leaves of one kind isolate a Task differently and give it different kernel features, so a
result names the one that ran it, under a scheduler too.

A reportable result is one that can be published, such as on a leaderboard, because it can
be reproduced exactly. Its Task must be committed in a git repository with no uncommitted
changes under the Task path: the lock records the repository's origin URL without any
credentials in it (or its path when it has none), the commit and the Task path. The engine commit, effective resources
and committed assets must also be recorded. A run whose lock cannot back a reportable
result says why; `--require-reportable` turns that into a non-zero exit for official runs. Final Task-image publication is not part of
the current contract; future delivery may add it without changing the self-contained
Task source contract.

## 8. Agent resources are declared, not discovered

Orbit never copies ambient host agent state. Host Skills, MCP configuration, and
subscription profiles are outside the Task resource model.

A Task's Skills and MCP servers are in its image, which the RunLock identifies. Only what
the harness preset, Run file, and CLI declare is added from the Host, and it replaces a Task's
resource of the same name for that run, as in Harbor. Same-name operator resources with
different digests fail before provisioning. Operator stdio MCP descriptors and adjacent code
are staged below the agent home; `{mcp}` resolves to that private resource directory.

Local stdio MCP commands execute inside the sandbox and are checked after files and
Skills are staged. Remote MCP is limited to Streamable HTTP endpoints whose host satisfies
the Task network policy. A Task authenticates to one only through the `headers` it declares,
whose credentials come from the checkout's `.env` like any Task variable; Native Harness
subscription credentials never reach one.

`cua-desktop` follows the same opt-in MCP path. It is not automatically injected.

## 9. Native continuation follows the retained solver

A native continuation names the harness and the exact native session ID. Resume sends
only the new instruction and selects that exact session, in the episode's own Sandbox, in
a retained solver a later episode adopted, or in one booted from its snapshot. Continuation fails when the native state is
absent or belongs to another harness. There is no "latest session" selector, transcript
replay fallback, or copying of native state between Sandboxes.

A retained solver or snapshot records the episode whose agent state it holds.
Reverification and continuation use it only for that episode, and a continuing episode claims the solver
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
destroyed, kept, or (the solver) saved as a snapshot. Default is destroy. Snapshot
verification saves the solver whatever that configuration says, as
[Teardown and retention](episode-protocol.md#teardown-and-retention) specifies. Before a
kept sandbox or a snapshot receives a durable handle, required evidence capture finishes,
every stage of its Task (setup, tests and an oracle's calibration case), the verifier
directory and known credentials are removed, and episode capabilities are revoked.
A later episode may hand the solver to another evaluated agent, which must not find
verification there. Subscription credentials stay in the
checkout-local Host profile and therefore require no retained-Sandbox cleanup.

Every Docker sandbox, QEMU runner and Daytona sandbox is labeled with Orbit ownership,
episode, role and requested retention (Docker and QEMU also with allocated GPU identity).
Daytona's organization may be shared, so listing and destroying act only on sandboxes that
carry Orbit's labels, and every snapshot records the same. Retained sandboxes and snapshots
remain discoverable through `orbit sandbox list` and removable only through an Orbit-managed
handle, which Orbit checks before it acts.
Live labels also prevent a retained GPU from being assigned to another Orbit sandbox.
Retention does not change rewards or verification records.

---

## What is **not** defended against

Stating these plainly is part of the model. Treating an unlisted gap as covered is how
security claims rot.

**A malicious task author.** A Task folder's setup and verification entries run as trusted
framework-controlled code, and a Task may declare `[agent].sudo` for its agent. Tasks
are reviewed content, not untrusted input; the identity model protects a result from its
*agent*, not a host from its Task. The gates that exist (`orbit lint`, `orbit calibrate`,
CI, PR review) are aimed at *broken* Tasks, not hostile ones. Judge keys stay on the Host
Gateway; a malicious verifier can only spend its episode's Judge session while that episode
runs.

**Container escape.** A container Sandbox on a leaf that shares the Host's kernel with it
has a container's isolation, and a kernel exploit reaches the Host. A leaf whose Sandboxes
each have a kernel of their own, a VM's or one in user space, puts that kernel between an
exploit and the Host's, so listing such leaves first in `sandbox.providers`, or only them,
keeps Tasks off the Host's kernel; still put nothing on that Host you would mind losing.
Which leaf gives which kernel, and how a GPU reaches each, is in
[Providers](../../packages/orbit-run/src/orbit/run/providers/README.md#provider-ids-and-profiles).
Each [container feature](task-authoring.md#container-features) a Task names exposes more of
the kernel its Sandbox runs on; no Provider widens a Sandbox for a feature it did not
name. On Daytona, Daytona itself sees everything in the sandbox and in the image pushed
to its registry.

**Traffic analysis, or what is said over an admitted tunnel.** Once `CONNECT`
succeeds the tunnel is opaque by design, past a blocklist's look at the server name — the
point is to reach an admitted host, not to inspect the conversation. An allowlisted host is
a trusted host.

**What a blocked host serves, from elsewhere.** A blocklist keeps the agent's programs from
the hosts it names, by name, by address and by the server name of a TLS handshake. The same
content on another host stays reachable: a mirror, an archive, a search engine's cache, a
service that fetches a page on request. So does a blocked host reached at an address the
list does not name by a client that hides its server name, with Encrypted Client Hello or
none at all, or through a CDN whose handshake names another of its sites. A Task that must
keep content from the agent withholds the network or allows only what it needs.

**A provider-side network tool the Gateway does not name.** Each dialect refuses the
tools and fields its README lists. A provider feature that reaches the network and is
added after that list is forwarded until it is listed, though a Harness that does not
offer it still never sends it.

**Exfiltration through the model service.** An agent can put anything it likes in a
prompt. The Gateway records API-key and subscription calls but does not police their
content. A Transport Trace proves routing and accounting; it is not a content policy.

**A compromised image.** The exact locally built Task-image content is recorded in
provenance, so a change is *detectable*. Nothing verifies that its base or authored
build was benign to begin with.

**Processes the agent leaves running.** Orbit does not stop processes the agent started,
because a Task may ask the agent to deploy a service that verification then checks, except
at the end of a turn a validation round follows: saving the solver stops every process in
it, and the next turn's solver boots without them, setup's included. In shared verification
a process the last turn left keeps running while the verifier runs with its own network,
open unless the Task seals it, so it can still reach that network and change the Sandbox
before or during scoring; Judges reach the Gateway under any policy. A snapshot verifier
boots fresh, so no process the agent left survives into it, but whatever the agent set to
start at boot starts again: its account's desktop autostart entries, its `HKCU` Run keys and
Startup folder on Windows, and, with sudo, system services. So does the next turn's solver a
validation round boots, which is sealed as soon as it is up, before anything is checked or
staged in it, so what starts at boot has the agent's own sealed network. A Task that needs a
strict boundary uses separate verification, which restores only the declared artifacts into
a fresh Sandbox. Calibration's validation check saves the oracle's solver before `tests/`
scores it, so in calibration of a Task that runs that check no process the oracle left
reaches `tests/` in any mode.

**Programs on the agent's disk.** A shared or snapshot verifier runs as root on the disk the
agent left. With `[agent].sudo` the agent could change any program there, the image's
Python that `ORBIT_VERIFY_PYTHON` selects included, and so what the verifier computes;
without it, only what its account can write. A Task whose verifier must not run
agent-changed code withholds sudo or uses separate verification, on its own image or the
solver's, which restores only the declared artifacts into a fresh Sandbox.

**Unscored rather than low.** With `[agent].sudo` the agent can also make what runs on its
disk after it fail under snapshot verification: the sanitation before the solver's save, the
save itself, and the boot of a verifier from the snapshot; and, with validation rounds in
any mode, a round's save or the boot of the next turn's solver from its snapshot. The
Episode then ends without a score, as an environment or Task error, rather than with a low
one; a sanitation that fails before a retention only forces the Sandbox's destruction. A
Task where that difference matters withholds sudo.

**Resource exhaustion by a co-tenant.** CPU, memory, writable storage, and Orbit-managed
physical GPUs, whole or by device memory, are admitted per sandbox or rejected. Network
bandwidth and resources consumed by processes outside Orbit are not reserved; concurrent
workloads can still starve each other. On a GPU that Sandboxes share, one can take another's
device memory: where nothing limits it, for the seconds until its Provider's watchdog kills
the process while a Run holds the Sandbox, and for as long as it likes once the Sandbox is
kept after its Run, since nothing watches it then; and, under any runtime, through managed
memory, which neither gVisor's limit nor any process's row in `nvidia-smi`, and so no
watchdog, sees.

**Side channels between concurrent episodes.** Sandboxes are separate containers on
separate bridges, but they share a kernel, a page cache and a clock.
