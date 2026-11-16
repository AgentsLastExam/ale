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

**Solution.** The [allowlist relay](../README.md#allowlist-relay): the variables name the
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

**Solution.** Every Sandbox is started with `--runtime` naming its leaf's runtime, `runc`
included, and one for which `docker inspect` reports another is removed and refused. Every
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

## A Kata sandbox's neighbours and DNS

**Problem.** On Docker's default bridge, a container with raw sockets wrote frames as the
Host, from the bridge's MAC and the Host's address, that reached a Kata sandbox's veth, 20
of 20, and a sealed Kata VM's filters passed them (November 2026). On any network but the
default bridge, a name lookup in the VM failed.

**Context.** A Linux bridge forwards a frame from any port whatever its source MAC, so in
a sandbox's namespace nothing tells such a frame from the Host's. A Kata VM sees no network
attached after it boots, so it cannot move from an internal network to one with a route off
the Host when its egress opens, as a runc sandbox does. Off the default bridge, Docker
names its embedded DNS server, `127.0.0.11`, which it serves through netfilter in the
sandbox's namespace, out of reach of a VM's guest.

**Solution.** Every Kata Sandbox, an `open` Task's included, boots on a bridge network of
its own, `ale-net-<sandbox id>`, which no other container joins and which goes with the
Sandbox: frames another container writes as the Host never reach it (measured). As the
Sandbox starts, the Provider writes over its `/etc/resolv.conf` the one Docker gives a
container on the default bridge, which names the Host's upstream servers.

**Decisions.**
- Not internal: opening egress removes the seal, so the sandbox leaves through this
  network's NAT; while sealed, the filters drop everything but the Host's ports.
- No rules on the Host's bridge instead: dropping what the bridge forwards between its
  ports needs `NET_ADMIN` in the Host's namespace, or the rootless daemon's, and leaves
  state there that a crashed run does not remove; a network of its own needs only Docker.
- The filters still pin both directions to the sandbox's and the Host's addresses: root in
  the sandbox forges nothing toward the Host.
- The filters that pass frames to the Host match the sandbox's own MAC and address and the
  bridge's MAC, and the ARP filter the sandbox's own ARP for the Host: in a Kata VM, whose
  root writes any frame, a frame to another MAC and an ARP reply claiming another address
  passed filters matching only the Host's address and port (measured, November 2026).
  `tc.py` builds them.
- Kata's filters toward the VM match the bridge's MAC and the Host's address, IP frames the
  sandbox's MAC too, which the guest takes from the veth; ARP's destination stays open,
  since the Host's requests are broadcast.
- Each filter is put with `tc filter replace`, the drops as flower filters with no key,
  which tc replaces in place where it cannot replace a matchall: sealing a sealed sandbox
  never takes its drop away first.
- `resolv.conf` is written in place by `docker exec` rather than bind-mounted: the file
  stays the container's own and writable, as Docker's is, with nothing to keep on the Host.
- The bridge's MAC, which the filters match, is read by a runc helper on the new network,
  which resolves the Host's address there and is removed before the sandbox starts, and is
  kept in a label for a reattaching run. A helper on `--network host` sees no bridge device
  under rootless Docker whose network namespace rootlesskit detaches (`--detach-netns`,
  `dockerd-rootless.sh`'s default; November 2026), and a helper on a running sandbox's
  network would be its neighbour.

## A Docker sandbox's death

**Problem.** When a sandbox died, its guestd session ended as a lost session, which said
nothing of why: under runc, `docker exec` exited with 137 and printed nothing. The
container's state read at that moment could still say it was running.

**Context.** `docker wait` returns once the container has stopped: up to 10.5 s after the
session ended under rootless Docker on a loaded Host.

**Solution.** For every Docker leaf, the Provider waits on a container whose guestd
session's stream ended unasked and reports from its state why the sandbox died.

**Decisions.**
- `docker wait` with a 30 s deadline, not polling: a container that stopped answers as soon
  as Docker has stopped it, and one that runs past it lost only its session.
- Waited on outside the timeout of the call whose session ended: that timeout bounds
  guestd's answer, and a short one would otherwise leave the death unexplained.
- Only an end of the stream ALE did not cause, of a session guestd answered on: ALE closes
  a session before it destroys, keeps or snapshots the Sandbox, and that end is no death; a
  guestd that never answered failed to start, which its stderr says at once.
- `OOMKilled` alone is no death: Docker keeps it once the OOM killer has taken any process
  of the container. Memory is blamed only with exit code 137.

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
