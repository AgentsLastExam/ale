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
