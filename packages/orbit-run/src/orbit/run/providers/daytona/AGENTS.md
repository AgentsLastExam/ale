# Daytona Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#daytona). This file
records the problems peculiar to it: what we saw, what we measured, and what we decided.

## Daytona's domain allowlist terminates TLS

**Problem.** Under `allowlist`, Chrome 157 and Firefox 160 on a Linux desktop Task refused
every allowed host with a certificate error (`ERR_CERT_AUTHORITY_INVALID`, "Warning:
Security Risk"), while `curl` and Python reached it (November 2026, CPU runner).

**Context.** The Provider used to seal an allowlist with Daytona's `domainAllowList`.
Daytona's proxy behind it ("netleash") terminates TLS: `openssl s_client` from the sandbox
shows `example.com` presenting a certificate issued by "netleash ephemeral CA". Daytona
puts that CA at `/etc/daytona/netleash/ca.crt` and names it in `SSL_CERT_FILE`,
`CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS`, which curl, Python and
Node read; browsers keep their own trust stores and reject it, with or without Encrypted
Client Hello. Daytona therefore sees allowed traffic in plaintext. Its allowlist also took
only domain names, at most 100, and passed plain HTTP and other ports of an allowed host.

**Solution.** `close_egress` seals `allowlist` with `networkBlockAll`, as `block`. The SSH
tunnel that carries the Gateway also carries the Host's egress proxy to the sandbox's
loopback (`PROXY_PORT`), and guestd's [relay](../README.md#egress-relay) fronts it, so
the agent's commands and its desktop reach allowed hosts exactly as on Docker.

**Decisions.**
- Our proxy rather than trusting Daytona's CA in each browser: TLS stays end to end, the
  allowlist means the same on every Provider, and no browser or image needs changing. The
  cost is that allowed traffic passes through the Host's connection.
- Trusting the netleash CA in the agent's NSS databases was rejected: it depends on tools in
  the image, differs per browser, and leaves Daytona reading the traffic.

## `networkBlockAll` accepts connections it passes nothing on

**Problem.** The episode test that checks setup's network against the agent's began to
report the agent's network open (November 2026).

**Context.** Under `networkBlockAll` every runner, CPU as GPU, accepts TCP connections to
ports 80 and 443 of any address, a GPU runner also to 53, and passes nothing on them: a TLS
handshake ends at once and no name resolves. The test checked a bare connect.

**Decision.** The sandbox is closed in every way that carries data, so nothing changes in
the Provider. Whether the network is open is judged by a completed TLS handshake with a
public host, in `_set_egress` and in every test.

## A sandbox that runs out of its memory

**Problem.** A Daytona sandbox whose processes used up its memory died with nothing to say
why: guestd's session ended (`guest session closed`), the episode became an environment
error and was retried, and Orbit's teardown then failed with
`409 Sandbox state change in progress`.

**Context.** Measured on Daytona's `us` region, daemon 0.232.2, with the container base at
1 GiB (November 2026):
- The sandbox is a container in a user namespace, with cgroup v2 and a cgroup namespace: its
  `/sys/fs/cgroup` holds `memory.max` of 1 GiB, every process sits in `/init.scope` below
  it, and `/sys/fs/cgroup/memory.events` counts `oom` for that limit. `dmesg` prints nothing
  and `/proc/meminfo` shows Daytona's host.
- One process allocating past the limit is killed alone. 400 forked 16 MiB processes, or a
  tmpfs that root mounts and writes full, end the whole sandbox within 1.4 to 2.8 s, the
  OOM killer taking the desktop first and then Daytona's daemon, PID 1. Daytona's own
  `/dev/shm` is 64 MiB and fills without harm.
- Daytona's record of a sandbox that died so says `stopped` with no `errorReason`; it then
  destroys it by itself within 10 to 25 s (`desiredState` `destroyed`), refusing a delete
  meanwhile with 409, and keeps none: a longer `autoDeleteInterval` is refused in the
  region.
- A watcher in the sandbox waiting on `memory.events` with `POLLPRI` had `oom` at the Host
  0.2 to 0.8 s before guestd's session ended; one reading every 2 s missed it.

**Solution.** `_Memory` in [provider.py](provider.py) starts `memory.WATCHER` over an SSH
session of its own once the framework is in place; a lost guestd session waits up to 10 s
for that session to end too, and raises `MemoryExhaustedError` if the sandbox's
`memory.events` counts any `oom_kill` or `oom`. `delete_sandbox` takes a 409 for a sandbox
Daytona already destroys as success, and waits for it to go.

**Decisions.**
- Both counts, since the sandbox started: Daytona's host could end its processes too, which
  counts `oom_kill` alone, and a loss after that is memory's as well; the message gives
  both, so `oom` 0 points at the host.
- Watched from inside, since nothing outside says why: the watcher shares the sandbox's
  memory and root can end it, which only hides an exhaustion behind a lost session.
- Orbit does not rank the agent's commands for the OOM killer here: 1000 kept the sandbox
  alive in both fatal cases, but the sandbox an agent's memory ends is the agent's result
  either way.

## `/dev/shm` is 64 MiB

**Problem.** Every Daytona sandbox has a 64 MiB `/dev/shm`, whatever its memory, where every
other Linux Sandbox has half its `memory_mb`
([Memory](../README.md#memory)). PyTorch's DataLoader with 8 workers dies at its first batch
in 64 MiB.

**Context.** Measured on Daytona's `us` region with the container base at 2 GiB (November
2026):
- Daytona's create request takes no size for it: `CreateSandbox` in its API client 0.233.0
  has `cpu`, `memory`, `disk`, `gpu`, `labels`, `env` and the like, nothing for `/dev/shm`.
- Root cannot remount it (`mount -o remount,size=1024m /dev/shm` fails with exit 32): the
  mount belongs to the namespace outside the sandbox's user namespace.
- Root can mount a tmpfs of its own over it. Its pages are charged to the sandbox's cgroup:
  900 MiB written by the agent raised `memory.current` by 965 MiB.

**Solution.** `_size_shared_memory` in [provider.py](provider.py) mounts a tmpfs of
`memory.shm_mb` with mode 1777 over `/dev/shm`, as root, right after the framework is
checked and before the image's command or the agent runs. A retained sandbox keeps the mount
while it runs, so attaching to it mounts nothing. In a 2 GiB sandbox with it written full, a
process allocating past the rest is killed alone and the sandbox answers, as on Docker; 400
processes of 16 MiB end the sandbox, as they do with it empty, and Orbit reports
`MemoryExhaustedError`.

**Decision.** `mount(2)` through the C library from the framework's Python, not the image's
`mount`, which an image need not carry.

## A root agent and the daemon

**Problem.** A Task whose agent runs as `root` was refused on every Daytona start, after the
sandbox was made: `root can still reach Daytona's daemon at 127.0.0.1:2280, ...; a sandbox
where the agent can run commands as root is not handed out` (November 2026). A Harbor Task,
whose agent runs as root by default, ran on Docker and could not run on Daytona.

**Context.** The daemon is PID 1, `/usr/local/bin/daytona`, and runs as root. It listens
without authentication on 2280 (its toolbox API), 22220, 22222 and 33333. The Provider
rejects every non-root account's connections to them with `iptables` owner rules; root's
cannot be rejected. Probed as root in a sandbox (daemon 0.233.1, CPU runner):
- the toolbox API's paths (from its own `/swagger/doc.json`) act inside the sandbox only:
  files, processes, sessions, git, LSP, ports, computer use, `/env` and `/init`;
- PID 1's environment names the organization, region, sandbox and snapshot, a telemetry
  endpoint and the egress proxy `172.20.0.1:18080` outside the sandbox. No variable holds a
  token, key, secret or password;
- `/etc/daytona` holds only the public CA of Daytona's egress proxy, and `/root/.daytona`
  the session that runs the image's command. `GET /env` returns nothing.

A non-root agent with `[agent].sudo` already reached the daemon through root, and was
handed its sandbox.

**Solution.** `_seal_daemon` still installs the rules in every sandbox, which keeps every
other account away. It probes only an agent that may not elevate; one that may, root among
them, is not refused.

**Decision.** The daemon gives root nothing root lacks in the sandbox, so refusing a root
agent kept nothing from it. What such an agent can change is what elevation always allows,
recorded as `sandbox.sudo`.

## Agent programs download in the sandbox

**Problem.** Uploading an agent's program from the Host sends its whole build over the
Host's connection, once per sandbox: 137 MB for Codex, 93 MB for Claude Code and 173 MB
for ZCode, so a Run of many episodes at once is bounded by the Host's upstream bandwidth.

**Context.** Measured in a 2-vCPU sandbox of a Debian image without Node.js or Python
(November 2026, CPU runner, the Host's cache warm), each time from the start of the install
to a program that answers `--version`:

| Program | Downloaded in the sandbox | Uploaded from the Host |
|---|---|---|
| Codex 0.146.0 | 5.0–5.3 s | 9.5–9.6 s |
| Claude Code 2.1.227 | 4.0 s | 6.9–7.0 s |
| Pi 1.4.0 (121 npm packages, 24 MB, and Node.js) | 12.6–13.8 s | 9.4–10.4 s |
| ZCode 0.17.0 (the 3.16.3 `.deb`) | 18.8–28.3 s | 22.6 s |

Node.js's 57 MB archive downloads in 0.2 s there. Pi's 121 packages take 5.3 s from
`registry.npmjs.org` with 16 threads or with 64, so the registry's response time, not the
number of connections, bounds them.

**Solution.** `DaytonaSandbox.fetches_files` is true: the sandbox downloads each file
itself, checks it by its digest and unpacks it, with the CA bundle the Host uploads beside
the manifest. A verifier whose network is not open gets its Agent Judge uploaded, as every
other Provider's sandbox does, and Grok's build, which the Host decompresses, is uploaded.

**Decision.** Downloading in the sandbox, though a program of many small npm packages
installs a few seconds slower: its cost is the registry's and stays per sandbox, while an
upload's grows with every episode the Host runs at once.
