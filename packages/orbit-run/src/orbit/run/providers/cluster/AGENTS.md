# Cluster provider

Decided: one Provider, `ClusterProvider`, serves every workload manager, named by the one a
Run's configuration selects (`slurm`, `pbs`, `lsf`), as everything but a job's commands and
how a Sandbox is held to its share of the job is the same on each. A workload manager's
module (`slurm.py`, `pbs.py`, `lsf.py`) says how a job is submitted, watched, found by its
unique name and cancelled, and what in its environment names it; Slurm holds each Sandbox in
a step of the job, and PBS and LSF, which start no steps, delegate the job's cgroup, which
`delegated.py` and the worker's `delegated_worker.py` split for Orbit and each Sandbox. A
cluster's configuration names the same common settings whatever its workload manager, and
adds only its own (Slurm's `qos`, `constraint` and nodes per job; PBS's select chunk), so
the Provider asks no question of which workload manager it serves.

Decided: a Run is driven from a login node of the cluster, never from a machine that reaches
one, and drives that one cluster; joining clusters or other machines is not this Provider's.
Each episode runs in a batch job of its own, the login node's Orbit only submitting it and
waiting for its result, and Orbit's process in the job, on its first node, runs the episode
whole, its protocol, Harness, Gateway and Sandboxes, holding the job as the allocation of
every Sandbox: no control channel from the login node is needed, so PBS's and LSF's file
bridge over the shared file system goes, and its latency with it, and the login node holds
no long-lived process.
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
Orbit's `--no-requeue` jobs). A job's deadline is the end Slurm states in its environment as
it starts (`SLURM_JOB_END_TIME`), else its start and wall time, less the drain, which the
job itself computes, asking Slurm nothing; `--signal` is not used: everything before the
save is Orbit's, in the job, and Slurm states the end exactly as the job starts, so a signal
to the batch step would only be a later second path. Decided: a job that Slurm ends first,
preempted or on a failed node, loses its node's store and what the Sandbox held, and Orbit's
own process with it, and the episode fails, to be retried from its start where the Run
allows retries, rather than resumed from periodic write-outs, which would cost a whole
image's `podman save` to NFS each time and lose the work since the last one all the same.

Measured on OpenRSI (`cpu_1`, a 1-CPU Sandbox of `container-ubuntu22-base`, wall times of 10
minutes, 2 of them to drain), with Orbit on the login node: an episode of 720 s of the
agent's work ran in three allocations, on both nodes, each run cut 120 s before its
allocation's deadline; saving the solver, writing it out, ending the allocation, queueing
and booting it in the next took 140 s and 214 s, none of it the agent's.

OpenRSI's H100 and B200 compute nodes run rootless Podman without a delegated user cgroup.
Slurm owns resource enforcement: it limits each step of a job on its own, at
`job_<id>/step_<n>/user` (memory, zero swap and a cpuset narrowed to the step), below the
job's limit, and a step that asks for `--gres=gpu:0` in a GPU job sees no GPU. Bind each
Sandbox's step to threads: Slurm can allocate both hardware threads of a core when one CPU
is requested.

One job serves its episode, a solver and every Sandbox booted from its snapshots, as the
episode boots them one at a time: a validation round's verifier and next solver, or a
snapshot verifier, start from the node's own Podman store, with no copy and no second wait
in the queue. It is sized for the largest of the solver and the verifiers, a separate one
included, and holds that while the episode works there; that cost was accepted for large
checkpoints and rounds on a busy queue. Decided: no Sandbox of an episode gets an allocation
of its own, and the job's allocation ends with the episode, not as it idles: the Provider in
the job never submits a job, so a Sandbox that does not fit beside what runs waits for room
there, and one the job would not last is refused, except a solver booted from a snapshot the
job saved: that snapshot is written out instead, and the episode's next job, which the login
node submits, boots from it (`SandboxHandOff`). A snapshot written out by an earlier job
that this one would not last is refused too, as a later job would not last it either and the
episode would never end.

Decided: the job's worker is a child process of Orbit's, never Orbit's own code, and runs
until Orbit closes the job's allocation: it changes its environment and its signal handling
and blocks as it waits, and it alone holds the stores. It gets none of the Host's
credentials, which reach the job with Orbit, as no process Orbit starts on the cluster does:
`sbatch` and `srun` hand the environment they are given to the job's processes.

