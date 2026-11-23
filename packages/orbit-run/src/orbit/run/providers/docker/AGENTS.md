# Docker Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md). This file records the
problems peculiar to it: what we saw, what we measured, and what we decided.

## A GNOME desktop's browsers under `allowlist`

**Problem.** On the container base's desktop (GNOME 42), Chrome 157 and Firefox 160
reached no allowlisted host (November 2026). Started from the desktop they went direct and
met `DNS_PROBE_FINISHED_NO_INTERNET`; started from an agent's command, with proxy
variables that carried the token, Chrome met `ERR_PROXY_AUTH_UNSUPPORTED`.

**Context.** In a GNOME session (`XDG_CURRENT_DESKTOP` set) Chromium and Firefox take
their proxy from `org.gnome.system.proxy`, whose mode is `none` in the base, and a program
started from the dock inherits the session's environment, which holds no proxy variables.
An agent's command carries no `XDG_CURRENT_DESKTOP`, so Chrome started from one reads the
variables, and Chromium drops credentials found there. Python's urllib drops them too when
the password is empty.

**Solution.** The [allowlist relay](../README.md#egress-relay): the variables name the
relay with no token in them, and `gsettings`, run as the agent in its desktop session
(`gui.session()` finds the session's bus), points GNOME's settings at it as well.

**Decisions.**
- The variables name the relay rather than carrying the token, so a client that cannot
  send credentials gets through, and the token stays inside guestd.
- guestd connects over `docker exec` and ends with its run, sealed or not, so what it
  changed is kept in `/run/ale/system-proxy.json`. A container's `/run` is part of its
  writable layer, so a commit keeps that file with the settings it explains; a snapshot
  restores the settings before it commits. A run that reattaches to a sealed container
  starts the relay again in a guestd of its own.

## A Host whose default runtime is not runc

**Problem.** `docker run` without `--runtime` starts a container under the daemon's
`default-runtime`. On a Host that makes another runtime the default, such as gVisor's
`runsc`, a `docker:runc` Sandbox and ALE's helper containers would run under it unnoticed,
and the RunLock would name a leaf that did not start the Sandbox.

**Context.** The runtime a container ran under is in `docker inspect` as
`HostConfig.Runtime`, which the Provider already reads to record the allocation's runtime.
The helpers (the network helper in a Sandbox's namespace, the QEMU and Firecracker runners
with `/dev/kvm`, the VM builders) need devices and capabilities a sandboxing runtime may not
give.

**Solution.** Every Sandbox is started with `--runtime` naming the runtime its leaf starts
it under, `runc` included, and one for which `docker inspect` reports another is removed and
refused. Every
helper is started with `--runtime runc` (`ale.run.HELPER_RUNTIME`).

**Decisions.**
- The check runs on every started container, not once in preflight: it observes what ran,
  as the RunLock records it, whatever the daemon was told.
- The preflight still finds the leaf's runtime, so a Run or a Node with a leaf whose runtime
  this Host lacks stops before any Sandbox starts: a runtime the daemon registers is among
  the names `docker info` lists, and a containerd shim's, such as Kata's, is a
  `containerd-shim-<name>-<version>` on the PATH Docker runs it from.
- Helpers always use runc, which Docker always registers, whichever leaf they serve.

## Container features under runc and Kata

**Problem.** Under runc, root in a Sandbox gets `EPERM` from `perf_event_open`, `bpf`,
`io_uring_setup`, `unshare` of a user or mount namespace and `mount` (measured with
`capprobe` in November 2026), so a Task that profiles, traces, mounts or nests namespaces
cannot run there.

**Context.** Docker's default seccomp filter and capability set deny those calls, and
lifting them for a runc container (`--security-opt seccomp=unconfined`,
`--cap-add SYS_ADMIN`) hands the Sandbox more of the Host's own kernel. Kata runs each
container in a microVM whose guest runs no seccomp filter, so all of them but `mount` pass
there without a flag; `mount` needs `CAP_SYS_ADMIN`, which Docker drops by default, and
that capability stays inside the guest.

**Solution.** `docker:runc` declares only `raw_sockets`, which every Docker container has.
`docker:kata` declares `perf`, `bpf`, `io_uring`, `raw_sockets`, `user_namespaces` and
`mount`, and adds `--cap-add SYS_ADMIN` only to a Sandbox that names `mount`.

**Decisions.**
- runc never widens its filter or capabilities for a feature: the cost lands on the kernel
  every other Sandbox and the Host share. A Task that needs one runs on another leaf.
- Kata grants `CAP_SYS_ADMIN` only on request, so a Sandbox has no privilege its Task did
  not name, even inside its own VM.

## A gVisor sandbox's packets bypass the namespace's netfilter

**Problem.** Under `runsc`, the network helper's iptables rules in a sandbox's namespace
never matched its packets (their counters stayed 0): another Host port stayed reachable and
7412 was not rewritten. A network connected after start never reached the sandbox, and
disconnecting the one it booted with cut it off for good, so runc's internal network could
not seal it (November 2026, runsc release-20261109.0).

**Context.** gVisor's netstack takes `eth0`'s address and routes when the sandbox boots and
sends and receives through a packet socket on `eth0`, below netfilter. tc's egress hook on
`eth0` sees every frame the Sentry sends, raw and packet sockets included; its ingress hook
runs after the packet socket has read a frame, so a drop there passed everything. netstack
implements iptables, which a process with `CAP_NET_ADMIN` in it may change through
`IPT_SO_SET_REPLACE`; a privileged `docker exec` has every capability there, but
`CAP_NET_RAW`, which stock iptables needs for its socket, only under `runsc-net-raw`.
Removing the default route, as
Kata's guest does, made off-Host traffic fail at once but left another Host port, which is
on-link, waiting for its timeout.

**Solution.** The sandbox boots on a network of its own (below), sealed by tc filters on
`eth0`'s egress, and ALE's chains in netstack refuse what the seal drops, set by
`ale/run/iptables.py` through a privileged `docker exec` on the framework's Python. Sealing
changes the filters and then the chains; opening, the chains and then the filters.

**Decisions.**
- The filters are the boundary; the chains only make it fail fast. The privileged exec runs
  the sandbox's own copy of the framework's Python, which root in the sandbox can replace
  and so hold every capability in the Sentry at ALE's next change of egress.
- The filters that pass frames to the Host match the sandbox's own MAC and address and the
  bridge's MAC, and the ARP filter the sandbox's own ARP for the Host: one matching only
  the Host's address and port passed a packet with a forged source from a sandbox with raw
  sockets, and in a Kata VM, whose root writes any frame, a frame to another MAC and an
  ARP reply claiming another address too (measured, November 2026). Both runtimes share
  them (`tc.py`).
- Kata's filters toward the VM match the bridge's MAC and the Host's address, IP frames the
  sandbox's MAC too, which the guest takes from the veth; ARP's destination stays open,
  since the Host's requests are broadcast.
- Each filter is put with `tc filter replace`, the drops as flower filters with no key,
  which tc replaces in place where it cannot replace a matchall: sealing a sealed sandbox
  never takes its drop away first.
- The writer is a standard-library script rather than an iptables binary, which the image
  may lack and ALE would have to ship and pin. It copies every entry ALE does not own as it
  reads it, since netstack reads some back unlike they were set, and replaces a table
  whole, under the xtables lock, so a dockerd's rules in the sandbox survive.
- No default-route removal: `REJECT` fails every sealed connection at once, another Host
  port included, which route removal cannot.
- `ALE_HOST` refuses the replies to connections the Host opens, as runc's rules and Kata's
  host filter drop them, so the three runtimes confine alike.
- `FORWARD` jumps to the chains too, so what a sandbox forwards between network namespaces
  of its own fails fast as its own processes do.

## A Kata or gVisor sandbox's neighbours and DNS

**Problem.** On Docker's default bridge, a container with raw sockets wrote frames as the
Host, from the bridge's MAC and the Host's address, that reached a Kata or gVisor sandbox's
veth, 20 of 20, and a sealed Kata VM's filters passed them (November 2026). On any network
but the default bridge, a name lookup in either failed.

**Context.** A Linux bridge forwards a frame from any port whatever its source MAC, so in
a sandbox's namespace nothing tells such a frame from the Host's; gVisor's netstack reads a
frame on its packet socket before tc's ingress could drop it. Neither runtime sees a
network attached after it boots, so it cannot move from an internal network to one with a
route off the Host when its egress opens, as a runc sandbox does. Off the default bridge,
Docker names its embedded DNS server, `127.0.0.11`, which it serves through netfilter in
the sandbox's namespace, out of reach of a VM's guest and of netstack.

**Solution.** Every Kata or gVisor Sandbox, an `open` Task's included, boots on a bridge
network of its own, `ale-net-<sandbox id>`, which no other container joins and which goes
with the Sandbox: frames another container writes as the Host never reach it (measured).
As the Sandbox starts, the Provider writes over its `/etc/resolv.conf` the one Docker gives
a container on the default bridge, which names the Host's upstream servers.

**Decisions.**
- Not internal: opening egress removes the seal, so the sandbox leaves through this
  network's NAT; while sealed, the filters drop everything but the Host's ports.
- No rules on the Host's bridge instead: dropping what the bridge forwards between its
  ports needs `NET_ADMIN` in the Host's namespace, or the rootless daemon's, and leaves
  state there that a crashed run does not remove; a network of its own needs only Docker.
- The filters still pin both directions to the sandbox's and the Host's addresses: root in
  the sandbox forges nothing toward the Host.
- `resolv.conf` is written in place by `docker exec` rather than bind-mounted: the file
  stays the container's own and writable, as Docker's is, with nothing to keep on the Host.
- The default bridge's `resolv.conf` is read once per Provider, by one helper however many
  sandboxes start at once: each helper is a container start, which a loaded daemon queues
  (80 s for one on a rootless daemon under a load of about 200, November 2026).
- The bridge's MAC, which the filters match, is read by a runc helper on the new network,
  which resolves the Host's address there and is removed before the sandbox starts, and is
  kept in a label for a reattaching run. A helper on `--network host` sees no bridge device
  under rootless Docker whose network namespace rootlesskit detaches (`--detach-netns`,
  `dockerd-rootless.sh`'s default; November 2026), and a helper on a running sandbox's
  network would be its neighbour.

## Sandbox networks and Docker's address pools

**Problem.** Every Kata or gVisor Sandbox, and every runc one that is not `open`, has a
bridge network of its own, which Docker took from its default address pools: `172.17.0.0/16`
to `172.31.0.0/16` and `192.168.0.0/16` in /20s, about 31 networks with the default
bridge's. A daemon running about 30 such Sandboxes at once, several Runs' and test suites'
together, started no more, and smaller pools needed `daemon.json` and a restart.

**Context.** `docker network create --subnet` takes any subnet. Docker refuses one that
overlaps another of its networks (`Pool overlaps with other one on this address space`) in
its own allocator, so two creations never both take it, but checks it against no route, as
it does a subnet from its pools: one overlapping the namespace's `10.0.2.0/24` was taken
(measured, Docker 29.9.3). A network's bridge is in the daemon's network namespace: the
Host's for a rootful daemon, rootlesskit's for a rootless one, which routes `10.0.2.0/24` to
slirp4netns or pasta, and, without `--detach-netns`, nothing of the Host's. Only the Host's
address, the helper that reads the bridge's MAC and then the sandbox ever hold an address on
it.

**Solution.** The Provider gives each network a /29 of `10.77.0.0/16` that no route in the
daemon's namespace overlaps, chosen at random, and tries another, up to 32, while Docker
reports an overlap ([`subnets.py`](subnets.py)).

**Decisions.**
- /29: the Host's address and five more, 8192 networks in the block. A /30 would hold the
  helper and the sandbox only if Docker gave the sandbox the address the helper let go.
- `10.77.0.0/16` lies outside Docker's default pools, so the networks Docker gives everyone
  else are untouched, and outside the defaults of rootlesskit (`10.0.2.0/24`), Podman
  (`10.88.0.0/16` and up), Kubernetes (`10.96.0.0/12`) and GCP's VPCs (`10.128.0.0/9`). A
  route inside it only takes the /29s it overlaps. One that holds it all, such as a VPN's
  `10.0.0.0/8`, leaves none, and creating a network says so; no Host so far routes it, so
  the block is no setting.
- Random choice and Docker's refusal rather than a lock: nothing to share between users and
  caches, nothing a crashed process holds, and Runs creating networks at once seldom pick
  the same /29 (with n networks, n in 8192 per try).
- The routes are read once per Provider by a runc helper on `--network host`, not in ALE's
  own namespace: the helper runs where the daemon puts a container on the Host's network,
  wherever ALE runs and wherever `DOCKER_HOST` points. Under `--detach-netns` that is the
  Host's namespace, and the bridges' then routes only rootlesskit's `10.0.2.0/24`, outside
  the block (measured). A route added after the read, such as a VPN's, is seen by the next
  Run.
- Only the IPv4 subnet is given, so a network has IPv6 only when the daemon gives it to
  every new network, as before.
- A creation may take 300 s, as a `docker run` may: a loaded rootless daemon given 150 at
  once answered six of them after 60 s, and finished them after their CLIs had given up;
  with 300 s it created all 150. A creation that times out or is cancelled removes its
  network.
- Destroying a sandbox removes its network even when removing its container failed or
  timed out, and logs a network Docker keeps by name: with 60 s limits, a loaded rootless
  daemon left 38 of 40 networks when the containers' removal timed out first, and nothing
  said so.

## A rootless daemon's Sandboxes and the Host's Gateway

**Problem.** Under rootless Docker 29.9.3 with rootlesskit's default `--detach-netns` and
`--disable-host-loopback` (on an 8-GPU Host shared by many users, November 2026), no Docker
leaf's Sandbox reached the Gateway, under runc or gVisor: a connection to the bridge's
address at the Gateway's port was refused. Only an oracle run, which dials no Gateway,
completed there, and the conformance suite's Gateway tests passed only once pytest itself
ran in the daemon's network namespace through `nsenter`.

**Context.** dockerd runs in the Host's network namespace; its bridges are in one
rootlesskit made for them, `$ROOTLESSKIT_STATE_DIR/netns`, which rootlesskit's child, the
daemon's parent, is in. Without `--detach-netns` they are in the daemon's own namespace,
which is not the Host's either. That namespace leaves the Host through slirp4netns, whose
`10.0.2.2` reaches the Host's loopback unless `--disable-host-loopback`. Measured there:
- rootlesskit's port driver forwards the other way: a port added through its API, as
  `docker run -p` adds one, listened on the Host's `127.0.0.1` and in no namespace of the
  daemon's, and the bridge's address still refused the connection.
- With `--disable-host-loopback=false`, a container on the default bridge reached the Host's
  sshd and a proxy on the Host's loopback at `10.0.2.2`; one on an internal network had no
  route there at all.
- To the Host's own address through slirp4netns, a runc container moved 376–408 MiB/s from
  the Host and 89–90 MiB/s to it, a gVisor one 102–106 and 50–55; a server in the daemon's
  namespace gave 5.0–5.5 and 5.0–6.3 GiB/s, and 820 MiB/s and 1.6–1.8 GiB/s.
- The Fleet's tunnel carries a Run's connections between machines over a WebSocket. Its
  Node end listens in the Node's own network namespace, where a rootless daemon's Sandbox
  meets the same wall.

**Solution.** [Docker runtimes](../README.md#docker-runtimes): the Provider listens in the
daemon's namespace at each port of the Gateway and egress proxy its Sandboxes reach, and
relays each connection to that port on the Host's loopback (`rootless.py`).

**Decisions.**
- A listener in the daemon's namespace rather than another route out of it: it needs no
  daemon setting, opens nothing more of the Host, and leaves the seal as it was, which
  still admits only those ports of the Host's address. On a Fleet Node the Provider relays
  to the node agent's ports like any other.
- At the Host's port number, on every address there: the seal's filters, chains and rules
  name that port, so they stay as they are, and one listener serves every network of the
  daemon. Only the daemon's own containers reach that namespace, as a rootful daemon's
  reach a Gateway bound to every address.
- Made by a fresh Python, since only a process of one thread may join a user namespace, and
  handed back over a Unix socket: ALE's process holds the listener and relays itself, so a
  crashed run leaves nothing in the daemon's namespace and no process of its own behind.
- An `asyncio.Protocol` relay that hands each read to the other end, rather than streams:
  in the same rounds under runc, streams moved 1.0–1.1 GiB/s from the Host and the Protocol
  2.0–2.2 GiB/s; under gVisor both moved what the direct path did, which netstack bounds.
- To the Host's loopback, on which the Gateway, the egress proxy and a node agent's
  listeners all answer, as they bind every address.
- One listener per port, held by each Sandbox that reaches it until it is destroyed, kept or
  snapshotted, and closed with the last: a node agent serves Run after Run, each at ports of
  its own.
- A connection the Host's port refuses is reset, not ended, as a refused connection is.
- Found as `dockerd-rootless.sh` finds it, from the daemon on the CLI's socket, and only for
  a daemon `docker info` reports rootless; none where ALE itself runs in that namespace.
  Found again for each new listener, since a daemon that restarted under a long-lived node
  agent is another process; `docker info` is read once more per port, about twice a Run.

## Container features under gVisor

**Problem.** On `runsc`, `--cap-add NET_RAW` gave no raw socket, and `--cap-add ALL` gave
no `perf_event_open`, `bpf` or `io_uring`.

**Context.** `runsc` removes `CAP_NET_RAW` from every container unless it runs with
`--net-raw`; `--allow-packet-socket-write` lets a packet socket write as well. Docker gives
a runtime arguments only from its registration, and letting a container's annotations
override `runsc`'s flags (`--allow-flag-override`) would let it override any of them. The
Sentry implements neither perf events, BPF nor io_uring.

**Solution.** Registrations of the same `runsc`, with the arguments its
[README](../../../../../../../images/runtimes/gvisor/README.md#install-on-a-host) shows:
`runsc`, `runsc-net-raw`, which adds `--net-raw`, and `runsc-nested` (below).
`docker:gvisor` gives `user_namespaces` and `fuse` with no grant, `mount` with
`CAP_SYS_ADMIN`, and `raw_sockets` by running the Sandbox under `runsc-net-raw`; a Sandbox
gets the union of its features' grants.

**Decisions.**
- A Sandbox that needs no raw socket runs under `runsc`, so it cannot craft packets; one
  that does captures with a packet socket but cannot write one, since the feature promises
  what ping and packet capture need.
- `perf`, `bpf` and `io_uring` are not offered; a Task that needs one runs on Kata or
  Firecracker.

## A container engine in a gVisor sandbox

**Problem.** A distribution's Docker failed to start in a gVisor sandbox:
`iptables: Failed to initialize nft: Protocol not supported` (Ubuntu 22.04's and 24.04's
`docker.io` 29.1.3, November 2026). Once it started, with nf_tables, it failed to start a
container: `mount callback failed on /tmp/containerd-mount…: symlink /proc/mounts
…/etc/mtab: file exists`, and `rm` of a file of an image failed with `EPERM`.

**Context.** Debian's, Ubuntu's and Alpine's `iptables` is the `nf_tables` variant; Docker
29 and Podman's netavark set their rules through it, or through nf_tables directly. gVisor's
netstack implements nf_tables behind `--TESTONLY-nftables` (release-20261109.0): its netlink
takes nf_tables messages from a process with `CAP_NET_ADMIN` in a network namespace, which
any process holds in one it creates, since the Sentry gives every container user namespaces.
netstack runs a packet through its iptables tables and then its nf_tables ones, as Linux
runs both, but each tracks connections in a table of its own, and they shared a packet's
connection and NAT flags: once ALE had set its chains, Docker's first connection panicked
the Sentry (`packet already had NAT: DNAT performed`); ALE's patch 0008 gives each its own
state. The libnftnl of iptables 1.8.7 (Ubuntu 22.04, the container base) writes a
batch's subsystem id in host byte order, which Linux takes and gVisor refused
(`Unknown subsystem id 2560`), and that iptables writes `--sport`, `--dport` and
`--tcp-flags` with the xtables `tcp` and `udp` matches, which gVisor took as placeholders
that matched every packet: a lookup through Docker's embedded DNS server got no answer.
ALE's patches 0006 and 0007 do both as Linux does. A container engine keeps its images in
overlay layers whose whiteouts, 0:0 character devices, mark what a layer deletes; the
Host's overlayfs, a sandbox's root, refuses to create one (`ovl_mknod`, `EPERM` under runc
too), while gVisor creates them on a gofer filesystem with the `userxattr` option. A
dockerd 29.1.3 that may open a packet socket but not write one, as under `--net-raw`, fails
to start a container (`failed to advertise addresses: operation not supported`); Docker CE
29.10 only warns, and both only warn when they cannot open one.

**Solution.** A Sandbox that asks for `nested_containers` runs under `runsc-nested`,
registered with `--TESTONLY-nftables` and without `--net-raw`, with `--cap-add NET_ADMIN`,
`SYS_ADMIN` and `SYS_RESOURCE`, and with a Docker volume of its own on each of
`/var/lib/docker`, `/var/lib/containerd` and `/var/lib/containers` (`gvisor.py`), removed
with the Sandbox. ALE's chains stay in netstack's iptables tables, which the engine's
nf_tables rules do not touch, and each set tracks and NATs connections on its own. Raw
sockets and a container engine are profiles of their own, since each needs a registration of
its own.

**Decisions.**
- nf_tables in the Sentry rather than iptables-legacy: it holds for any distribution, for
  Docker installed at any time, started by its init script or by hand, set to its nftables
  backend or not. Switching the Sandbox to iptables-legacy, by `update-alternatives` or a
  legacy `iptables` first on the PATH, misses an init script that sets its own PATH, an
  engine set to nftables, and netavark.
- Only Sandboxes that ask for it reach nf_tables: the parser and interpreter are test-only
  code in the Sentry, which every process could otherwise drive from a network namespace
  of its own. The price is a third registration on every Host.
- No `--net-raw` under `runsc-nested`, rather than a fourth registration with
  `--allow-packet-socket-write`: a Sandbox gets raw sockets or a container engine, not both,
  and a nested container gets no raw socket; ping still works through ICMP datagram
  sockets.
- `NET_ADMIN`, `SYS_ADMIN` and `SYS_RESOURCE`, measured as what Docker's engines need:
  without `NET_ADMIN` dockerd does not start, without `SYS_ADMIN` it starts no container,
  and without `SYS_RESOURCE` Debian's `/etc/init.d/docker` fails at `ulimit -n 1048576`, as
  it does under runc. They stay in the Sentry and give nothing on the Host; `SYS_PTRACE`,
  which gVisor's own guide lists, was not needed.
- Volumes rather than a tmpfs, which would charge every image layer to the Sandbox's memory.
  A snapshot keeps them as volumes rather than in its image: an engine's layers, whiteouts
  among them, cannot be part of an image's layer, whose whiteouts would read as the layer's
  own ([Volumes](#volumes)).
- gVisor's own gaps are closed as ALE patches that do what Linux does, rather than worked
  around in the Sandbox, so that a Task needs nothing for gVisor.
- ALE's chains stay in iptables rather than move to nf_tables, whose gVisor implementation
  has no `REJECT`: a refusal there would wait out the seal's drop again.
- Podman's storage works on its volume, and its containers run on the host network, but
  its own networks are not offered: past the recursive bind of a namespace file that gVisor
  refuses (`EINVAL`, which Linux allows), the CNI bridge plugin's hairpin mode and a netlink
  request of netavark 1.4 fail too.

## A Sandbox's swap

**Problem.** What a Task's process got of its memory depended on the Host: with Docker's
default swap allowance, one process of a 512 MiB runc Sandbox held 1008 MiB in one run and
504 MiB in another on a Host with 8 GiB of swap, and one of a 512 MiB gVisor Sandbox 1112 MiB
(November 2026).

**Context.** `--memory` alone sets the cgroup's `memory.max`, and Docker then lets the
container swap as much again (`--memory-swap` twice `--memory`), as far as the Host's free
swap, which every user of the Host shares, reaches. A VM's memory is its size, and ALE's
Linux VMs have no swap.

**Solution.** Every Sandbox, on every Docker leaf, gets `--memory-swap` equal to `--memory`.

**Decisions.**
- No swap rather than an allowance of it: an allowance holds only as far as the Host's free
  swap at that moment, so a Task would still get different memory on different Hosts, or on
  one Host at different times.
- On every runtime: under Kata the flag only sets the guest container's `memory.swap.max` to
  0, where the microVM has no swap anyway, and leaves its size and its cgroup on the Host as
  they were (measured, Kata 4.3.1).

## A gVisor sandbox's memory and death

**Problem.** Under `runsc`, an application got less memory than under runc for the same
`--memory`, and an allocation past the limit could end the whole sandbox, even with
`--oom-score-adj 500` and no swap: one process allocating as the container started lost it
in 3 of 30 runs under rootful Docker, 300 processes of 16 MiB in 6 of 20 and, under rootless
Docker, 10 of 10, and `/dev/shm` written full in 13 of 20 and 10 of 10 (November 2026). When
the sandbox died, its guestd session ended with `urpc method "containerManager.WaitPID"
failed: EOF`, which said nothing of why, and its state read at that moment still said it was
running.

**Context.** Every runsc process is in the container's cgroup, and the OOM killer chooses
among them by `oom_score_adj`. Under rootless Docker, Docker's default ties the stubs that
run the application with the Sentry, whose loss ends the sandbox. A stub the Host's OOM
killer takes frees nothing until the Sentry has torn its process down, since the
application's memory is the Sentry's memory file, so the next charge takes another process
([Out of memory](../../../../../../../images/runtimes/gvisor/README.md#out-of-memory) has
the mechanism and the measurements). `docker wait` returns once the container has stopped:
0.2 to 0.3 s after the session ended under rootful Docker, up to 10.5 s under rootless
Docker on a loaded Host.

**Solution.** ALE's runtime keeps a sandbox's memory below its cgroup's limit and kills in
the Sentry, one process at a time, as a guest kernel would (patch 0009), and counts those
kills in `memory.oom_control`, which a watcher in the sandbox writes to a `docker exec`
session ([A sandbox that runs out of its memory](#a-sandbox-that-runs-out-of-its-memory)).
The Provider adds the Sentry's memory to `--memory` (the Providers README has the measure),
passes `--oom-score-adj 500` for the Host's OOM killer, which still acts on memory the
Sentry does not see, and, for every Docker leaf, waits on a container whose guestd
session's stream ended unasked and reports from its state why the sandbox died, an OOM kill
in it among the reasons.

**Decisions.**
- A patch to the Sentry, not swap: with Docker's default swap allowance the Host reclaims
  the memory file to swap, until swap fills too, and then 7 of 20 sandboxes were lost the
  same way; swap also makes a Task's memory depend on the Host
  ([A Sandbox's swap](#a-sandbox-s-swap)).
- The Sentry kills rather than releases a stub's memory faster after the Host's kill: the
  Sentry itself allocates while it handles the death, and that charge takes the next
  process.
- The watcher runs from the Sandbox's start, as Kata's does, since any kill since then
  counts. It asks for `oom_score_adj` -1000, which the Sentry grants, so that it outlives
  the kills it reports, and its memory is part of the Sentry's allowance (the Providers
  README has the measure). It reads the count every second, since the Sentry announces no
  change of it: in an idle sandbox beside a Python process standing in for guestd, it added
  4.7 MiB to the container's memory and 0.07% of a core to its CPU, where reading every
  100 ms took 0.4% (120 s each, rootless Docker 29.9.3, Linux 6.12, November 2026). A count
  a second old is enough: the Sentry spares init, so its kills alone never end the sandbox,
  and one that lost only guestd is read once `docker wait` gave up, 30 s later.
- The Sentry's allowance is measured beside the framework's processes alone, the desktop
  ended: one process beside the desktop varies by about 50 MiB from run to run, and the two
  runtimes account the desktop's memory differently, so no allowance gives parity there.
- The allowance puts runc's share in the middle of gVisor's, and the parity test allows 32
  MiB either way: the Sentry's own memory when it killed, 40 to 70 MiB at 4 GiB, made one
  process's share vary by up to 48 MiB from run to run, at every size. Returning the
  Sentry's free heap before each kill did not narrow it (alternating runs, 512 MiB and
  4 GiB).
- `--oom-score-adj 500` for every gVisor sandbox, rootful or rootless: it ranks every stub
  above the Sentry and the gofer wherever the shim's value is below it.
- `docker wait` with a 30 s deadline, not polling: a container that stopped answers as soon
  as Docker has stopped it, and one that runs past it lost only its session.
- Waited on outside the timeout of the call whose session ended: that timeout bounds
  guestd's answer, and a short one would otherwise leave the death unexplained.
- Only an end of the stream ALE did not cause, of a session guestd answered on: ALE closes
  a session before it destroys, keeps or snapshots the Sandbox, and that end is no death; a
  guestd that never answered failed to start, which its stderr says at once.

## A sandbox that runs out of its memory

**Problem.** A Sandbox whose processes used up its memory, and died of it, ended its episode
as an environment error, which is retried: an agent could fill memory to turn a failing
episode into a fresh one. Docker records none of the OOM kills of Kata's guest kernel, nor
of gVisor's Sentry.

**Context.** Measured with the container base (November 2026; at 1024 MiB with Linux 7.0,
Docker 29.1.3 and Kata 4.3.1; at 512 MiB, no swap, with Linux 6.12, rootless Docker 29.9.3
and ALE's runsc 0.1.2):
- One process allocating past the limit is killed alone on every leaf, and the Sandbox
  lives.
- 400 forked 16 MiB processes end the whole sandbox under runc (the OOM killer reached
  supervisord: exit 137); under Kata the guest, and under gVisor the Sentry, kills them one
  by one and the sandbox lives. A tmpfs mounted with `mount` and written full ends a Kata
  sandbox, and in a gVisor one every process the Sentry may choose, guestd among them;
  `/dev/shm`, then 64 MiB under runc and gVisor and half the VM under Kata, filled without
  harm; it now holds half the Task's memory ([The size of `/dev/shm`](#the-size-of-devshm)).
- Docker's `OOMKilled` turns true at the first OOM kill of any process of the container,
  one `docker exec` started included, and stays true while the container runs on and after
  it stops, by `docker kill` too; under runc and runsc, with rootful Docker 29.1.3 and
  rootless Docker 29.9.3 (Linux 5.15 and 6.12) alike. It counts kills for a limit around the
  container as well: under a systemd slice whose `MemoryMax` (768 MiB) was below the
  container's `--memory` (2048 MiB), standing in for the Host, the container counted `oom 0`
  while the OOM killer ended its processes, and Docker reported `OOMKilled`.
- The Sentry's kills are no OOM kill of the container's cgroup on the Host: its
  `memory.events` and Docker's `OOMKilled` count none of them, and the Sentry counts them
  in `memory.oom_control` in the sandbox.
- Under Kata `OOMKilled` stays false although the container's cgroup in the guest counted
  `oom_kill 1`. In the guest the microVM runs out before the container's cgroup does
  (`global_oom`, `CONSTRAINT_NONE`), so its `oom` stays 0 while `oom_kill` counts every
  kill.
- A Kata hypervisor killed from outside, as the Host's OOM killer would, left the container
  exited (255) within 5 s but its `docker exec` sessions open, so a command waited out its
  timeout, which in an episode is the agent's final `timeout`.

**Solution.** The container's state, which the Provider reads once a guestd session ended
unasked (above), says whether the Host's kernel killed in it: `OOMKilled`. Under Kata,
whose guest kernel kills, and gVisor, whose Sentry does, a watcher in the sandbox
([`memory.py`](../memory.py)) writes that kernel's count to a `docker exec` session at every
change: the container's `memory.events` in the guest, where any `oom_kill` or `oom` counts,
or the Sentry's `memory.oom_control`, where any `oom_kill` does. A loss after such a kill
raises `MemoryExhaustedError`, whose message gives Docker's record, the watcher's count or
both. Under Kata the Sandbox also waits on its container and ends guestd's sessions once it
stops.

**Decisions.**
- Docker's record, read once when the Sandbox is lost, rather than the container's cgroup
  watched while it lives: it costs no thread per Sandbox, and it outlives the container,
  whose cgroup goes with it. It does not say whose limit was reached, which ALE does not
  need: a loss to the Host's memory ends the Episode as one to the Sandbox's does.
- Under gVisor both: the Sentry's kills reach neither Docker nor the Host's cgroup, and the
  Host's OOM killer still acts on memory the Sentry does not see, such as its own heap,
  which only Docker records.
- Any OOM kill since the Sandbox started counts, as `OOMKilled` keeps it: a Sandbox that
  survived one, as a small container base whose desktop the OOM killer took while it
  started can, and was lost later is lost to memory.
- A loss after an OOM kill is a memory death whatever the exit code: the Sentry ended once
  with 139, and what killed the main process followed from the memory.
- Under Kata every kill in the guest counts, since the microVM's memory is the Task's, and
  under gVisor every kill of the Sentry's, which kills only for the sandbox's own limit. The
  watcher is in the sandbox, where root could end it; that only hides an exhaustion behind a
  lost session, as root ending guestd does. A hypervisor the Host's OOM killer ends leaves
  no record in the guest, and its loss stays a lost guest.
- A stop by ALE, by its GPU watchdog or its storage watch
  ([Writable storage](#writable-storage)), is reported as what ALE stopped the Sandbox for,
  without reading its state or its watcher: an earlier OOM kill of one process, after which
  the Sandbox went on, leaves `OOMKilled` set or a count raised, which would make ALE's stop
  a loss to memory.

## The size of `/dev/shm`

**Problem.** PyTorch's DataLoader with 8 workers, which pass batches of 64 images of
3×224×224 float32 to the main process through `/dev/shm`, ended at its first batch with
`DataLoader worker (pid(s) ...) exited unexpectedly`, a bus error in a worker, under runc, as
under gVisor on Slurm, where a Linux VM of the same memory ran it (November 2026).

**Context.** Docker gives every container a 64 MiB `/dev/shm` (`size=65536k`), whatever its
`--memory`. runsc mounts Docker's as a tmpfs of the Sentry of the size Docker gave
(`--shm-size 512m` gave `size=524288k`; ALE's runsc 0.1.0, by `docker run`). Kata's
guest mounts its own `/dev/shm` without a size, half the microVM, and ignores `--shm-size`
(Kata 4.3.1). A tmpfs's pages are charged to the cgroup of whoever writes them, under gVisor
the Sentry, so `--memory` holds them as it holds a process's. Linux sizes a tmpfs at half its
memory by default.

**Solution.** Every Sandbox gets `--shm-size` of half its Task's `memory_mb`
([`memory.shm_mb`](../memory.py)). Measured with the container base at 2048 MiB (rootful
Docker 29.1.3, Linux 7.0, Kata 4.3.1, November 2026):

| | runc | Kata |
|---|---|---|
| `/dev/shm` | 1024 MiB, `size=1048576k` | 1049 MiB, half the 2195 MiB microVM |
| Written until a write failed with `ENOSPC` | 1024 MiB | 1048 MiB |
| Then one process allocating | held 608 MiB, killed alone; the Sandbox answered | held 592 MiB, killed alone; the Sandbox answered |
| Then 400 processes of 16 MiB | the Sandbox died, `MemoryExhaustedError`, as with `/dev/shm` empty | the guest killed them one by one; the Sandbox lived |

The DataLoader above, on CPU with 8 CPUs under runc, failed at 64 MiB and ran at 2320
samples/s in a 16 GiB Sandbox (8 GiB of `/dev/shm`) and 2745 in a 4 GiB one (2 GiB).

**Decisions.**
- Half the memory, not a fixed size: what `/dev/shm` holds is the Sandbox's memory either
  way, and half is what a Linux machine of that memory gives, so a Task has the same room in
  a container as in a VM. A fixed size would be too small for a large Task or most of a small
  one's memory.
- Half the Task's `memory_mb`, not of `--memory`, which under gVisor adds the Sentry's
  allowance.
- Passed under Kata too, which ignores it, so that every runtime starts from one argument
  list.

## guestd's replies under gVisor

**Problem.** A screenshot of 1.1 MB never arrived from a gVisor sandbox whose image set
`PYTHONUNBUFFERED=1`; the same image under runc delivered it.

**Context.** guestd runs on the framework's Python but inherits the image's environment
through `docker exec`. Under `runsc` a write to a pipe outside the sandbox returns at most
64 KiB, and a text stream over an unbuffered file, which `PYTHONUNBUFFERED` makes stdout,
drops the rest of each write.

**Solution.** guestd writes each reply until the last byte is taken, and every Provider
starts it with `-I`, which ignores the image's `PYTHON*` variables and user site, in the
root directory; the Python it runs as root to read the desktop session's variables is
isolated too.

**Decisions.**
- Both, on every Provider: the writer holds whatever a pipe takes, and `-I` keeps an
  image's `PYTHONPATH` or `PYTHONHOME` from replacing guestd's modules.
- No root process of guestd's starts in a directory, or with variables, the agent may have
  chosen: `docker exec --workdir /`, `cd /` over Daytona's SSH, `os.chdir('/')` in Orchard's
  start, `WorkingDirectory=/` in the VM unit; Firecracker's init and the Windows service
  already start it in `/` and the framework's own folder.

## Sharing a GPU by memory

**Problem.** A Task that needs a few GiB of device memory took a whole 80 GB H100, and on a
Host whose GPUs other users' jobs share, a GPU an ALE Sandbox took could still be full: its
exclusive flock said nothing of what others use.

**Context.** The Host's `nvidia-smi` reports each GPU's `memory.free` and, to an
unprivileged user, every user's compute processes by Host PID (driver 570, November 2026); a
PID's cgroup names its container. Under runc each CUDA process is a row; under gVisor the
whole sandbox is one, its Sentry, whose memory is nvproxy's count. Other tenants allocate up
front, grow in steps and start or stop in larger ones (the README's admission has the
measures). Under rootful Docker ALE's user cannot signal a container's processes. runsc with
patch 0004 limits a sandbox's device memory on each GPU by an annotation; nothing limits a
runc container's.

**Solution.** [GPU memory](../README.md#gpu-memory): `gpu_memory_mb` shares a GPU among the
Sandboxes that name it, admitted by headroom plus a margin under one lock until `docker run`
returns, limited by the annotation under gVisor, and, under either runtime, watched every 5 s
by the leaf's watchdog. Past its share, a runc Sandbox loses its largest process on that GPU
and its episode goes on; a gVisor Sandbox is stopped, and its episode ends in an environment
error.

**Decisions.**
- One lock for the Host's GPUs rather than one per GPU: admitting a shared Sandbox reads
  every GPU's headroom, and the lock must cover the read and `docker run` both. It is named
  by the Docker daemon's ID in `/tmp`, not kept in ALE's cache, so that every user and cache
  that admits through one daemon takes the same lock. The running containers' labels stay
  the record, so a crashed process holds nothing; what one daemon's Sandboxes reserved and
  do not use yet is invisible through another daemon.
- Every Sandbox gets both GPU labels, empty when unused: `docker run --label` overrides an
  image's label of the same name, so a Task image cannot pose as ALE's record, and the
  labels need no defensive parsing.
- Headroom subtracts only what ALE's Sandboxes reserved and do not use yet: what they use is
  already missing from `memory.free`, and counting it twice would refuse Sandboxes that fit.
- Among the GPUs that can take a Sandbox, the one with the most headroom does: spreading
  keeps Sandboxes away from GPUs other tenants keep busy, and from the growth of their jobs.
  The price is that a Sandbox that takes its GPU whole can be refused where packing the
  shared ones would have left a GPU empty; `[sandbox.docker] gpus` pins the GPUs a leaf may
  take, for a Host that wants some kept for whole-GPU Sandboxes.
- The margin protects against growth, not arrivals: admission is a snapshot, and a tenant
  that starts later can fill the GPU anyway. A refusal is a capacity miss: "not now", which
  a Fleet Node answers so that the Sandbox may come back to it, not "never here".
- Under runc the watchdog kills one process, the largest on that GPU, so the memory others
  were admitted with comes back at once, and the episode goes on, as after the OOM killer
  took a process past a memory limit. Going past a share is the Sandbox's own doing: ending
  its episode as an environment error let an agent turn a failing episode into a rerun or
  an unscored one, and gave runc and gVisor different outcomes for the same thing.
- The kill runs in a helper container that joins the Sandbox's PID namespace, never in the
  Sandbox: an agent with `sudo` can replace the Sandbox's own Python, and so could make a
  kill run there a silent no-op. The watchdog re-checks the PID's container and start time
  on the Host and the helper the start time again in that namespace, so a reused PID is
  never killed; the process's name may change and does not count. A runc Sandbox cannot
  nest a PID namespace, so `NSpid`'s last field is its PID there.
- The kill is logged in the context of the Sandbox's latest command when the watchdog
  found the overrun, so it lands in its episode's execution trace, in the phase that ran
  it, rather than in the trace of whichever episode started the one watchdog task, or in
  the next phase, which may have begun by the time the helper's container is removed.
- Under gVisor the watchdog stops the whole Sandbox and its episode ends in an environment
  error: the Sentry is its one process, and memory past the limit means nvproxy's accounting
  failed, which is ALE's failure, not the agent's.
- One watchdog task per Docker leaf of a process, which ends when it watches nothing,
  rather than one per Sandbox: each look reads the whole Host once.
- Polling, every 5 s: NVML has no event for memory use. A Sandbox's rows are compared with
  its share as they are: a share holds every CUDA context, and the driver's own memory for
  a context is in no row. The watchdog acts on several Sandboxes at once, and warns once per
  streak of failed looks rather than at every look.
- Managed memory is seen by neither the limit nor the watchdog (it appears in no process's
  row); admission sees it as memory no longer free.

## Writable storage

**Problem.** A Task's `storage_mb` became `--storage-opt size`, which the containerd image
store, a fresh Docker 29's default, accepts and ignores: a Sandbox wrote past its limit
unnoticed and could fill the Host's disk. The classic `overlay2` driver refuses the option
under rootless Docker and off xfs mounted with `pquota`, so there such a Sandbox failed at
`docker run`.

**Context.** Measured in November 2026:
- Under the containerd store, Docker 29.1.3 rootful and 29.9.3 rootless, both on ext4, let
  a container of 64 MiB write 200 MiB; overlay2 refused the option on ext4, on xfs without
  `pquota`, and under rootless Docker 29.1.3 on xfs with `pquota`. Moby's containerd image
  service creates a snapshot without the container's storage options
  (`daemon/containerd/image_snapshot.go`, v29.1.3).
- `SizeRw` of `docker container inspect --size` is the blocks of the container's writable
  layer, as containerd's snapshotter (or the classic driver) walks it on the Host. It counts
  what runc, gVisor registered with `--overlay2=none` and Kata (virtio-fs, `cache=auto`)
  write alike, by the next read, under rootful Docker 29.1.3 and rootless 29.9.3 alike; not
  a volume, a tmpfs, the holes of a sparse file, nor a file deleted while still open. The
  README has the table, and the costs: a walk takes about 1 s of the daemons' CPU per
  100,000 files, rootful or rootless, and a Sandbox wrote at up to 0.95 GB/s to an NVMe
  disk.
- A ready Sandbox of the container base holds 3.4 MiB in its layer, and 16 MiB 30 s later.

**Solution.** Every Docker leaf holds a Sandbox that names `storage_mb` to it by measuring:
[`storage.py`](storage.py) reads its `SizeRw` every 5 s while this process drives it, and
past `storage_mb` MiB the Sandbox kills the container, logs the stop to the execution trace
as the GPU watchdog logs a kill, and has its sessions raise `StorageExhaustedError`, which
the episode records as `storage_exhausted`. A Sandbox's volumes count too, measured by a
helper ([Volumes](#volumes)). What measuring leaves to the agent is
[issue #152](https://github.com/AgentsLastExam/ale/issues/152).

**Decisions.**
- Measure, as Kubernetes measures `ephemeral-storage`, rather than limit: it works the same
  on every daemon, rootful or rootless, either store, and under every runtime, with
  nothing asked of the Host.
- From outside the Sandbox, by the daemon's count, never by asking the Sandbox, whose root
  the agent may hold.
- Everything over the image counts, from the start, what ALE and the image write included,
  as on a disk of that size: a baseline taken when the Sandbox is ready would leave out
  3.4 MiB, and what the framework and the desktop write after it would count anyway.
- Every 5 s, not Kubernetes' 10 s: a Sandbox writing at the disk's 0.95 GB/s overshoots by
  about 5 GiB rather than 10, at about 1 s of the daemons' CPU per 100,000 files measured
  per measurement, 1 core for 50 Sandboxes of 10,000 files each.
- One task per Sandbox, so that a layer of many files, slow to walk, delays no other
  Sandbox's measurement; and `inspect` per container, which ran 51 of them in 0.6 s where
  one `inspect` naming all 51 took 3.1 s and `docker ps --size` 1.3 s.
- Kill the container, as the GPU watchdog stops a gVisor Sandbox, rather than stop it
  gracefully: a Sandbox past its limit should write nothing more.
- Volumes by a helper of the Sandbox's own, not by the daemon: `docker system df -v`,
  Docker's only report of a volume's size, sizes every volume and layer of the daemon (4 to
  6 s with 80 volumes) and failed when a container went meanwhile.
- Alternatives measured and not taken:
  - gVisor's root overlay with a size (`dev.gvisor.flag.overlay2=root:self,size=N`): `df`
    shows the limit and a write past it fails with `ENOSPC`, rootful and rootless, but every
    file's metadata stays in the Sentry's memory, which it does not reclaim (about 1.7 KiB a
    file: 100,000 empty files took 164 MiB of the Sandbox's memory), `docker commit` and
    `docker cp` no longer see what the Sandbox writes, and an image whose `/tmp` is empty
    gets a memory-backed `/tmp` beside the limit; and it serves `docker:gvisor` alone.
  - Docker's own `--storage-opt size`: enforced only by the classic `overlay2` driver on
    xfs mounted with `pquota`, rootful, which a Host would have to switch to.
  - Firecracker, which gives each Sandbox a disk of its size: it attaches no GPU and needs
    `/dev/kvm`.

## A Sandbox booted from a snapshot was held to its own writes alone

**Problem.** A container booted from a snapshot, as a snapshot verifier or a continuation
is, could write `storage_mb` over the snapshot, however much the snapshot already held, so
it had more room than a disk of that size holding the snapshot (November 2026, found
comparing Providers for snapshot verification: QEMU's and Orchard's disks hold the
snapshot's files).

**Context.** The storage watch measures `SizeRw`, the container's writable layer, which for
a container booted from a committed image holds only what it wrote since. The commit's own
layer is not in it, and an image's `Size` covers every layer below it.

**Solution.** A commit measures the stopped container's `SizeRw` and labels the image
`ale.written` with that plus what the snapshot the container booted from held; a container
booted from a snapshot carries the label, which a reattaching run reads back, and the watch
adds it to each measurement (`storage.Watch(base=...)`).

**Decision.** A label rather than measuring the image: `SizeRw` at commit is exactly the
layer the commit adds, and costs one measurement the watch already makes.

## A commit cut short could leave a snapshot no record names

**Problem.** A `docker commit` cancelled with its episode, as a Run's cancellation or the
bound on a snapshot verifier's save cancels it, is killed with its CLI. Had the daemon
written the image by then, an `ale-snapshot:<sandbox id>` image would be left that no result
or RunLock names (November 2026, found reviewing snapshot verification, which saves a solver
mid-episode).

**Context.** Measured on Docker 29.1.3 with the containerd image store, committing a stopped
container that wrote 1 GiB, 54 s in all: a CLI killed 85, 95 or 99% of the way through left
no image in the minute after, nor did one killed 1.5 s into a commit of 3 GiB, so the daemon
gives up a commit whose client is gone. Five commits of a container removed meanwhile all
failed.

**Solution.** The Sandbox remembers that it was committing until the commit returns.
Destroying it then removes the container first, which fails any commit of it still in the
daemon, and then `ale-snapshot:<sandbox id>`, if the daemon had finished it; a snapshot that
was handed back is left to its owner.

**Decisions.**
- Removed when the Sandbox is destroyed, which every exit path does, rather than at the
  cancellation itself.
- No wait for the daemon's commit: once the container is gone, only an image the daemon
  finished can remain, and the removal that follows takes it.

## Volumes

**Problem.** A volume must reach every Sandbox its snapshot serves without a copy, and no
Sandbox's writes may reach another's view. A `docker commit` keeps no volume. A Sandbox
that runs a container engine lost its images with a commit, so its profile saved no
snapshot, and what it wrote to a volume was not held to `storage_mb`.

**Context, as measured.** Docker's `local` volume driver mounts an overlay whose `lowerdir`
is other volumes' mount points, as root under rootful Docker, at the container's start; it
works under runc, Kata (virtio-fs) and gVisor (gofer). An overlay's root takes its owner and
mode from its upper directory, root's 0755 for an empty volume, so an agent account could
no longer write to the root of a view of its own home. An overlay's options, which name
each layer's mount point, must fit in one page: 60 layers of
`/var/lib/docker/volumes/ale-v-<12 hex>/_data` do. Several episodes on one daemon remove
snapshot images while another lists them. A Docker engine in a gVisor Sandbox whose storage
is an overlay view (g2, Docker 26.1.5, gVisor 0.1.4) showed again files its images' layers
had deleted, the view hiding their whiteouts, and failed to delete another (`Operation not
permitted`).

**Solution.** Each path is a stack of volumes: the Sandbox's own, then, for one booted from
a snapshot, an empty upper and a work volume under an overlay volume that names the
snapshot's stack. A snapshot labels its image with each stack and its root's owner and
mode, read through guestd before the container stops, and keeps the top layer; a view
restores the root's owner and mode as root before anything else runs in the Sandbox. A
stack that would reach 60 layers is copied into one as the snapshot is taken. An engine's
storage is a volume the snapshot keeps whole, which a Sandbox booted from it that runs an
engine gets a copy of. A helper per Sandbox mounts the volumes that hold its writes and
counts their blocks at each measurement of the storage watch. Destroying a snapshot
removes the layers no remaining snapshot or view names, and the listing tolerates images
and volumes that vanished since.

**Decisions.**
- A layer per save, as image layers are, rather than a copy: a view costs no time or disk
  however large the volume. Copying into one layer every 59 saves keeps saving possible
  past what an overlay stacks, at the price of one copy, reflinked where the filesystem
  can.
- An engine's storage is copied rather than viewed: its own layers need whiteouts, which
  an overlay view hides and its upper refuses. A copy on a reflinking filesystem costs
  little; on ext4 it copies the engine's images.
- Volumes are measured by `du` in a helper container rather than on the Host: ALE need not
  run as root, a rootless daemon's volumes are not ALE's to walk, and the helper's walk is
  the walk the daemon makes of a writable layer. It runs beside the Sandbox, mounts nothing
  writable and reaches no network, and its baseline is in the helper, so that a run that
  reattaches the Sandbox measures alike.
- A path is a volume only if it is a directory in the image, found by the image's own
  framework Python before the container starts: a file cannot be a volume, and a path that
  passes through a link would mount somewhere else than the image means.
