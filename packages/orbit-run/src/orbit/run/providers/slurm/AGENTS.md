# Slurm provider

Decided: Orbit runs on a login node of the cluster, never on a machine that reaches one, and
a Run drives that one cluster; joining clusters or other machines is not this Provider's.
OpenRSI's login nodes have no Docker, no Podman and no subordinate ID range for the account,
so no container runs there, and images are prepared on the cluster. Measured on OpenRSI's
compute nodes (Podman 4.9.3, Buildah 1.33.7): `COPY --chmod` and
`RUN --mount=type=bind,from=<build context>`, which the framework layer uses, work, and a
heredoc in `RUN`, which BuildKit reads, does not; none of the Task repositories'
Dockerfiles, about 50, uses one. Tags are pinned by digest before the key is taken, since
the cache keeps an image by its key and a moved tag is another image.

The home is NFS (version 3, default attribute caching), whose client trusts its cached
listing of a directory, so a file another machine just made can be missing to a lookup for
up to a minute. Measured on the login node: a file a job wrote showed 20 s later to a plain
lookup, and within the 0.5 s it was polled at once the directory was listed first, as
opening a directory makes the client read it again. Every file Orbit waits for in a job's
directory is therefore looked up after its directory is listed, and a preparation job's
result, which it writes as it ends, is still read for 90 s after the job ends before its
absence is a failure.

