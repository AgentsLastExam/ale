# Slurm provider

OpenRSI's H100 and B200 compute nodes run rootless Podman without a delegated user cgroup.
Slurm owns resource enforcement: it limits each step of a job on its own, at
`job_<id>/step_<n>/user` (memory, zero swap and a cpuset narrowed to the step), below the
job's limit, and a step that asks for `--gres=gpu:0` in a GPU job sees no GPU. Bind each
Sandbox's step to threads: Slurm can allocate both hardware threads of a core when one CPU
is requested.

One allocation serves a solver and every Sandbox booted from its snapshots, as the
episode boots them one at a time: a validation round's verifier and next solver, or a
snapshot verifier, start from the node's own Podman store, with no copy and no second wait
in the queue. It is sized for the largest of the solver and the verifier its request names,
and holds that for its whole life; that cost was accepted for large checkpoints and rounds
on a busy queue. A boot that does not fit beside what runs gets an allocation of its own,
loading the snapshot from a `podman save` archive on the shared home, as does a snapshot the
Run keeps, whose allocation ends with the episode.

The batch step, not the Host, starts each Sandbox's step (`srun --exact`), so a dropped login
connection never ends it; the Host only writes the request and reads the step's files on the
shared home. With `--ignore-cgroups` alone, one allocating process could take down the entire
Sentry, so the runtime reads the nearest cgroup above the step that has a `memory.max`,
without joining or modifying it. The batch step holds Podman's pause namespace through
cleanup. Each job needs private XDG, graph, run and temporary directories so concurrent jobs
of one UID can end independently.

A job's store on the node's `/tmp` outlived it in three ways, 88 of them on two nodes: Podman
sets an empty store up again at its path as `podman unshare` exits, Python ends at once on
the SIGTERM Slurm sends a cancelled or preempted job, and a job that Slurm kills at its wall
time, or whose node goes down, cleans up nothing. The batch step removes what Podman left
after its release, turns SIGTERM into an exit so that its cleanup runs, and, as it starts,
removes the stores on its node of this account's jobs that have ended, which each store
names in its `job` file.

A commit chain of 200, each booted from the one before, worked under rootless Podman 4.9.3
on Linux 6.8, which is the declared `max_saves`. A container engine's storage is volumes,
which a commit does not keep, so that profile saves no snapshot.

Podman uses NVIDIA CDI devices by UUID. Setting NVIDIA_VISIBLE_DEVICES to void disables
the duplicate NVIDIA hook. Register the injected libraries inside the sandbox. For nested
Docker, Podman's default capability set omits capabilities the inner Docker expects.
The restricted gVisor runtime and the engine's storage volumes remain per sandbox.

The Slurm step carrying the host tunnel needs unbuffered binary I/O. Normal step
output can wait for a newline in a protocol whose first frame contains none.
