# Slurm provider

OpenRSI's H100 and B200 compute nodes run rootless Podman without a delegated user cgroup.
The single-node job owns resource enforcement. Bind the worker step to threads: Slurm can
allocate both hardware threads of a core when one CPU is requested.

With `--ignore-cgroups` alone, one allocating process could take down the entire Sentry.
Slurm's runtime reads the job cgroup without joining or modifying it. The batch step
holds Podman's pause namespace through cleanup. Each job needs private XDG, graph, run
and temporary directories so concurrent jobs of one UID can end independently.

A job's store on the node's `/tmp` outlived it in three ways, 88 of them on two nodes: Podman
sets an empty store up again at its path as `podman unshare` exits, Python ends at once on
the SIGTERM Slurm sends a cancelled or preempted job, and a job that Slurm kills at its wall
time, or whose node goes down, cleans up nothing. The batch step removes what Podman left
after its release, turns SIGTERM into an exit so that its cleanup runs, and, as it starts,
removes the stores on its node of this account's jobs that have ended, which each store
names in its `job` file.

Podman uses NVIDIA CDI devices by UUID. Setting NVIDIA_VISIBLE_DEVICES to void disables
the duplicate NVIDIA hook. Register the injected libraries inside the sandbox. For nested
Docker, Podman's default capability set omits capabilities the inner Docker expects.
The restricted gVisor runtime and the engine's storage volumes remain per sandbox.

The Slurm step carrying the host tunnel needs unbuffered binary I/O. Normal step
output can wait for a newline in a protocol whose first frame contains none.
