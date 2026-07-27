# Security model

What this engine guarantees about a result, how, and — just as importantly — what it
does not guarantee. The last section is the one worth reading first if you are deciding
whether to run untrusted code here.

## What the guarantees are for

Every claim below exists to protect the **integrity of a number**. An agent that reached
the open internet, read the answer key, or spent a credential is not a cheating agent so
much as a result that means nothing. The threat being modelled is a benchmark that
silently stops measuring what it says it measures.

## 1. A sandbox has exactly one way out

Default is `network.mode: block`. Each episode gets its own Docker bridge created
`--internal`, which has no route off the host. The only thing on it besides the sandbox
is the host itself, reached at that bridge's own gateway address.

This is why the address is read from the network rather than assumed: Docker's
`host-gateway` alias does not work on an internal bridge, and a wrong assumption here
would look like a broken run rather than a broken guarantee.

`allowlist` uses the **same routeless bridge**. What changes is what the host will
forward on the sandbox's behalf — see §2 — never what the sandbox can dial.

`open` is a deliberate escape hatch: an ordinary bridge with normal egress. A task must
declare it, and the declaration is recorded in provenance.

Verified by `tests/integration/test_gateway_isolation.py` and by the `demo-netprobe`
task, which attempts DNS resolution and a raw TCP connection from inside a sandbox and
fails admission if either succeeds.

## 2. Egress is a decision made in our code

Two host-side services, one token:

- **The model gateway** speaks the Anthropic dialect. It swaps in the real credential,
  imposes the model, enforces ceilings by refusing the next call, and records every call
  including the ones that failed upstream.
- **The egress proxy** serves `CONNECT` for hosts a task declared, and refuses
  everything else with a 403 naming what was declared.

Both authenticate the same per-episode bearer token and share one session registry, so
an episode has one identity and one allowlist.

Allowlisting is enforced here rather than with `iptables` on the host or a filtering tool
inside the image, for two reasons: it needs no privilege we would otherwise not require,
and it does not depend on tooling in an image we did not build. **Traffic that ignores
the proxy variables has nowhere to go**, so the failure mode is closed.

Matching is on the requested name, with subdomains of a declared host included. It is
deliberately *not* on a resolved address: a task declares `pypi.org` because that is what
it means, and pinning to whatever IP that resolved to once would break the task rather
than tighten it.

## 3. Real credentials never enter a sandbox

The provider key lives in the host's gitignored `.env`. A sandbox receives a URL and a
bearer token that is worthless anywhere else and is revoked when the episode ends —
verified by `test_a_revoked_session_loses_its_egress`.

The netprobe task additionally greps its own sandbox for credential material and fails
if it finds any.

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

`verify/`, `oracle/`, and any asset a task listed under its `verify` stage are copied
into the sandbox **during scoring**. While the agent works they are not access-controlled
— they are not there.

This is why there is no "secret" flag on an asset. A flag is a promise about behaviour
that some future code path could forget to check; a file that was never uploaded cannot
be read by anything.

Verified by `test_agent_never_sees_verification_material`, whose setup probe exits
non-zero if any scoring path exists during setup.

## 6. Limits end episodes rather than stalling them

Token, cost and turn ceilings are enforced by the gateway refusing the next call with a
429; the episode maps that to `budget_exceeded`. Per-phase deadlines produce `timeout`.
Teardown is cancellation-shielded, so a killed phase still reclaims its container.

Every terminal state is one typed status, and failures never enter score aggregates. A
`task_error` — a crashed or silent verifier — is deliberately distinct from a zero score,
because conflating them makes a broken task look like a hard one.

Verified by `tests/integration/test_limits.py`, which asserts prompt typed termination
and that no container outlives its episode.

## 7. A result carries what produced it

`RunLock` records the task source and commit, spec hash, resolved image **digest**,
agent version and integrity, harness family, every asset revision and origin, kit
hashes, config hash, seed, and the engine's own commit.

A run whose lock cannot back a published result says so. `--require-reportable` turns
that statement into a non-zero exit for CI. A task run from a local path is refused for
publication, because it cannot be re-fetched.

---

## What is **not** defended against

Stating these plainly is part of the model. Treating an unlisted gap as covered is how
security claims rot.

**A malicious task author.** A task repository's `setup/run.sh` and `verify/run.sh` run
as root in the sandbox, and a task may declare `resources.sudo` for its agent. Tasks are
reviewed content, not untrusted input; the identity model protects a result from its
*agent*, not a host from its task. The gates that exist (`ale lint`, `ale validate`, CI, PR review) are aimed at
*broken* tasks, not hostile ones.

**Container escape.** A sandbox is a Docker container with default isolation. A kernel
exploit reaches the host. If you need to run genuinely hostile code, use the VM backend
when it lands, and do not put anything on that host you would mind losing.

**Traffic analysis, or what is said over an allowlisted tunnel.** Once `CONNECT`
succeeds the tunnel is opaque by design — the point is to reach a declared host, not to
inspect the conversation. An allowlisted host is a trusted host.

**Exfiltration through the model gateway.** An agent can put anything it likes in a
prompt. The gateway records calls; it does not police their content.

**A compromised base image.** Images are pinned by digest in provenance, so a change is
*detectable*. Nothing verifies that the contents were benign to begin with.

**Resource exhaustion by a co-tenant.** CPU and memory are capped per sandbox; disk and
network bandwidth are not. Concurrent episodes on one host can starve each other.

**Side channels between concurrent episodes.** Sandboxes are separate containers on
separate bridges, but they share a kernel, a page cache and a clock.