OpenRSI's QoS wall times are 3 h (`short`), 6 h (`dev`, `mid`) and 12 h (`long`, `lowprio`),
and the others preempt `lowprio` with 120 s of grace (`PreemptMode=REQUEUE`, which cancels
Orbit's `--no-requeue` jobs). An allocation's deadline is read from Slurm's own count rather
than from a clock, and `--signal` is not used: everything before the save is Orbit's, on the
login node, and Slurm states the end exactly as the job starts, so a signal to the batch
step would only be a later second path. Decided: an allocation that Slurm ends first,
preempted or on a failed node, loses its node's store and what the Sandbox held, and the
episode fails, to be retried from its start where the Run allows retries, rather than
resumed from periodic write-outs, which would cost a whole image's `podman save` to NFS each
time and lose the work since the last one all the same.

Measured on OpenRSI (`cpu_1`, a 1-CPU Sandbox of `container-ubuntu22-base`, wall times of 10
minutes, 2 of them to drain): an episode of 720 s of the agent's work ran in three
allocations, on both nodes, each run cut 120 s before its allocation's deadline; saving the
solver, writing it out, ending the allocation, queueing and booting it in the next took 140
s and 214 s, none of it the agent's.

OpenRSI's H100 and B200 compute nodes run rootless Podman without a delegated user cgroup.
Slurm owns resource enforcement: it limits each step of a job on its own, at
`job_<id>/step_<n>/user` (memory, zero swap and a cpuset narrowed to the step), below the
job's limit, and a step that asks for `--gres=gpu:0` in a GPU job sees no GPU. Bind each
Sandbox's step to threads: Slurm can allocate both hardware threads of a core when one CPU
is requested.

One allocation serves a solver and every Sandbox booted from its snapshots, as the episode
boots them one at a time: a validation round's verifier and next solver, or a snapshot
verifier, start from the node's own Podman store, with no copy and no second wait in the
queue. It is sized for the largest of the solver and the verifier its request names, and
holds that while the episode works there; that cost was accepted for large checkpoints and
rounds on a busy queue. Decided: once nothing runs there and every snapshot it holds is
written out with no Sandbox to boot from it there, as when a verifier went to an allocation
of its own, it ends at once rather than idle until the episode ends, holding its GPUs. A
round's next solver that could have had the rest of it waits in the queue instead, which
spends none of the agent's time; the boot went elsewhere because the allocation was near its
end or full anyway.

Decided: an allocation mid-episode, one that boots a snapshot or a separate verifier's,
waits in the queue for as long as Slurm keeps it pending, rather than for
`queue_timeout_sec`: giving up there ended the episode, and for a snapshot destroyed it, the
agent's only state, over a wait that spends none of its time.

A snapshot written out, the image's `podman save` archive and each volume layer's tar, is
not compressed. Measured on OpenRSI (`cpu_1`, 8 CPUs, Podman 4.9.3) with a solver of
`pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel` and 2 GiB of random data written in it, 15.5
GB: committing it took 11 to 13 s; `podman save | gzip -1` to the shared home took 378 s for
9.8 GB, as one gzip process writes 41 MB/s, and `podman load` of that took 321 s; `pigz -1`
on 8 threads took 89 s; uncompressed, the save took 46 s, 2 s more than to `/dev/null`, and
the load 189 s. A move's save must fit in `drain_minutes`, which the gzipped one barely did
at the default of 10, and the next allocation's load lengthens the move's gap; a carried
snapshot is removed once the next save succeeds, so the space is held briefly.

A boot that does not fit beside what runs gets an allocation of its own, loading the
snapshot from a `podman save` archive in the cache, as does a snapshot the Run keeps,
whose allocation ends with the episode.

The batch step, not Orbit, starts each Sandbox's step (`srun --exact`), so Orbit's own
process ending never ends it; Orbit only writes the request and reads the step's files in
the cache. With `--ignore-cgroups` alone, one allocating process could take down the
entire Sentry, so the runtime reads the nearest cgroup above the step that has a
`memory.max`, without joining or modifying it. The batch step holds Podman's pause namespace
through cleanup. Each job needs private XDG, graph, run and temporary directories so
concurrent jobs of one UID can end independently.

A job's store on the node's `/tmp` outlived it in three ways, 88 of them on two nodes: Podman
sets an empty store up again at its path as `podman unshare` exits, Python ends at once on
the SIGTERM Slurm sends a cancelled or preempted job, and a job that Slurm kills at its wall
time, or whose node goes down, cleans up nothing. The batch step removes what Podman left
after its release, turns SIGTERM into an exit so that its cleanup runs, and, as it starts,
removes the stores on its node of this account's jobs that have ended, which each store
names in its `job` file.

A commit chain of 200, each booted from the one before, worked under rootless Podman 4.9.3
on Linux 6.8, which is the declared `max_saves`.

A volume must reach the Sandboxes a snapshot serves without a copy, a separate verifier of
another image included, and a commit keeps no volume. A volume is therefore a layer in the
allocation's store: a Podman volume, which Podman fills from the image, or a plain
directory, an overlay's upper layer or a copy of an engine's storage. A view is a kernel
overlay that the step mounts with `podman unshare` in the allocation's own rootless
namespace, which every step and container of the job shares: measured on OpenRSI (Linux
6.8), an unprivileged overlay mounts there with `userxattr`, gVisor's gofer serves it to a
container as any bound directory, a deletion through it is a whiteout that a later view
honours, and a container sees the mount because Podman's containers start in that namespace.
Each layer is named by a short link below the store, `l/<layer>`, and the overlay is mounted
from the store's root, so its options, which must fit in a page, name a chain of 200 layers;
Docker's own overlay driver does the same. A container engine's storage is copied, never
viewed, as on Docker. A snapshot written out for another allocation carries each layer as a
tar that `tar --xattrs --xattrs-include=user.*` writes in the namespace, which keeps owners,
whiteouts and the overlay's opaque directories; it is not compressed, as a volume's output
often is already.

A Sandbox's `/dev/shm` is Podman's `--shm-size` of half its Task's memory
([`memory.shm_mb`](../memory.py)). Podman makes it a tmpfs on the node and binds it into the
container, so runsc reaches it through its gofer (`9p`), where it mounts Docker's as a tmpfs
of the Sentry; its pages are charged to the step's cgroup all the same. A `--tmpfs` at
`/dev/shm`, which replaces Podman's with a tmpfs of the Sentry as under Docker, was slower.
Measured on OpenRSI (Podman 4.9.3, Orbit's runsc 0.1.4, November 2026), with PyTorch's
DataLoader of 8 workers and batches of 64 images of 3×224×224 float32 on one H100, in a
Sandbox of 16 CPUs and 16 GiB, three passes each, in both orders:

| `/dev/shm` | Samples per second |
|---|---|
| 64 MiB | none: a bus error in a worker at the first batch |
| `--shm-size`, 8 GiB over `9p` | 1795, 2170, 2155; 1720, 2152, 2152 |
| `--tmpfs`, 8 GiB in the Sentry | 1231, 1507, 1494; 1232, 1497, 1497 |

In a 2048 MiB Sandbox, 1024 MiB were written before a write failed with `ENOSPC`; with
`/dev/shm` full, one process allocating held 568 MiB and was killed alone, and the Sandbox
answered after it; 400 processes of 16 MiB ended the whole Sandbox as `MemoryExhaustedError`
(the Sentry counted 63 to 67 kills, then the step exited 137) with `/dev/shm` full or empty,
under either flag.

Podman uses NVIDIA CDI devices by UUID. Setting NVIDIA_VISIBLE_DEVICES to void disables
the duplicate NVIDIA hook. Register the injected libraries inside the sandbox. For nested
Docker, Podman's default capability set omits capabilities the inner Docker expects.

The Slurm step carrying the Gateway tunnel needs unbuffered binary I/O. Normal step
output can wait for a newline in a protocol whose first frame contains none.

## Sandboxes of several nodes

Decided: a Sandbox of several nodes runs under gVisor, as one of a single node does, with
gVisor's RDMA proxy (`--rdmaproxy`), not under a runtime that shares the node's kernel. On
OpenRSI's H100 nodes (November 2026) gVisor kept up with the bare nodes on InfiniBand
([README](../README.md#slurm-nodes) gives the figures), and so did crun: `ib_write_bw` of
1 MiB across the two nodes reached 385.9 Gb/s under crun, against 389.6 bare. Docker nested
in a gVisor Sandbox ran containers on the nodes' network, and could not under crun, under
runc or in its rootless mode alike: Slurm's cgroups are read-only to it
(`mkdir /sys/fs/cgroup/docker: read-only file system`). Without `--rdmaproxy`, NCCL in
gVisor falls back to TCP sockets.

Rootless Podman cannot make device nodes, so it turns `--device /dev/infiniband/uverbsN`
into a bind mount, while the RDMA proxy registers only the devices the OCI spec lists in
`linux.devices`; the Sandbox then saw no device ("Function not implemented"). Orbit's gVisor
patch 0012 takes such a bind mount as the device under `--rdmaproxy`. Its patch 0011
backports upstream's acceptance of mlx5's data-direct calls, without which NCCL falls back
to TCP in images whose rdma-core is newer. A node gives a Sandbox only the HCAs whose port
is InfiniBand: OpenRSI's `mlx5_0` is the Ethernet NIC of the node's own network, through
which RoCE traffic would leave past every filter. `rdma_cm` is not needed, and `umad` and
`issm` are root's on the node. OpenRSI's InfiniBand ports are full members of partition
`0x43df` and limited members of the default one; without `umad` a node cannot list the
partition's other members.

Decided: the nodes' network is a router namespace on each node, which the Sandbox never
enters, rather than a WireGuard device in the Sandbox's own namespace. A nested-containers
Sandbox's root holds `NET_ADMIN` there; measured: such a root that routed everything through
its interface to the other nodes still reached only them, the rest refused by the router.
The router forwards by policy routing alone, so the nodes need nothing past iproute2 for it,
and the WireGuard device takes from each peer only that peer's address. The nodes have no
`wg`, so the worker sets the device's key and peers over generic netlink and derives each
public key with X25519 itself. Neither the router's private key nor the nodes' ssh keys
are ever on a command line: other accounts' jobs share a node, and any account reads
`/proc/<pid>/cmdline`. Each node writes its router's key and endpoint below the Sandbox's
directory, and the others read them after listing the directory, as for every file another
machine writes there.

Measured: runsc refuses to join a pod's UTS namespace ("joining UTS namespace … is not
supported"), so the pods share only their network namespace and each container sets its own
hostname. A container in a pod gets the pod's `/dev/shm`, 64 MiB unless the pod names
another size, and not its own `--shm-size`, under crun and runsc alike; with that, NCCL's
ranks failed to make their segments ("No space left on device"), so the Sandbox's pod is
made with the Sandbox's size. pasta gives the router namespace a copy of the node's address,
so two routers on one node could not reach each other through it; a Sandbox's nodes are
always distinct.

Decided: the nodes' variables (`ORBIT_NODES`, `ORBIT_NODE_RANK`, NCCL's and Gloo's) are
given to every command, by the Host to node 0's and by each node's sshd to its sessions,
never set on the container: `podman commit` keeps a container's environment, so a
Sandbox of one node, or on nodes without InfiniBand, booted from the snapshot would inherit
`NCCL_NET=IB` and an interface it lacks.

Decided: an agent starts processes on another node with ssh, through Orbit's own sshd on
each node, rather than through a command of Orbit's own or Slurm. Asked only to train on
node0 and node1 with nothing said of how, Codex's ultima-alpha (six times) and Claude Opus,
Sonnet and Haiku (once each) all reached node1 first with `ssh node1`, as people on a
cluster do, and `mpirun`, `pdsh` and DeepSpeed use it, so an agent needs to learn nothing;
Kubeflow's MPI Operator, which replaced ssh with `kubectl exec`, went back to ssh. Slurm's
own `srun` would hand the agent the job's Slurm credentials, which reach outside the
Sandbox. Images have no sshd, so the framework layer of a Task one of whose Sandboxes spans
several nodes adds a static OpenSSH, without OpenSSL (5.4 MB in all), as it adds guestd and
the framework's Python: the image carries it wherever it runs, under any runtime and
Provider, and a snapshot keeps it, where a mount would need each Provider to fetch and mount
it on every boot, which a remote Provider cannot. The image's own `ssh` client, where it has
one, works with it, as the keys and known hosts are at the paths every OpenSSH client reads.
A session gets the container's variables and its node's through sshd's `SetEnv`, as a plain
sshd would drop the image's `PATH` (a conda image's `python` was then the system's): tested
on the nodes, sshd 10.2 parses quotes, backslashes, `#` and empty values there as given. An
sshd without PAM refuses an account locked in `/etc/shadow` even its key, and `useradd`
leaves an account so, as in PyTorch's image, so the OpenSSH is built without shadow
passwords and never reads it.

Decided: the nodes share `/shared`, a directory of the cache on the cluster's shared file
system (OpenRSI's NFS home), rather than each
node holding only its own disk: a program edited on node 0 and run on node 1 from a stale
copy fails without a word, and data copied to every node takes each node's time. Measured on
OpenRSI (NFSv3, 100 Gb/s), from a node: 0.7 GB/s writing and 3.0 GB/s reading one stream,
2.9 GB/s writing and 7.1 GB/s reading eight, against the node's own disk's 0.7, 3.2, 0.9 and
13.1; 1000 files of 1 KiB took 3.9 s against 0.03 s, so a venv or a build belongs on a
node's own disk. A file another node creates appeared on a node already polling for it after
10 to 25 s, in gVisor, crun and on the bare node alike (NFS's cached lookups); edits,
renames and deletions were seen at once; of 400 lines appended to one file by both nodes at
once, 238 to 360 were kept. gVisor needed two changes, patch 0013: NFS lets the job's
account own files for itself alone, so gVisor's chown of a new file to the creating
account's mapped uid failed (`Operation not permitted`, only root could create files), and a
directory the Sandbox had listed stayed as listed, as gVisor reread it through a descriptor
it kept open, which the NFS client answers from its cache (Python could not import a module
another node added). Its snapshot keeps it, and a Sandbox that may change it gets a copy
rather than the snapshot's own, so a snapshot stays what every Sandbox booted from it, and a
failed move's retry, start from; a verifier gets it read-only, so that nothing it writes
reaches the agent's next turn.

Decided: a Sandbox that names `storage_mb` is measured by its own step on each node, which
holds its store and so reads it at the disk's speed, rather than by the Host, which would
reach the store only through Slurm steps. How often follows the room left
(`providers/storage.py`), as measuring a Sandbox of many files every 5 s far below a large
limit costs a walk for nothing.

A hold step removes its node's store once the job is stopped or, when the batch step fails,
released by a file it polls: srun turns a SIGTERM into SIGKILL for its tasks, which would
leave the store. Measured: after each of six such removals, the store's directory came back
empty, naming no job; a job starting on the node removes the empty ones of this account
older than ten minutes.

## Ending a guest session

Each guest session, guestd's two, the memory watcher's and a node's Gateway tunnel, is an
`srun` on the login node whose step runs `podman exec`. Podman runs a monitor, conmon, for
the session in that step, which writes the session's exit code to a file below the
container's directory as it ends. Killing the `srun` ends the step and the monitor with it,
before that file is written: on OpenRSI (Podman 4.9.3, November 2026) a Sandbox's container
had four exec monitors on its node, and three 5 s after one session's `srun` was killed.
`podman stop` then stops the container, but its cleanup waits 5 s for that file and fails
(`timed out waiting for file …/userdata/<session>/exit/<container>`, exit 125), and the save
with it. Closing a tunnel used to kill its `srun`, so the first save of every Sandbox that
reached a Gateway failed this way, and with it every run of a real agent's CLI, at its first
validation round, snapshot verifier, separate verifier of its volumes or move.

The Host ends a session as a command ends: it closes the session's input, which ends the
guest's end of it once that reads it, and kills the `srun` only if it still runs 5 s later
([`process.end`](../../process.py)). Measured there: every session of a saved Sandbox ended
so, with exit 0, 1.05 to 1.22 s after its input closed.
