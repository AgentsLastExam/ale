# gVisor runtime

This directory builds ALE's gVisor runtime: `runsc` from an official gVisor release with the
patches in [`patches/`](patches/) applied in order. The patches are ALE's own and are not
sent upstream; a stock `runsc` lacks them.

| Patch | Effect |
|---|---|
| `0001-runsc-rootless-systemd-cgroups` | Under rootless Docker, `--memory` and `--cpus` are enforced through the user's own systemd manager instead of failing or being dropped with `--ignore-cgroups`. |
| `0002-runsc-container-oom-score-adj-on-stubs` | A container that exceeds `--memory` with an `oom_score_adj` above the one it inherits loses the process that maps the most memory, normally the one allocating, as under runc, instead of the whole sandbox ([Out of memory](#out-of-memory)). |
| `0003-runsc-oom-score-adj-of-exiting-process` | Under rootless Docker, a container whose process exits at once starts instead of failing with `open /proc/<pid>/oom_score_adj: permission denied`. |
| `0004-nvproxy-gpu-memory-limit` | A container can be given a limit on the memory it allocates on each of its GPUs; an allocation beyond it fails as out of memory inside the container ([GPU memory limit](#gpu-memory-limit)). |
| `0005-runsc-tmp-on-rootfs-without-overlay` | Under `--overlay2=none`, the `/tmp` of an image whose `/tmp` is empty stays a directory of the container's filesystem on the Host, as under runc, instead of an in-memory tmpfs whose files count against the container's memory and which `docker commit` does not keep. |
| `0006-nftables-batch-subsystem-in-host-byte-order` | The `iptables` 1.8.7 of Ubuntu 22.04, whose libnftnl writes a batch's subsystem id in host byte order, changes nf_tables instead of failing with `Invalid argument`, as Linux takes it. |
| `0007-nftables-xt-tcp-udp-matches` | A rule that the `iptables` 1.8.7 of Ubuntu 22.04 writes with `--sport`, `--dport` or `--tcp-flags`, through the xtables `tcp` and `udp` matches, matches only the packets it names, instead of every one. |
| `0008-netstack-nftables-connection-state-of-its-own` | Under `--TESTONLY-nftables`, a packet that iptables tracks and NATs, as once any of its tables is set, is tracked and NATed by nf_tables as well, each by its own rules, instead of the Sentry panicking (`packet already had NAT`) and ending the sandbox. |

Each patch's header says what it changes, why, and how it was verified.

## The image

`ghcr.io/agentslastexam/ale-gvisor:<version>` holds, under `/gvisor`, the layout of
gVisor's release tarball:

```text
/gvisor/
├── runsc
├── containerd-shim-runsc-v1
└── gvisor-bin/              sidecars runsc runs from next to itself: gvisor_sentry,
                             runsc-netgofer, runsc-metric-server, checkpointgofer,
                             runsc-fd-parking, gvisor-sentry-prewarmer, gvisor-proc-umounter
```

`runsc` and its sidecars come from one build and are installed together. The image's
version is `VERSION` in [`build.sh`](build.sh), and `runsc --version` reports the release
and that version, such as `release-20261109.0+ale.0.1.1`. Nothing in it depends on the
engine, so it has its own version, raised when the build changes.

## Install on a Host

Copy `/gvisor` into a directory of its own version, so that an upgrade never replaces the
binaries of a sandbox that is running:

```bash
version=0.1.1
dir=/usr/local/lib/ale-gvisor/$version
id=$(docker create ghcr.io/agentslastexam/ale-gvisor:$version)
sudo mkdir -p "$dir"
docker cp "$id":/gvisor/. - | sudo tar -x -C "$dir"
docker rm "$id"
"$dir/runsc" --version
```

Register it three times in `/etc/docker/daemon.json`, the same on every Host, with GPUs or
without, and apply it with `sudo systemctl reload docker`, which leaves running containers
alone:

```json
{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/lib/ale-gvisor/0.1.1/runsc",
      "runtimeArgs": [
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all"
      ]
    },
    "runsc-net-raw": {
      "path": "/usr/local/lib/ale-gvisor/0.1.1/runsc",
      "runtimeArgs": [
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all",
        "--net-raw"
      ]
    },
    "runsc-nested": {
      "path": "/usr/local/lib/ale-gvisor/0.1.1/runsc",
      "runtimeArgs": [
        "--overlay2=none",
        "--allow-suid",
        "--nvproxy-allowed-driver-capabilities=all",
        "--TESTONLY-nftables"
      ]
    }
  }
}
```

ALE's `docker:gvisor` leaf starts a sandbox under `runsc`, under `runsc-net-raw` when its
Task needs raw sockets, or under `runsc-nested` when it runs a container engine, and its
preflight refuses a Host where a runtime it needs is missing, is registered with other
arguments than these, or is not this build
([Providers](../../../packages/ale-run/src/ale/run/providers/README.md#docker-on-gvisor)).

- Registered by `path`, `runsc` runs under containerd's runc shim, which passes it
  `runtimeArgs`. gVisor's own shim, `io.containerd.runsc.v1`, would read `runsc`'s flags
  only from a TOML file that `ConfigPath` names in the runtime's options; its
  `/etc/containerd/runsc/config.toml` holds only the shim's own settings.
- `--overlay2=none` leaves a container's writes in its Docker filesystem, where Docker
  and the Host see them, as under runc; gVisor's default overlay keeps them inside the
  sandbox.
- `--allow-suid` makes a setuid program, such as `sudo`, run with its owner's identity, as
  under runc; `runsc` otherwise ignores the setuid bit.
- `runsc` turns on nvproxy, its proxy of the NVIDIA driver, for a container that
  `docker run --gpus` gives GPUs, and only for it: a container without GPUs never touches
  the driver, while `--nvproxy` would turn nvproxy on, and probe the driver, for every
  container. `--gpus` needs the NVIDIA Container Toolkit on the Host, as under runc.
  `--nvproxy-allowed-driver-capabilities=all` allows the driver capabilities `compute`,
  `utility`, `graphics`, `video` and `ngx`, not only gVisor's default `compute,utility`;
  a container that asks for another, such as `display`, fails to start. Without GPUs it
  changes nothing.
- `--net-raw` gives a container the raw and packet sockets Docker's default capabilities
  hold, which `runsc` otherwise removes: ping works and a packet socket captures, while a
  packet socket's write fails (`ENOTSUP`) without `--allow-packet-socket-write`, which ALE
  does not pass.
- `--TESTONLY-nftables` gives the Sentry's network stack nf_tables, which the `iptables` of
  Debian, Ubuntu and Alpine (its `nf_tables` variant), Docker and Podman set their rules
  through; without it they fail with `Failed to initialize nft: Protocol not supported`.
  The Sentry's netlink then takes nf_tables messages from a process that holds
  `CAP_NET_ADMIN` in a network namespace, as any process does in one of its own, so only
  the runtime for container engines passes it. It goes without `--net-raw`: the `docker.io`
  dockerd 29.1.3 of Ubuntu 22.04 and 24.04 fails to start a container when it can open a
  packet socket but not write one (`failed to advertise addresses: operation not
  supported`), and only warns when it cannot open one.
- For a container with OCI hooks, as `--gpus` adds, `runsc` refuses to mount into the
  root filesystem through a symlink, so Docker's data root must be given as a path
  without one. Reached through a symlinked home directory, for instance, it keeps GPU
  containers from starting on a Host with NVIDIA's fabric manager, whose socket the
  NVIDIA Container Toolkit mounts into them.

Under rootless Docker, copy `/gvisor` without `sudo` into a directory of the user's, such as
`~/.local/lib/ale-gvisor/$version`, register it in `~/.config/docker/daemon.json`, and run
`systemctl --user reload docker`. Limits need the systemd cgroup driver and a user manager
that delegates the `memory`, `cpu` and `pids` controllers; `runsc` reaches the manager over
the session bus at `$DBUS_SESSION_BUS_ADDRESS`, else `$XDG_RUNTIME_DIR/bus`, else
`/run/user/<uid>/bus`.

## Out of memory

A sandbox's processes start with the `oom_score_adj` of the containerd shim that starts
them, one above containerd's own: -998 under rootful Docker, and under rootless Docker one
above the user's containerd, such as 1 where it runs at 0. On the systrap platform, the
default, the application runs in stub processes. Stock `runsc` gives the Sentry the
container's value and leaves the stubs at the inherited one; with patch 0002 it gives the
stubs the container's value and keeps the Sentry and the gofer at the inherited value, or
at the container's where that is lower. On a platform without stubs, such as KVM, the Sentry
gets the container's value.

Anonymous memory allocated past `--memory`, in steps, with this build:

| Docker | Container's `oom_score_adj` | Ranks | What the OOM killer took |
|---|---|---|---|
| Rootful (29.1.3, Linux 7.0) | 0, Docker's default | stubs 0; Sentry and gofer -998 | 1 of 25 allocations ended the whole sandbox; 2 of the 13 checked for it also took another process |
| Rootful | 500 | stubs 500; Sentry and gofer -998 | in 20 of 20, neither the sandbox nor another process |
| Rootless (29.9.3, Linux 5.15, systemd 249) | 0, Docker's default | all 0, a tie | with the container base's desktop, the whole sandbox in 3 of 3, and once as the desktop started; in a bare Python image, only the allocating process in 3 of 3 |
| Rootless | 500 | stubs 500; Sentry and gofer 1 | in 15 of 15, 10 of them with the desktop, neither the sandbox nor another process |

Rootful Docker's default already ranks every stub above the Sentry, and these runs are too
few to tell 0 from 500 there; rootless Docker's default ties them, and when the OOM killer
takes the Sentry the sandbox ends. A gVisor container therefore needs `--oom-score-adj`
above the inherited value; ALE passes 500. `runsc` cannot hold the Sentry lower instead: an
unprivileged process cannot go below its `oom_score_adj_min`, the value its user's systemd
manager was started with (`OOMScoreAdjust` of `user@.service`: 100 in systemd 259, unset
and so 0 in systemd 249).

Memory that the application writes into a tmpfs inside the sandbox, such as `/dev/shm`,
with `write(2)` is mapped by the Sentry itself, so a container that fills its limit that
way loses the whole sandbox whatever the ranks: under rootful Docker, 2 of 2 at 0 and 4 of
4 at 500. Under runc the OOM killer ends its processes, largest first, until it reaches the
writer. Written through `mmap`, it is the writer's, and the ranks decide: only the writer
died under rootful Docker at 0 (3 of 3) and rootless Docker at 500 (2 of 2), and the whole
sandbox under rootless Docker at 0 (2 of 2).

## GPU memory limit

A container asks for a limit on the device memory it may allocate on each of its GPUs, in
bytes, with the annotation `dev.gvisor.flag.nvproxy-gpu-memory-limit`:

```bash
docker run --runtime runsc --gpus device=GPU-... \
    --annotation dev.gvisor.flag.nvproxy-gpu-memory-limit=$((8 << 30)) IMAGE
```

The limit applies to each GPU separately and is shared by all the container's processes.
It counts every allocation of device memory as the driver sizes it, in whole pages, so
that a 3 MiB `cuMemAlloc` counts 4 MiB: `cudaMalloc`, `cuMemAlloc`, `cuMemCreate`,
PyTorch's allocator, and the buffers of each CUDA context, 516 MiB per process on an H100
with driver 570 and 608 MiB under PyTorch 2.7. An allocation beyond the limit fails as on
a full GPU: CUDA returns `CUDA_ERROR_OUT_OF_MEMORY` (`cudaErrorMemoryAllocation`), PyTorch
raises `torch.OutOfMemoryError`, and the process and the container go on; a CUDA context
that does not fit fails to be created with the same error. Memory stays counted while the
container keeps it allocated: through a handle to it in any of its processes, one opened
with `cuIpcOpenMemHandle` included, an export to a file descriptor
(`cuMemExportToShareableHandle`) until every process holding the descriptor has closed
it, an import of one, or a mapping of it (`cuMemMap`) until it is unmapped. It returns to
the limit when the last of these goes, at the latest when the processes holding them
exit. Without the annotation there is no limit.

The patch's header lists the other ways the driver keeps memory allocated that nvproxy
counts, such as graphics semaphore surfaces, and the requests it refuses under a limit
because it could not count them: an allocation at a fixed physical address fails as
unsupported, as does an export of device memory to a dma-buf for GPUDirect RDMA, and an
allocation under a device object that the process duplicated or imported, rather than
allocated itself, fails as out of memory. These follow the driver's source and were not
tested on a GPU. CUDA 12.8 and PyTorch 2.7 made no fixed-address allocation and none under
such a device in the tests, which did not enable the `rdma` driver capability that dma-buf
export needs.

Not counted:

- memory the driver allocates for itself, such as channels, context buffers and page
  tables: 6 MiB per CUDA context on an H100 with driver 570, so it grows with the number
  of contexts a container creates;
- managed memory (`cudaMallocManaged`), whose pages the driver moves to the GPU on use and
  evicts when the GPU is full.

Apart from the limit, a single `cudaMallocManaged` (`cuMemAllocManaged`) of 1536 MiB or
more did not return within 60 s under gVisor, in the upstream release as in this build:
libcuda retries `UVM_VALIDATE_VA_RANGE`, which keeps failing with
`NV_ERR_INVALID_ADDRESS`. 1024 MiB works. Under runc on the same Host and driver,
`cuMemAllocManaged` of 1024, 1536 and 4096 MiB and `cudaMallocManaged` of 2048 MiB work.

`cuMemGetInfo` (`cudaMemGetInfo`), `torch.cuda.mem_get_info`, NVML and `nvidia-smi` in the
container report the whole GPU, as under runc: its total memory and the memory that every
process on it uses, other tenants' included, not the limit. NVML lists the container's
processes as one, the Sentry, by its Host PID. A framework that sizes its memory pool from
them must be configured to stay within the limit.

The limit was tested with the CUDA 12.8 driver and runtime APIs and PyTorch 2.7 on H100s
with driver 570; graphics, video and GPUDirect RDMA use of the GPU were not tested.

## Build

```bash
images/runtimes/gvisor/build.sh WORKDIR
```

The script checks out `GVISOR_TAG`, fails unless it still names `GVISOR_COMMIT`, applies
every patch in `patches/` in name order, and builds `//debian:gvisor-release-tar-bz2` with
gVisor's Makefile, which runs Bazel in gVisor's builder container with the options of
gVisor's own release pipeline. It then checks the tarball's layout and the version `runsc`
reports, and builds `ghcr.io/agentslastexam/ale-gvisor:$VERSION` from this directory's
`Dockerfile`. When that version is already published, it warns that the local image only
shadows the published one. It needs a rootful Docker daemon and about 15 GB in `WORKDIR`
and Docker's storage together. On a 112-core Host it takes 5.5 minutes with an empty
`WORKDIR`, plus the pull of gVisor's builder image the first time, and under 2 minutes
again with the Bazel cache in `WORKDIR`.

`.github/workflows/images.yml` runs it and publishes each version once. To move to another
gVisor release, set `GVISOR_TAG` and `GVISOR_COMMIT`, rebase the patches, verify them again,
and raise `VERSION`; a patch added or changed also raises `VERSION`.