A snapshot written out, the image's `podman save` archive and each volume layer's tar, is
not compressed. Measured on OpenRSI (`cpu_1`, 8 CPUs, Podman 4.9.3) with a solver of
`pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel` and 2 GiB of random data written in it, 15.5
GB: committing it took 11 to 13 s; `podman save | gzip -1` to the shared home took 378 s for
9.8 GB, as one gzip process writes 41 MB/s, and `podman load` of that took 321 s; `pigz -1`
on 8 threads took 89 s; uncompressed, the save took 46 s, 2 s more than to `/dev/null`, and
the load 189 s. A move's save must fit in `drain_minutes`, which the gzipped one barely did
at the default of 10, and the next job's load lengthens the move's gap; a carried snapshot
is removed once the next save succeeds, so the space is held briefly. A snapshot the Run
keeps is written out as it is saved, as the job's stores end with the episode.

The worker, not Orbit, starts each Sandbox's step (`srun --exact`), which Slurm holds to its
resources; Orbit only writes the request and reads the step's files in the cache. With
`--ignore-cgroups` alone, one allocating process could take down the entire Sentry, so the
runtime reads the nearest cgroup above the step that has a `memory.max`, without joining or
modifying it. The worker holds Podman's pause namespace through cleanup. Each job needs
private XDG, graph, run and temporary directories so concurrent jobs of one UID can end
independently.

A job's store on the node's `/tmp` outlived it in three ways, 88 of them on two nodes:
Podman sets an empty store up again at its path as `podman unshare` exits, Python ends at
once on the SIGTERM Slurm sends a cancelled or preempted job, and a job that Slurm kills at
its wall time, or whose node goes down, cleans up nothing. The worker removes what Podman
left after its release, turns SIGTERM into an exit so that its cleanup runs, and, as it
starts, removes the stores on its node of this account's jobs that have ended, which each
store names in its `job` file.

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
viewed, as on Docker. A snapshot written out for a later job carries each layer as a
tar that `tar --xattrs --xattrs-include=user.*` writes in the namespace, which keeps owners,
whiteouts and the overlay's opaque directories; it is not compressed, as a volume's output
often is already. A later job loads a snapshot's layers with its image, into its stores as
its worker starts, or, once it runs, by a control command on each node.

A Sandbox's `/dev/shm` is Podman's `--shm-size` of half its Task's memory
([`memory.shm_mb`](../memory.py)). Podman makes it a tmpfs on the node and binds it into the
container, so runsc reaches it through its gofer (`9p`), as it does Docker's under
`docker:gvisor`; its pages are charged to the step's cgroup all the same. A POSIX semaphore
there works between processes only with Orbit's gVisor patch 0015: without it, a process
that opened one by name, as Python's multiprocessing does in its spawn context, never woke
the process that created it, nor was woken by it, and in a Task that serves a model with
SGLang-Omni the server never became ready. A `--tmpfs` at `/dev/shm`, which replaces
Podman's with a tmpfs of the Sentry and needs no patch, passes less than half as many
samples. Measured on OpenRSI (Podman 4.9.3, Orbit's runsc 0.1.0 with and without patch 0015,
gVisor release-20261116.0, November 2026), with PyTorch's DataLoader of 8 workers passing
batches of 64 images of 3×224×224 float32 to the main process, which copies each to one
H100, in a Sandbox of 16 CPUs with 8 GiB of `/dev/shm`, three passes of forked workers and
one of spawned ones each, in both orders:

| `/dev/shm` | Samples per second, forked | Spawned |
|---|---|---|
| 64 MiB | none: a bus error in a worker at the first batch | none |
| `--shm-size` over `9p`, patch 0015 | 4680, 4709, 5260; 4622, 4930, 4739 | 3793; 4103 |
| `--shm-size` over `9p`, without it | 4608, 4685, 4809; 4677, 4946, 4781 | 4079; 4174 |
| `--tmpfs` in the Sentry | 2158, 2117, 2203; 2218, 2082, 1924 | 1944; 1998 |

In a 2048 MiB Sandbox, 1024 MiB were written before a write failed with `ENOSPC`; with
`/dev/shm` full, one process allocating held 568 MiB and was killed alone, and the Sandbox
answered after it; 400 processes of 16 MiB ended the whole Sandbox as `MemoryExhaustedError`
(the Sentry counted 63 to 67 kills, then the step exited 137) with `/dev/shm` full or empty,
under `--shm-size` and `--tmpfs` alike.

Decided: a Sandbox's processes keep the `oom_score_adj` they inherit, 0, rather than the 500
Docker gives its gVisor containers (`gvisor.FLAGS`). With 500, every process's stub ranked
above the Sentry, which stays at 0, for the Host's OOM killer, and killing a stub frees
nothing, as the memory is the Sentry's. Measured on OpenRSI with the 400 processes of 16 MiB
above and the Sandbox left alone for 20 s after its loss: with 500, its Sentry stopped
answering in 12 of 21 runs, its threads all asleep and its log silent, while the Host's OOM
killer went on taking stubs at the step's limit, so Podman's removal waited without end and
the job held its node; with 0, the Host's OOM killer took the Sentry, the largest process,
and the Sandbox ended as `MemoryExhaustedError` and was removed, in 10 of 10. One process
past the memory still dies alone, to the Sentry's own OOM killer. Docker's gVisor Sandboxes
left alone the same way all lived (6 of 6, rootful Docker), so Docker keeps 500.

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
holds its store and so reads it at the disk's speed, rather than by Orbit's process, which
reaches the other nodes' stores only through Slurm steps. How often follows the room left
(`providers/storage.py`), as measuring a Sandbox of many files every 5 s far below a large
limit costs a walk for nothing.

A hold step removes its node's store once the job is stopped or, when the worker fails,
released by a file it polls: srun turns a SIGTERM into SIGKILL for its tasks, which would
leave the store. Measured: after each of six such removals, the store's directory came back
empty, naming no job; a job starting on the node removes the empty ones of this account
older than ten minutes.

## Ending a guest session

Each guest session, guestd's two, the memory watcher's and a node's Gateway tunnel, was an
`srun` on the login node whose step runs `podman exec`, and on the job's other nodes is one
from Orbit's; on Orbit's own node it is `podman exec` alone, whose monitor no step holds.
Podman runs a monitor, conmon, for the session in that step, which writes the session's exit
code to a file below the container's directory as it ends. Killing the `srun` ends the step
and the monitor with it, before that file is written: on OpenRSI (Podman 4.9.3, November
2026) a Sandbox's container had four exec monitors on its node, and three 5 s after one
session's `srun` was killed. `podman stop` then stops the container, but its cleanup waits 5
s for that file and fails (`timed out waiting for file
…/userdata/<session>/exit/<container>`, exit 125), and the save with it. Closing a tunnel
used to kill its `srun`, so the first save of every Sandbox that reached a Gateway failed
this way, and with it every run of a real agent's CLI, at its first validation round,
snapshot verifier, separate verifier of its volumes or move.

The Host ends a session as a command ends: it closes the session's input, which ends the
guest's end of it once that reads it, and kills the `srun` only if it still runs 5 s later
([`process.end`](../../process.py)). Measured there: every session of a saved Sandbox ended
so, with exit 0, 1.05 to 1.22 s after its input closed.

A save whose first `podman stop` fails stops the container once more. Podman records a
session whose exit file it waited for in vain as ended all the same (`getActiveExecSessions`
in Podman's `libpod/container_exec.go`), so the second stop finds the container stopped and
no session running, and completes Podman's cleanup of it, of its network and mounted
storage, which the first left undone. Measured: with one session's `srun` killed, the first
stop failed as above, the second succeeded, and a Sandbox booted from the snapshot read what
the first had written and reached its Gateway.

Decided: both, rather than either alone. Ending sessions by their input spares every save
Podman's 5 s wait for each session and leaves nothing on the node half ended; the second
stop covers an `srun` lost any other way, by the Host's own kill after 5 s or on its node.
The stop is repeated once, on any failure: Podman marks every lost session in the first, so
a second failure has another cause, which it reports.

## PBS and LSF jobs

PBS and LSF jobs hold one node, so Orbit reaches the worker by commands of its own there and
by nothing else. Without Slurm's steps, the worker holds each Sandbox to its CPUs, memory
and no swap in a cgroup of its own, in the job's cgroup v2 hierarchy, which the workload
manager must delegate to the job's account. Its batch script used to run the worker, which
moved itself into a leaf of its own and enabled the controllers on the job's cgroup, which
cgroup v2 allows only of a cgroup that holds no process itself, and killed whatever its
subtree held as it ended. Orbit's process now runs the script, and the worker as its child.

Decided: before the worker starts, Orbit moves its own process into a leaf of the job's
cgroup, `orbit-engine`, enables the memory, CPU and, where the workload manager offers it,
cpuset controllers on the job's cgroup, and gives the worker a sibling subtree,
`orbit-sandboxes`, whose path it names in `delegated.json`, rather than leave Orbit beside
the worker or in its subtree: the job's cgroup then holds no process, and nothing the worker
kills as it ends is Orbit's. A job whose cgroup holds another process, or that the account
may not write, is refused before any Sandbox starts, naming why, rather than run Sandboxes
the job's limit alone holds. A job on a PBS queue whose select chunk leaves out a resource
holds the queue's whole of it, which the job's Orbit admits Sandboxes to.
