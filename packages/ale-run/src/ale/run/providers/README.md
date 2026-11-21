# Providers

Providers turn prepared Task images into Sandboxes. ALE ships the following backends:

| Package | Backend | Prepared input |
|---|---|---|
| `docker/` | local Docker container, a Kata microVM per container ([Kata](#docker-on-kata-containers)), or a container on gVisor ([gVisor](#docker-on-gvisor)) | immutable OCI image identity |
| `slurm/` | a Linux gVisor container in a single-node Slurm batch allocation, beside the Sandboxes booted from its snapshots | immutable OCI image identity |
| `qemu/` | QEMU/KVM guest runner | qcow2 disk, or a chain of [layers](#disk-layers) on one |
| `firecracker/` | Linux microVM on Firecracker/KVM | immutable OCI image identity, converted once to an ext4 disk |
| `daytona/` | Daytona cloud container | immutable OCI image identity, pushed by digest |
| `orchard/` | macOS VM on Orchard/Tart | digest-pinned OCI image, or a local image `build.sh` built |
| `fleet/` | a Sandbox of any of these on a Fleet Node ([Fleet](../../../../../../docs/specs/fleet.md)) | what the Node's leaf takes; each Node prepares its own images |

Each package keeps its Provider and Sandbox in `provider.py` and exports both. Beside it,
`docker/kata.py` builds the tc filters and VM size a sandbox in a Kata microVM needs,
`docker/gvisor.py` the tc filters, netstack chains, size and GPU memory limit of one on
gVisor, `docker/tc.py` the tc commands both use, `docker/gpu_sharing.py` decides which
GPUs a Docker Sandbox takes and watches what a shared one uses, `docker/storage.py` measures
what a Docker Sandbox writes ([Docker writable storage](#docker-writable-storage)), and
`docker/rootless.py` relays a Sandbox's connections to the Host's ports under rootless
Docker;
`daytona/api.py` is Daytona's REST client and `daytona/image.py` adapts and publishes
images; `orchard/client.py` drives Orchard and Tart, `orchard/framing.py` frames images
and `orchard/desktop.py` waits for the desktop; `fleet/tunnel.py` carries a Run's Gateway
to its Sandboxes on a Node, both ends of it, and the node agent itself is the `ale-fleet`
package. Two files run elsewhere and import nothing from the engine:
`firecracker/guest_init.py` is the init every microVM boots, and `qemu/ntfs_discard.py`
runs in the VM builder. `desktop.py` and `privileges.py` serve several Providers, and
`runner.py` holds what QEMU and Firecracker share, as each boots its guest in a runner
container: the guest service's token and connection, the proxy's variables, the guest's
console that shows its OOM kills, and the check for a desktop that has drawn nothing yet.
`memory.py` counts the OOM kills in a gVisor, Kata or Daytona sandbox from inside it
([Memory](#memory)).

They implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
profiles, preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and episode
protocol code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).

## Provider ids and profiles

A Run's `sandbox.providers` and a Node's `--provider` name Providers by provider id, and a
Run gives each Sandbox the first one listed that serves it
([Provider selection](../../../../../../docs/specs/sandbox-image.md#provider-selection)).
In `__init__.py`, `leaf()` builds a leaf from its id, `SCHEDULERS` names the schedulers,
and `ProviderRegistry` builds each Provider of the Run's list when first needed, chooses
among them (`select`) and checks the chosen leaves on the Host (`preflight`). Each Provider
states its capability profiles in `profiles()`, compared by `ale.core.capability.unmet`,
and a leaf the rules no profile can state in `refusals()`, which a scheduler asks of the
leaves its profiles name:

| Provider id | Starts | GPUs | Snapshots | Container features | Also refuses |
|---|---|---|---|---|---|
| `docker:runc` | Linux containers, on the machine's architecture | the Host's NVIDIA GPUs that `sandbox.docker.gpus` allows, by the models and memory nvidia-smi reports, shared by `gpu_memory_mb` and watched ([GPU memory](#gpu-memory)); none without a working nvidia-smi | yes | `raw_sockets` | — |
| `docker:kata` | Linux containers, on the machine's architecture | none | yes | `perf`, `bpf`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | — |
| `docker:gvisor` | Linux containers, on the machine's architecture | as `docker:runc`'s, shared under a limit | yes, but not with `nested_containers` | `user_namespaces`, `fuse` and `mount`, with `raw_sockets` in one profile and `nested_containers` in another | — |
| `slurm` | Linux x86-64 containers on configured partitions | whole GPUs of the configured partition model | 200 in a row, but not with `nested_containers` | as the two gVisor profiles above | a solver that, with the verifier booted from its snapshot, needs more than one node of a partition supplies; explicit writable storage limits |
| `firecracker` | Linux containers on x86-64, up to 32 CPUs | none | yes | `perf`, `bpf`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | — |
| `qemu` | Linux and Windows VMs on x86-64 | those of its VFIO pool, whole, when it is given one; no Run or Node setting gives it one | yes | none: a VM takes none | — |
| `orchard` | macOS VMs on arm64 | none | yes | none: a VM takes none | storage that is not whole GiB; an agent account that is root, or `admin` for an agent without `[agent].sudo` |
| `daytona` (scheduler) | Linux containers on x86-64 | Daytona's, whole | no | `nested_containers`, `fuse`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | memory or storage that is not whole GiB; `gpu_types` naming no Daytona GPU type |
| `fleet` (scheduler) | what the leaves lent by its registered Nodes that run this engine's framework start, online or not, read once per Run | as those leaves | as those leaves | as those leaves | as those leaves |

Every profile applies `open`, `block`, `allowlist` and `blocklist`, apart from Orchard's,
which applies the last three only where setup's Softnet is the worker's; a macOS Sandbox refused
for that names the setup that installs it. A Linux Task's container is x86-64, so a Docker
leaf on an arm64 machine serves none. A Docker leaf reads its GPUs' models and memory once,
from nvidia-smi, so a GPU Task goes to the next listed Provider on a Host whose GPUs it does
not accept or have the memory it needs on each. A Sandbox that names `storage_mb` goes only
to a leaf that holds it to that: under QEMU, Firecracker, Orchard and Daytona by a disk of
its own, at least that size, as each one's section says, and under Docker by measuring what
it writes and ending it past the limit, but in `docker:gvisor`'s profile for
`nested_containers` ([Docker writable storage](#docker-writable-storage)). A Node's profiles
are its leaves', each limited to the CPUs and memory the Node lends in all. A Node lends
Docker under any of its runtimes, QEMU and Orchard, but not Firecracker.

`docker:runc` shares the Host's kernel with its Sandboxes, so a kernel exploit in one
reaches the Host. `docker:kata` and `firecracker` give each Sandbox a VM's kernel and attach
no GPU, so a GPU Task passes them for the next leaf listed; `docker:gvisor` gives each
gVisor's kernel in user space and attaches GPUs through nvproxy, gVisor's proxy of the
NVIDIA driver, whose calls still reach the Host's driver. A Run whose `sandbox.providers`
leaves `docker:runc` out keeps every container Task on a kernel of its own
([Security](../../../../../../docs/specs/security.md)).

## Gateway address

Every Sandbox dials its Gateway at `SANDBOX_GATEWAY_PORT` (7412), whatever port the Host
serves it on.

| Provider | Where the fixed port leads |
|---|---|
| Slurm | guestd listens on loopback inside the container. Its byte stream crosses a second Slurm step to the Host, carrying only the Gateway and, under `allowlist` or `blocklist`, the episode's egress proxy |
| Docker | `ale-gateway.internal` names the Host's address on the container's bridge. The helper that holds `NET_ADMIN` in the container's network namespace keeps an `ALE_GATEWAY` nat chain sending that address's port 7412 to the Gateway's port; a reattaching run flushes and refills it. `open` Tasks run the helper for this rule alone. Under Kata the helper rewrites the port with tc filters instead ([Kata](#docker-on-kata-containers)); under gVisor the same chain is in the sandbox's own network stack, written by a privileged `docker exec` ([gVisor](#docker-on-gvisor)). Under rootless Docker, whose bridges are in a network namespace of rootlesskit's, the Provider listens at the Gateway's port there and relays each connection to the Host ([Docker runtimes](#docker-runtimes)) |
| QEMU | The runner's `ALE_GATEWAY` nat chain sends `172.30.0.1:7412` from the guest bridge to the Host at the Gateway's port, and a reattaching run refills it |
| Firecracker | As QEMU, from the guest's tap device in the runner |
| Daytona | `guestd/tunnel.py` listens at 7412 on the sandbox's loopback |
| Orchard | A relay the Provider starts as root at boot listens at `127.0.0.1:7412` in the VM and connects to the Gateway's port at the Mac, the VM's default IPv4 route; it lives until the VM is deleted, or until a later episode that continues in a retained VM stops it (through `lsof` on the port) and starts its own |
| Fleet | The Node's Provider's own route, to the port where the node agent listens for the Run; the agent carries each connection over the Run's tunnel to its Gateway |

## Egress relay

The egress proxy knows an episode by its egress session's token, which many programs cannot
present: Chrome, Edge, Firefox and PowerShell 5.1's `Invoke-WebRequest` send proxy
credentials only in answer to a challenge, and the proxy's 407 carries none (Chrome reports
`ERR_PROXY_AUTH_UNSUPPORTED`); Chromium also drops those in the proxy variables. So once
Docker, QEMU, Firecracker, Daytona, Orchard or Slurm has sealed an `allowlist` or `blocklist`
Sandbox, `relay.py`
has guestd (`guestd/egress.py`) listen on an ephemeral loopback port, add the token to the
request that opens each tunnel, and forward it to the proxy. While the Sandbox is sealed,
the proxy variables of every command, the agent's and a verifier's, name that port, with no
token. On a desktop the agent account's system proxy names it too,
bypassed for the Host's address and loopback: on Windows the default connection's WinINet
settings, which WinINet clients and Chromium-family browsers read; on Linux the GNOME
settings (`org.gnome.system.proxy`), which Chromium-family browsers and Firefox read in a
GNOME session, written with `gsettings` in that session; on macOS the web and secure web
proxies and bypass domains of every enabled network service, written as root with
`networksetup`, which Safari, Chrome and other CFNetwork clients read, and which are the
whole system's rather than one account's. A Linux desktop without GNOME's settings keeps the
variables alone. Programs already running follow the change on Windows and Linux.

`open_egress`, a power-off and a snapshot stop the relay and give the account its own
settings back: open, its programs go straight out again, and a snapshot outlives the relay.
Only settings that still name the relay go back, so what the agent set itself stays for
verification. A macOS proxy that had no server is turned off again with the relay's address
left in it, unused. A failure is logged, not raised: the agent may have broken its own
desktop. guestd keeps what it changed in `/run/ale/system-proxy.json`,
`%ProgramData%\ALE\system-proxy.json` on Windows or `/var/db/ale/system-proxy.json` on
macOS, because a guestd connected over `docker exec` or Orchard's exec ends with its run,
sealed or not. A run that reattaches to a sealed Sandbox starts the relay again with its own
proxy and token, and so does sealing a sealed Sandbox, as a shared verifier sealed after the
agent is: QEMU and Firecracker then keep the runner rules they have rather than insert them
twice, which one opening would not lift.

Measured with Chrome 157 on Windows base 0.1.3, where a browser started before the seal
switches as well, with Chrome 157 and Firefox 160 on the container base's GNOME 42, started
from the desktop or from an agent's command, and with Safari on Cirrus Labs' macOS 26 base:
each reaches the declared host and is refused the rest, its own background hosts included.
Windows' `curl.exe` honors the variables, but schannel fails its certificate revocation
check, which goes over plain HTTP the proxy does not carry, unless it runs with
`--ssl-revoke-best-effort`.

## Snapshots

| Provider | How a snapshot is saved, booted and destroyed |
|---|---|
| Docker | `docker stop`, then `docker commit` to `ale-snapshot:<sandbox id>`. The image keeps the container's command and labels, with `ale.retention=snapshot`, `ale.prepared` naming the prepared image, and no GPU label. A container boots from its image ID; `docker rmi` destroys it and fails while a container still uses it |
| QEMU | The guest shuts down: Windows with `shutdown.exe /s`, Linux with `sync` and then `systemctl poweroff`, since its own power-off leaves files written just before it empty. The overlay itself becomes `qemu-snapshots/<sandbox id>/disk.qcow2` in the ALE cache, a [layer](#disk-layers) on the disk it booted from, beside `snapshot.json` with its episode, role, OS and prepared image; nothing is copied. When a guest booted from a snapshot is saved as one, its overlay first takes in that snapshot's clusters (`qemu-img rebase` onto the snapshot's parent, which reads only what the snapshot holds), so a snapshot never sits on another one. A VM boots from it through a fresh overlay, once the disk below it is the one it was taken on. It needs every disk below it, so snapshots are destroyed before `vm-builds` is cleared. Destroying it fails while a runner mounts it |
| Firecracker | The Provider has the guest's init (PID 1) stop every process and unmount the writable disk, so the disk needs no journal replay, and waits for the runner to exit. The writable disk then becomes one more read-only layer: `firecracker-snapshots/<sandbox id>/` in the ALE cache holds the root disk, any layers under it and a sparse copy of the new one, hard-linked where the filesystem allows, so it outlives the disk cache without copying it, beside `snapshot.json` with its episode, role, OS, prepared image and digest. A microVM boots from it with the layers stacked under a fresh writable disk, eight at most, since Firecracker's devices are few. Destroying it fails while a runner mounts it |
| Daytona | None: snapshots count against an organization quota shared with other tools, so its profile saves none |
| Slurm | `podman stop`, then `podman commit` to `localhost/ale-snapshot:<step>` in the allocation's own store on its node, which keeps the snapshot while a Sandbox its episode boots from it is to come; a Sandbox booted from it there starts in a step of that allocation, from the node's store, so nothing is copied. One that does not fit beside what runs there gets an allocation of its own, which loads the snapshot written out with `podman save` to the shared `~/.cache/ale/slurm/snapshots`; a snapshot the Run keeps is written there as it is saved, since its allocation ends with the episode. Destroying it removes the image, then the file, and fails while a Sandbox still runs on it ([Slurm](#slurm)) |
| Orchard | The guest shuts down from inside, as for a framed or `build.sh` image; `tart clone` then keeps the stopped VM's disk as the local image `ale-snapshot-<id>`, which copies no data on APFS. A VM boots from its clone at the desktop's resolution, made on first use as for a framed image. Destroying it deletes those clones and then the image, and fails while a VM booted from it remains, since driving that VM again after a crash reads the snapshot's record |
| Fleet | The Node's Provider saves it on that Node, as in its row, and Sandboxes that boot from it are placed there, on the leaf that saved it ([Placement](../../../../../../docs/specs/fleet.md#placement)). The Fleet's handle for it is `fleet:snapshot:<sandbox>`; destroying it has the Node's Provider destroy it at the Node's next heartbeat |

How each counts what a snapshot holds against the `storage_mb` of a Sandbox booted from it
([Resources](../../../../../../docs/specs/task-authoring.md#resources-and-network)):

| Provider | What the snapshot holds, and how it counts |
|---|---|
| Docker | A commit labels the image `ale.written` with the bytes written over the Task's image, the container's `SizeRw` measured once it has stopped plus what the snapshot it booted from held. A container booted from it carries that label, and the [storage watch](#docker-writable-storage) adds it to every measurement. Each commit stacks one more layer: Docker's layer store chains 125 at most under any graph driver, so a Sandbox whose image's layers and planned saves would exceed that is refused before it starts; the containerd image store's snapshotter, which replaces that store, stacks far more |
| QEMU | The overlay is the larger of the snapshot's virtual size and `storage_mb` plus its overhead, and the guest's root filesystem already holds what the solver wrote |
| Firecracker | `snapshot.json` records `written_mb`, the writable disk's use as the guest's `df /` showed it before shutdown plus what the snapshot it booted from held; a microVM booted from it gets a writable disk of `storage_mb` less that, plus the overhead, and one whose `storage_mb` that already fills is refused before it boots, as the Task's error. Each save stacks one more layer, of eight at most, so the profile saves eight in a row (`max_saves`), and a Sandbox whose snapshot's layers and [planned saves](../../../../../../docs/specs/sandbox-image.md#provider-selection) together would exceed eight is refused before it boots |
| Orchard | The clone is the snapshot's disk, at least `storage_mb`, with the solver's files on it |

## Volumes

A Provider whose profiles say `volumes` mounts each
[volume](../../../../../../docs/specs/task-authoring.md#volumes) of a Sandbox as a
filesystem of its own, which its snapshot keeps, and gives a separate verifier on the same
leaf a view of it; Daytona and Orchard mount none, so a separate verifier there gets a copy
of every artifact, as of any other.

| Provider | A volume | What a snapshot keeps of it | A separate verifier's view | `storage_mb` |
|---|---|---|---|---|
| Docker | a named volume of its own, which Docker fills from the image | its top layer, in a stack of layers | an overlay volume of the stack under a layer of its own | counted by a helper that measures the volumes ([below](#docker-volumes)) |
| Slurm | a Podman volume in the allocation's store, which Podman fills from the image | its top layer, in a stack of layers in the store | an overlay of the stack under a layer of its own, in the solver's allocation ([below](#slurm-volumes)) | not held |
| Firecracker | an overlay of the image's directory and of each layer's, under a layer on the writable disk | the writable disk, with the root | the snapshot's disks attached read-only, and the same overlay under a layer of its own | counted with the writable disk |
| QEMU, Linux | an overlay of the disk's own directory under `/opt/ale/volumes/<hash>` on the disk | the disk | the snapshot's disk attached read-only once the guest is up, and an overlay of what it holds there under a layer of its own | counted with the disk |
| QEMU, Windows | the directory on the disk, which stays one | the disk | a junction to it on a copy-on-write overlay of the snapshot's disk, attached once the guest is up | counted with the disk |
| Fleet | as on the Node's leaf | as on the Node's leaf | on the Node and leaf that keep the snapshot ([Placement](../../../../../../docs/specs/fleet.md#placement)) | as on the Node's leaf |

### Docker volumes

On Docker ([`docker/volumes.py`](docker/volumes.py)):

| | What the Docker leaf does |
|---|---|
| A Sandbox of the image | finds which of its volume candidates are directories in the image, with no link on their way, by the image's own framework Python in a helper container of it, and mounts a named volume of its own at each, `ale-v-<12 hex>`, labelled `ale.volume=<sandbox id>`, which Docker fills with what the image holds there on the first mount |
| Its snapshot | records each path's stack of layers, topmost first, in the image's `ale.volumes` label with the owner and mode of the volume's root, read before the container stops; the volume that held the Sandbox's writes is the top layer, which destroying the Sandbox leaves |
| A Sandbox the snapshot serves | mounts, for each path, a view: a `local`-driver overlay volume of the stack under an empty upper and a work volume of its own, created by the daemon, with no copy of the image's content (`volume-nocopy`), so a verifier's own image adds nothing; it gives the view's root the recorded owner and mode, which an overlay takes from its upper layer; saving it puts that upper layer on top of the stack |
| A stack of 60 layers | is copied into one layer of its own as the 60th save is taken, with `cp -a --reflink=auto` in a helper container, since an overlay's options, the layers' mount points among them, must fit in one page: a solver saved for each of many rounds keeps saving |
| A container engine's storage | of a Sandbox that asks for `nested_containers`, `/var/lib/docker`, `/var/lib/containerd` and `/var/lib/containers`, is a volume of its own too, which its snapshot keeps whole; a Sandbox booted from the snapshot that runs an engine gets a copy of it, as above, never a view, and one of another image, a separate verifier's, none |
| `storage_mb` | counts what the Sandbox writes to its volumes as to its writable layer: a helper container of its own, `ale-meter-<sandbox id>`, mounts the volumes that hold its writes read-only and counts their blocks with `du`, less what they held as it started, at each measurement of the [storage watch](#docker-writable-storage); a snapshot's `ale.written` adds what they held. A measurement took 0.06 s with nothing written and 0.33 s with 100,000 files of 1 KiB, where the daemon's of the layer took 0.04 s and 0.54 s (Docker 29.1.3, NVMe) |
| Destroying the snapshot | removes the image, then each of its layers that no other snapshot's label and no view's `ale.volume-lowers` label names: a later round's snapshot still stacks an earlier one's layers |

Kata shares a view with its microVM through virtio-fs and gVisor through its gofer, as any
volume. Measured on runc and Kata (Docker 29.1.3, containerd image store) and gVisor 0.1.4
(Docker 26.1.5): an agent that works in its home, over two validation rounds, with a
separate, snapshot or shared verifier, or a shared one with a separate verifier of
`validation/`, each view seeing none of what an earlier verifier wrote and, under separate
verification, none of the image's link, FIFO or excluded directory; a Sandbox writing
300 MiB to its volume past a `storage_mb` of 256, stopped as `storage_exhausted`; and a
gVisor Sandbox's Docker engine whose image, which deletes a file of its base, is still
there and still deletes it once booted from its snapshot, where it deletes another.

### Slurm volumes

A Sandbox's volumes are layers in its allocation's Podman store on the node
([`slurm/worker.py`](slurm/worker.py)): a Podman volume of its own for each candidate the
Host's Docker finds a directory in the framed image, and for each engine path of a Sandbox
that asks for `nested_containers`; a view, an overlay that the step mounts in the
allocation's rootless Podman namespace with `userxattr`, of a snapshot's stack under an
empty layer of its own, which gVisor's gofer serves as any directory; or a copy (`cp -a
--reflink=auto`) of an engine's storage. Every layer has a short link below the store, by
which an overlay's options name the 200 layers of [`MAX_SAVES`](#slurm) in one page. A
snapshot records each path's stack, and a step unmounts its views before the Host hears of
its snapshot, as a mounted upper layer is no other overlay's lower. A snapshot written out
for an allocation of its own carries each of its layers beside its image as an uncompressed
tar that keeps owners, whiteouts and the overlay's extended attributes, which that
allocation unpacks before it starts anything. The allocation is sized for the separate
verifier too where a partition holds both, and loads that verifier's image, from the Host,
when it starts; destroying a snapshot removes its layers that no other snapshot and no
running view stacks. Measured on OpenRSI (Linux 6.8, Podman, gVisor 0.1.4): a Podman volume
filled from the image, a view of a stack of two layers, a deletion through it kept as a
whiteout that the next view honours, and a container engine's images and deletions kept by a
snapshot written out and loaded in another allocation.

## Reattaching by handle

A Sandbox's `handle` names it to `reattach` and `discard`, by which another process on the
same machine, such as a restarted Fleet node agent, drives it again or destroys it.

| Provider | How `reattach` and `discard` find a Sandbox by its handle |
|---|---|
| Docker | `docker:<container>`: `docker inspect` finds the container, whose labels must name it ALE's, and it must be running the recorded image. Reattaching confines it by the network mode its `ale.network` label records, which it was started with, whatever the reattaching run asks, re-applies the [Gateway route](#gateway-address) as a reattaching run does and reads the seal back from the container: a runc one is sealed when it is off the default bridge, a Kata one when the seal's drop filter is on `tap0_kata`, a gVisor one when it is on `eth0`'s egress, whose netstack chains are then written again to match. `discard` removes the container and its network; one that is already gone needs nothing |
| QEMU | `qemu:<runner>`: the same for the runner container, whose labels must name it ALE's and whose overlay must sit on the recorded disk. Reattaching refills the runner's `ALE_GATEWAY` chain and reads the seal back from its `FORWARD` drop rule. `discard` removes the runner and its overlay |
| Orchard | `orchard:<name>:<uid>`: the VM by its name, which must still have that UID and run the image that framing, a snapshot, or a kept VM's record names on this Mac. Reattaching opens new guest sessions; the Gateway relay started at boot outlives the old ones. `discard` deletes the VM unless Orchard replaced it |
| Daytona, Firecracker, Fleet | None: their Sandboxes have no handle, and `reattach` refuses |

## GPU models

A Task's `gpu_types` decides which GPU models a Sandbox may get
([GPUs](../../../../../../docs/specs/task-authoring.md#gpus)). Every Provider checks the
models it got with `ale.core.capability.accepts_model`, which applies that rule.

| Provider | How it gives only the models the Task accepts |
|---|---|
| Docker | Reads the Host's GPUs with `nvidia-smi` and leases only those among `sandbox.docker.gpus`, or among all when it is unset, of an accepted model, whole or by memory ([GPU memory](#gpu-memory)). Too few refuses the Sandbox before it starts, and the container must then show exactly the leased UUIDs. `docker:kata` attaches none: `--gpus` does not reach the microVM; `docker:gvisor` attaches them as runc does ([gVisor](#docker-on-gvisor)). The allocation records each GPU as the Host's `nvidia-smi` reports it |
| QEMU | A device bound to `vfio-pci` has no Host driver to name its model, so the guest's `nvidia-smi` is read once it boots; a model the Task does not accept destroys the Sandbox before any Task code runs. The pool is leased without regard to model, so a pool of several models can refuse a Task while an accepted GPU is free |
| Daytona | Maps the names to Daytona's GPU types as Harbor does: `h100` and `nvidia-h100-80gb` to `H100`, `h200` and `nvidia-h200-141gb` to `H200`, `rtx-pro-6000` and `nvidia-rtx-pro-6000` to `RTX-PRO-6000`, `rtx-4090` and `rtx-5090` to themselves, ignoring case. It asks for them in the Task's order, which Daytona tries in turn, and leaves out names with no type; a Task left with none is refused before anything is created. The models attached are then checked against the Task's own names, as on every Provider. `MI355X` is never asked for, as `nvidia-smi` cannot see it to record it. A Task naming no model sends no type, so Daytona chooses |
| Firecracker | Attaches none: Firecracker passes no GPU through |
| Orchard | Attaches none |

## GPU memory

A Task's `gpu_memory_mb` is what its Sandbox needs on each GPU
([GPUs](../../../../../../docs/specs/task-authoring.md#gpus)), each process's CUDA context
included: a context took 516 MiB with the bare driver API and 608 MiB under PyTorch 2.7 on
an H100 with driver 570, so a Task of two PyTorch processes needs 1216 MiB before its first
tensor. `docker:runc` and `docker:gvisor` share a GPU among Sandboxes that name it; QEMU's
VFIO pool and Daytona give whole GPUs, and know no model's memory before they attach it (a
GPU bound to `vfio-pci` has no Host driver to report it, and Daytona's GPU types state
none), so they refuse no such Sandbox for its memory. A Docker profile gives, as each
model's memory, the most it admits a shared Sandbox with on an empty GPU of it:
`memory.total` less `memory.reserved` less the margin (76993 MiB of an H100 with driver 570
and the default margin), so that a Sandbox that needs more on every model it accepts goes to
the next listed Provider rather than to a leaf that could never admit it.

**Admission.** A Docker leaf admits a Sandbox to the Host's GPUs under one lock for its
Docker daemon, `/tmp/ale-gpu-<daemon ID>.lock` (`docker info --format {{.ID}}`), which every
ALE process of every user that admits through that daemon takes, whatever its cache, from
reading the GPUs until `docker run` returns. It is opened read-only, which `flock` needs no
more than: `fs.protected_regular` refuses `O_CREAT` on another user's file in the sticky
`/tmp`, so the first process creates it with `O_EXCL`, readable by all whatever its umask,
and the others open it as it is. The running containers' labels are the record of what ALE
holds: every Sandbox carries `ale.gpus`, the GPUs it holds, and `ale.gpu_memory_mb`, what it
reserved on each when it shares them, both empty when unused, so an image's labels never
stand in for them. The record is per daemon: Runs on one Host through different daemons,
such as each user's rootless one, see what each other's Sandboxes use, through nvidia-smi,
but not what they reserved and do not use yet. Under the lock it reads `nvidia-smi
--query-gpu=...,memory.total,memory.reserved,memory.free`, the labels of the running ALE
containers (`docker ps`), and, when one shares a GPU, what each process uses (`nvidia-smi
--query-compute-apps=pid,used_memory,gpu_uuid`), which it assigns to a container by the
process's cgroup (`docker-<id>.scope` with the systemd driver, rootful or rootless;
`/docker/<id>` with cgroupfs). nvidia-smi lists every user's processes, by their PIDs in the
Host's namespace: under runc one row for each CUDA process, under gVisor one for the whole
Sandbox, its Sentry, whose memory is nvproxy's count (measured).

| Sandbox | May take a GPU | Of those, takes |
|---|---|---|
| without `gpu_memory_mb` | that no ALE Sandbox holds | the one with the most free memory |
| with `gpu_memory_mb` | that no ALE Sandbox holds whole, and whose headroom is at least `gpu_memory_mb` plus `sandbox.docker.gpu_memory_margin_mb` (4096 by default) | the one with the most headroom |

Headroom is `memory.free` less, for each ALE Sandbox sharing the GPU, what it reserved and
does not use yet; what it uses already is not free, so it counts once, and one that uses
more than it reserved adds nothing. `memory.free` excludes what the driver reserves (470 MiB
of an H100's 81559 with driver 570). The margin is for other tenants' jobs, which grow after
they start: one on a shared Host of eight H100s grew by 4.2 GB within 10 s. It cannot cover
a tenant that starts after admission, as jobs there did, in steps of 19 to 31 GB. Too little
refuses the Sandbox with `ProviderCapacityError`, naming each GPU and its free memory, what
ALE reserved there and the headroom left; a Fleet Node answers with it that it has no room
for now, and the Fleet asks it again later. The allocation records how the Sandbox holds its
GPUs (`whole`, `limit` or `watched`), what it reserved on each, and each GPU's
`memory.total`, `memory.reserved` and `memory.free` when it was admitted.

**Enforcement.** Under `docker:gvisor` a shared Sandbox gets the annotation
`dev.gvisor.flag.nvproxy-gpu-memory-limit=<gpu_memory_mb in bytes>`: an allocation past it
fails as CUDA out of memory (`cudaErrorMemoryAllocation`, `torch.OutOfMemoryError`), a
context that does not fit fails to be created, and the Sandbox goes on
([runtime](../../../../../../images/runtimes/gvisor/README.md#gpu-memory-limit)). Its
preflight, whenever a Sandbox may share GPUs, checks that `runsc flags` lists
`-nvproxy-gpu-memory-limit`, which only ALE's build has. Under `docker:runc` nothing holds a
process to a device memory limit, so every selection (`ale run`, `ale calibrate`, a
reverification) says once, for the leaf and how many Sandboxes it serves so: `note
docker:runc watches the gpu_memory_mb of N Sandboxes rather than enforcing it: a process
past its share is killed`.

**Watchdog.** Each Docker leaf of a process, the Run's or a Node agent's, watches its shared
Sandboxes while that process holds them, from `create` or reattaching until it destroys,
keeps or snapshots them: a kept Sandbox is watched again only when a later Run reattaches to
it. Every 5 s (NVML has no event for memory use) it reads what each process uses and
compares the sum of a Sandbox's rows on each GPU with its share, which holds every CUDA
context; acting on several Sandboxes, it acts on all at once.

Under runc a Sandbox past its share loses its largest process on that GPU, and only that
one, as the OOM killer kills a process past a memory limit: the episode goes on, and the
agent sees its process killed (signal 9, exit code -9 through `exec`). What the Sandbox did,
the Sandbox's own process pays for; nothing turns it into an environment error that would
rerun or unscore the episode. The kill runs in a helper container (the runner image under
`runc`) that joins the Sandbox's PID namespace (`--pid container:<id>`), since under rootful
Docker ALE's user cannot signal a container's processes and nothing the Sandbox holds, its
Python included, may decide whether the kill happens. The watchdog first checks that the
PID's cgroup still names the container and its start time (`/proc/<pid>/stat` field 22) is
the one it read; the helper checks the start time again through that namespace's `/proc` and
then kills. The Sandbox stays watched, and is killed again if it goes past its share again.
The kill is logged to the episode's `trace.execution.jsonl` as a warning naming the process,
what it held, the GPU, what the Sandbox used and its share, in the phase of the command
the Sandbox ran when the watchdog found it, by a Fleet Node's leaf as by a Run's. Until
the watchdog acts, up to 5 s later, a runc Sandbox past its share holds memory others on the
GPU were admitted with.

Under gVisor nvproxy should have refused the memory, so a Sentry's row past its share is a
failure of nvproxy's accounting for memory the driver attributes to the Sentry, which is
ALE's failure: `docker kill` stops the whole Sandbox, its episode ends in an environment
error (`the sandbox itself died: ALE's GPU memory watchdog stopped it: ...`), and it is no
longer watched.

What blinds the watchdog: `nvidia-smi` failing, which it logs once per streak of failed
looks and once when it looks again; a row whose memory reads `[N/A]`; a Host `/proc` mounted
with `hidepid`, which hides other users' processes, a rootful Docker's containers among
them; and ALE running in a PID namespace of its own, as in a container: nvidia-smi gives
each process's PID in the caller's namespace, where the Sandboxes' processes have none.

Not counted by gVisor's limit, nor in nvidia-smi's rows: the memory the driver allocates for
itself, 6 MiB per CUDA context on an H100 with driver 570, which makes the rows of a GPU sum
9 to 15 MiB below its `memory.used`; and managed memory (`cudaMallocManaged`), which the
driver moves to the GPU on use and evicts when it is full: 3 GiB of it raised the GPU's
`memory.used` and no process's row (measured), so the watchdog does not see it either, while
admission sees it as memory no longer free. A Sandbox sees the whole GPU in `nvidia-smi` and
`cudaMemGetInfo`, what other tenants use included, not its share.

## Memory

A Sandbox's memory is its Task's `memory_mb`: a container's limit, a VM's size. Its kernel
decides what dies when that, or the memory around it such as the Host's, runs out. A
Sandbox, or guestd in it, lost after an OOM kill in it since it started raises
`MemoryExhaustedError`
([Episode protocol](../../../../../../docs/specs/episode-protocol.md#agent-or-oracle)),
whose message gives what showed the kill.

| Provider | Where the limit is | One process past it | What loses the Sandbox | How ALE tells |
|---|---|---|---|---|
| `docker:runc` | its cgroup on the Host | killed alone | many processes: the OOM killer reaches supervisord; `/dev/shm`, half its memory, is too small to | Docker's `OOMKilled`, which it sets at any OOM kill in the container and keeps after it stops ([Docker](docker/AGENTS.md#a-sandbox-that-runs-out-of-its-memory)) |
| `docker:gvisor` | its cgroup on the Host, the Sentry's memory included, which the Sentry keeps 16 MiB below | killed alone, by the Sentry | a mounted tmpfs written full: the Sentry ends every process it may choose, guestd among them; many processes are killed one by one | `oom_kill` in the Sentry's `memory.oom_control`, which a watcher in the sandbox writes to a `docker exec` session, since Docker records none of the Sentry's kills; and Docker's `OOMKilled` as under runc, for the Host's OOM killer, which still acts on memory the Sentry does not see ([gVisor](../../../../../../images/runtimes/gvisor/README.md#out-of-memory)) |
| `docker:kata` | the microVM's memory | killed alone | a mounted tmpfs written full; many processes are killed one by one | `oom_kill` or `oom` in the container's `memory.events` in the guest, which a watcher there writes to a `docker exec` session: Docker records no OOM kill in the guest |
| `firecracker` | the microVM's memory | killed alone | a tmpfs written full; many processes are killed one by one, guestd at times among them | the kernel's OOM-killer lines on the guest's serial console since it booted, the runner's log ([QEMU](qemu/AGENTS.md#a-vm-that-runs-out-of-its-memory)) |
| `qemu`, Linux | the VM's memory | killed alone | a tmpfs written full; many processes are killed one by one | the same |
| `qemu`, Windows | the VM's memory and its page file | its allocation fails | nothing measured | none: Windows has no OOM killer, and a lost guestd is an environment error |
| `daytona` | the sandbox's cgroup, inside it | killed alone | many processes, or a mounted tmpfs written full; Daytona then destroys the sandbox itself | `oom_kill` or `oom` in its `memory.events`, which a watcher writes to an SSH session of its own ([Daytona](daytona/AGENTS.md#a-sandbox-that-runs-out-of-its-memory)) |
| `orchard` | the VM's memory, and the swap macOS keeps on its disk | killed by macOS once swap has nearly filled the disk | processes past memory and free disk freeze the guest | none: the frozen guest answers nothing, and a loss is an environment error ([Orchard](orchard/AGENTS.md#a-macos-vm-that-runs-out-of-its-memory)) |
| `fleet` | its Node's leaf | as on that leaf | as on that leaf | the Node's leaf; the error crosses to the Run by name |

The message says whose memory ran out where its evidence does: a cgroup's `oom_kill` counts
every process any OOM killer ended, the Host's too, and `oom` each time its own limit was
reached; the Sentry kills only for the sandbox's own limit; a console line says `Memory
cgroup out of memory` for a cgroup's limit in the VM and `Out of memory` for the VM's.
Docker's `OOMKilled` does not say. A hypervisor the Host's OOM killer ends leaves no record
in its guest, so a Kata, QEMU or Firecracker Sandbox lost so is a lost guest. guestd's
systemd unit in a Linux VM sets `OOMPolicy=continue`, so that one of its commands the OOM
killer ends goes alone.

A Linux Sandbox's `/dev/shm` holds half its `memory_mb` ([`memory.shm_mb`](memory.py)), as a
Linux kernel sizes a tmpfs by default and so every Linux VM's. Docker and Podman give 64 MiB
whatever the memory, in which PyTorch's DataLoader with 8 workers of 64 images each ended at
its first batch (`DataLoader worker (pid(s) ...) exited unexpectedly`, a bus error in the
worker) under runc and gVisor. Its files are the Sandbox's memory: with it full, a write
fails as on a full disk, and a process past what is left is killed alone.

| Provider | `/dev/shm` |
|---|---|
| `docker:runc` | `--shm-size`, a tmpfs charged to the container's cgroup |
| `docker:gvisor` | `--shm-size`, which runsc mounts as a tmpfs of the Sentry |
| `docker:kata` | half the microVM, its guest's own: Kata ignores `--shm-size` ([Docker](docker/AGENTS.md#the-size-of-devshm)) |
| `slurm` | Podman's `--shm-size`, a tmpfs on the node that runsc reaches through its gofer, charged to the step's cgroup ([Slurm](slurm/AGENTS.md)) |
| `firecracker`, `qemu`, Linux | half the guest's memory, its kernel's default |
| `daytona` | a tmpfs that root mounts over Daytona's 64 MiB one at start, charged to the sandbox's cgroup ([Daytona](daytona/AGENTS.md#devshm-is-64-mib)) |
| `qemu`, Windows; `orchard` | none: Windows and macOS have no `/dev/shm` |
| `fleet` | as on its Node's leaf |

No Sandbox swaps to the Host. Every Docker leaf starts a Sandbox with `--memory-swap` equal
to `--memory`, under every runtime. Docker's default, twice `--memory`, let one process of a
512 MiB runc Sandbox hold 1008 MiB on a Host with 8 GiB of swap, and 504 MiB in another run
there, against 504 MiB without swap; one of a 512 MiB gVisor Sandbox held 1112 MiB. Under
Kata the flag only sets the guest container's `memory.swap.max` to 0: the microVM has no swap
and its size is unchanged, and Kata's cgroup on the Host has no limit either way. The Linux
VMs have no swap: the VM base, built from `ubuntu:24.04`, and Firecracker's microVM, built
from the Task's image, have neither a swap file nor a swap partition. Windows keeps its page
file and macOS its swap on the guest's own disk, part of what the VM holds.

## Container features

A Task's `container_features` name the kernel features its container needs
([Container features](../../../../../../docs/specs/task-authoring.md#container-features)).
Each container profile lists those its Provider gives. A Provider adds a grant, such as a
flag, only to a Sandbox that names the feature; what it gives without one, every Sandbox it
starts has anyway. QEMU and Orchard start VMs, which take none.

[`capprobe.py`](../capprobe.py) shows them: run as root on the framework's Python, it makes,
for each feature named, the syscalls that need it (`perf_event_open`; a BPF array map;
`io_uring_setup`; opening `/dev/fuse`; a raw ICMP socket; `unshare` of a user namespace; a
tmpfs mount and `unshare` of a mount namespace) and reports each one's errno. Starting a
container is no syscall, so `nested_containers` is shown by the conformance suite, which
installs Docker in the Sandbox, starts its daemon and runs containers that reach the
Internet, publish a port, resolve each other by name and delete a file of their image; the
Docker suite also starts a Docker preinstalled in an image with `service docker start`.
Before any episode, the Run's preflight probes the features a leaf's selected Tasks need in
a throwaway Sandbox of that leaf for each set they ask for; conformance asks for every
feature a profile declares, Kata's suite shows a Sandbox that did not ask for `mount` lacks
it, and gVisor's that one that did not ask for `mount` or `raw_sockets` lacks them and one
that did not ask for `nested_containers` runs under `runsc` without the engines' volumes. A
scheduler's are probed only by its conformance suite: a Run does not reach Daytona's runners
or a Node's leaves, and a Node declares its leaves' features without probing them. Measured
on a 112-core x86-64 Linux Host (kernel 7.0, Docker 29.1.3) and on Daytona's CPU runners;
Daytona's GPU runners were not probed:

| Provider | Gives | How | Lacks, as probed |
|---|---|---|---|
| `docker:runc` | `raw_sockets` | Docker's default capabilities hold `NET_RAW`, so every container has it and none gets a flag | `perf`, `bpf`, `io_uring`, `user_namespaces` and `mount` (`EPERM`, Docker's seccomp filter and capabilities); `fuse` (no `/dev/fuse`) |
| `docker:kata` | `perf`, `bpf`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | The microVM (guest kernel 6.18) runs no seccomp filter, so the first five need no flag; `mount` adds `--cap-add SYS_ADMIN` to the container that asks, without which it fails with `EPERM` | `fuse` (no `/dev/fuse`) |
| `docker:gvisor` | `user_namespaces`, `fuse`, `mount`, `raw_sockets`, `nested_containers` | The Sentry implements user namespaces and its own `/dev/fuse` for every container; `mount` adds `--cap-add SYS_ADMIN`; `raw_sockets` runs the container under `runsc-net-raw` (on `runsc`, `--cap-add NET_RAW` does nothing); `nested_containers` runs it under `runsc-nested`, which gives netstack nf_tables, with `NET_ADMIN`, `SYS_ADMIN` and `SYS_RESOURCE` and its engine's storage on volumes. A Sandbox has `raw_sockets` or `nested_containers`, not both ([gVisor](#docker-on-gvisor)) | `perf` (`ENODEV`), `bpf` (`EPERM`; `EINVAL` with `CAP_SYS_ADMIN`) and `io_uring` (`ENOSYS`), whatever is granted |
| `firecracker` | `perf`, `bpf`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | The guest's root holds every capability of its own kernel (6.1) | `fuse` (no `/dev/fuse`) |
| `daytona` | `nested_containers`, `fuse`, `io_uring`, `raw_sockets`, `user_namespaces`, `mount` | A sandbox runs in a user namespace whose root holds every capability, with `/dev/fuse`; Docker installed in it runs containers. Daytona grants nothing per sandbox | `perf` (`EACCES`) and `bpf` (`EPERM`), from the runner's kernel (6.8) |

## Desktop resolution

Each Provider starts a desktop at the request's `desktop_resolution` and then checks it with
`desktop.require_resolution`, which reads Cua Driver's `get_screen_size`.

| Provider | How the desktop is started at the requested size |
|---|---|
| Docker | `--env VNC_RESOLUTION=WIDTHxHEIGHT`, which replaces the image's own value; the container base's Xvnc starts at that size (`-geometry`). The name is the one VNC desktop images share, but the image's X server need not be VNC: Xvfb starts at it with `-screen 0 ${VNC_RESOLUTION}x24`. A desktop of another size is refused with that advice, since only the image can change it |
| Daytona | The same variable, in the environment the Provider starts the image's command with and runs guestd in |
| Firecracker | The same variable, in the environment the guest's init starts the image's command with. A microVM has no display device, so its desktop is the image's own X server, as in a container |
| QEMU | Every guest gets QEMU's standard VGA (`VGA=std`), whose EDID names the size as the display's preferred mode (`-global VGA.xres=… -global VGA.yres=…`). OVMF sets that mode at boot, Linux's `bochs` driver and GNOME start at it, and Windows' Microsoft Basic Display Adapter keeps the mode the firmware set; it ignores the size virtio-vga, the runner's own default, announces. The display has 64 MB of video memory, since Linux's driver keeps two frames of the screen. OVMF sets no mode above 3200x1800 and falls back to 1280x800, which is why `DesktopResolution` stops there |
| Orchard | Tart keeps a VM's display size in the VM's configuration, which a clone copies and Orchard cannot set, so each resolution boots its own clone of the framed image, `ale-framed-<key>-<WIDTHxHEIGHT>`, made on first use with `tart set --display WIDTHxHEIGHTpt` and deleted when the image is framed again. The size is in points, Tart's default for macOS: a 1024x768 desktop is 2048x1536 pixels, and Cua Driver returns its captures scaled down (1366x1025 for 1024x768). macOS keeps a display narrower than 1024 points at 1024x768, which is why `DesktopResolution` starts there |

## Agent elevation on Linux and macOS

The Linux framework layer installs `sudo` with the image's package manager when the image
lacks it, and grants nothing. Every Linux and macOS Provider calls
`privileges.set_elevation` through the root guestd, then `privileges.verify_identities`,
before any Task code runs: Docker once `docker exec` has started guestd, QEMU and
Firecracker once the booted guest answers, Daytona before it starts the image's own
command, Orchard once guestd answers. A verifier runs no agent, and verification runs as
root, so in a verifier's Sandbox none sets or checks the account's elevation, which stays as
the disk has it; the Linux Providers still prove the framework root there, and QEMU a
Windows framework's Administrator token. The script runs on the framework's Python:

- It replaces the last two lines of `/etc/sudoers`, a marker comment and one rule:
  `<user> ALL=(ALL:ALL) NOPASSWD: ALL` to grant, `<user> ALL=(ALL:ALL) !ALL` to revoke.
  Sudo applies the last rule that matches, so that rule decides whatever the image's own
  rules, drop-ins and groups say, including for processes the image started before it,
  such as Docker's container command and a VM's desktop session. A snapshot carries the
  rule its Sandbox had, and the next start replaces it.
- Revoking also takes the account out of `sudo`, `wheel` and `admin`, the groups that make
  administrators, which other programs trust without asking sudo: on Linux in `/etc/group`
  and `/etc/gshadow`, for polkit and others; on macOS with `dseditgroup`, since Directory
  Services keeps its groups and `admin` members approve system changes with their password.
  macOS's `/etc/sudoers` ends by including `sudoers.d`, so the appended rule comes last
  there too.
- An account that is uid 0 cannot be revoked, so `[agent].user` naming one without
  `[agent].sudo = true` is a `TaskDefinitionError`. On macOS the agent account is never
  root: the image must sign it in to a desktop, so Orchard refuses it either way.

`verify_identities` then checks `sudo -n true` as the agent, so a grant that does not work
(an image with no sudo and no package manager to install it) is refused with that reason,
as is a way up the revocation missed.

## GPU driver capabilities

A Docker GPU sandbox is started with `--gpus` and the argument `"device=<uuid>,<uuid>"`,
double quotes included: Docker reads the value as one CSV record, and the quotes keep the
list of UUIDs in one field (in a shell, `--gpus '"device=<uuid>,<uuid>"'`). It also gets
`NVIDIA_DRIVER_CAPABILITIES=all`. Docker serves the request on one of two NVIDIA paths, and
only one of them reads that variable. Measured with Docker 29.7.2, NVIDIA Container Toolkit
1.20.0 and driver 580.178.04 on an RTX 4090, with NVIDIA OVRTX 0.6 (a Vulkan path tracer) as
the renderer:

| Path | What enters the sandbox |
|---|---|
| CDI (Container Device Interface): Docker 29.7.2 tries it first when the Host has an NVIDIA CDI spec | The selected GPUs' own device nodes plus every library and shared device node the spec lists (`/dev/nvidia-modeset`, the video codec libraries and the Vulkan ICD manifest included), whatever the variable says: `compute,utility` and `all` give the same set |
| NVIDIA runtime hook: older Docker, a Host without a CDI spec, or `--gpus driver=nvidia.runtime-hook,...` | Only the libraries of the requested capabilities. With `compute,utility` there is no `libGLX_nvidia`, `libEGL_nvidia` or `libnvidia-rtcore`, and OVRTX fails at `vkCreateInstance` with `VK_ERROR_INCOMPATIBLE_DRIVER`; with `graphics` added it renders |

A Host whose Docker default runtime is `nvidia-container-runtime` was not measured.

The hook mounts no Vulkan ICD or GLVND EGL vendor file. An image that renders ships them at
the paths the NVIDIA CDI spec uses, `/etc/vulkan/icd.d/nvidia_icd.json` and
`/usr/share/glvnd/egl_vendor.d/10_nvidia.json`. A CDI spec mounts the Host's own manifests at
the Host's paths, which replace these where the paths match, as on the measured Host and on
Daytona; a driver installed from Ubuntu's packages keeps its Vulkan ICD in
`/usr/share/vulkan/icd.d`, and the sandbox then has both. The hook also mounts
no `/usr/share/nvidia/nvoptix.bin`: OVRTX then logs that it cannot load the OptiX denoiser
weights, skips denoising, and its image differs from the CDI path's.

`all` is requested rather than `graphics` alone because the CDI path injects every driver
file whatever is requested, and anything less would leave a sandbox on the hook path short
of what the same Task gets there. On the hook path `display` adds `/dev/nvidia-modeset`,
`video` the codec libraries (NVENC, NVDEC) and `ngx` the DLSS library.

Daytona mounts its Host's whole driver into a GPU sandbox, as the CDI path does, and
ignores `NVIDIA_DRIVER_CAPABILITIES`: `compute,utility`, `compute,utility,graphics` and no
value gave the same files on an H100 with driver 580.178.04. It registers none of them with
the linker, though. Before the Provider runs `ldconfig`, the linker cache holds no NVIDIA
library and `libEGL_nvidia.so.0` and `libGLX_nvidia.so.0`, the names the EGL vendor file
and the Vulkan ICD load, do not exist: neither loads for the agent, and `vkCreateInstance`
fails with `VK_ERROR_INCOMPATIBLE_DRIVER`, as on Docker's hook path without `graphics`.
Afterwards EGL initializes the GPU and Vulkan lists the H100. The other sonames,
`libcuda.so.1` among them, exist either way, which is why CUDA worked without it.

## Docker runtimes

A Docker leaf is Docker under one runtime, `docker:<runtime>`. Each entry of `RUNTIMES` in
`docker/provider.py` holds all a leaf knows of its runtime: its name in the daemon, the
container features it gives and what a Sandbox that asks for each gets (capabilities,
another name the runtime is registered under, or volumes), whether GPUs reach its Sandboxes,
how they are confined, the Host devices it needs, the memory it adds to a Sandbox's, the
`docker run` flags every Sandbox gets, and, for a runtime ALE builds itself, the arguments
it must be registered with, what its version reports, the command that lists the GPU drivers
it supports, what a Sandbox's kernel log shows and its flag that limits a shared Sandbox's
GPU memory. A Sandbox runs under one registered name, so features that each need another
come in profiles of their own. A runtime is added there and, as a provider id a Run may
name, to `ProviderId` in `ale.core.config`; a unit test fails while the two differ. Every
Docker leaf keeps the Provider name `docker` in its handles.

Its preflight, given what the Run's Sandboxes of it need (everything its profiles declare,
for a Node), finds each name they run under among those `docker info` lists, registered
with exactly the arguments the runtime takes when ALE builds it, or, for a containerd shim's
name such as
`io.containerd.kata.v2`, the shim `containerd-shim-kata-v2` on the PATH Docker runs it
from, and checks its devices. It starts every Sandbox with `--runtime` naming the runtime
its features need, `runc` included, and refuses one for which `docker inspect` reports
another, so a Host whose daemon defaults to another runtime never runs a Sandbox there
unnoticed. The allocation records the runtime Docker reports. ALE's own helper
containers, the network helper, the QEMU and Firecracker runners and the VM builders,
always run under `runc`, which Docker always registers.

When the stream of a guestd session that has answered ends unasked, the Provider runs
`docker wait` on the container for up to 30 s, outside the timeout of the call that was
waiting, which returns once it has stopped (under rootless Docker up to 10.5 s after the
session ended), and then reads its state. A Sandbox in which Docker recorded an OOM kill
(`OOMKilled`), or under gVisor or Kata its watcher counted one, stopped or with only guestd
gone, was lost to memory ([Memory](#memory)). Otherwise a stopped container is reported as
the sandbox itself dying: it was killed from outside on exit code 137, and otherwise its
main process exited with its code; one still running lost only the session, which is
reported as a lost guest session, with guestd's stderr. A session that never answered failed
to start, and its stderr is reported at once. ALE ends a session itself before it destroys,
keeps or snapshots the Sandbox, and that end asks nothing of Docker. Nor does the end of a
Sandbox that ALE stopped, by the GPU watchdog ([GPU memory](#gpu-memory)) or the storage
watch ([Docker writable storage](#docker-writable-storage)): it is reported as what ALE
stopped it for, whatever `OOMKilled` or a watcher says.

On every Docker leaf a sandbox runs its image's command before the Provider confines it,
until the network helper's first run returns: 0.35 to 0.45 s after `docker run` did, most of
it the helper's own container (measured with the container base, `block`, on a 112-core
Host under a load of about 35: three runc, four Kata and four gVisor sandboxes). A runc
sandbox's internal network has no route off the Host meanwhile, but the Host's ports answer
it; a Kata or gVisor sandbox is on its own network unsealed, for which the Provider reads
what the seal needs of that network before the sandbox starts and writes its `resolv.conf`
after the seal. Only a Sandbox booted from a snapshot runs code an agent wrote in that
time, and its setup phase has the network anyway.

A rootless daemon's bridges are in a network namespace of rootlesskit's, which it detaches
for them (`--detach-netns`, `dockerd-rootless.sh`'s default) or which is the daemon's own,
and which reaches no port on the Host's loopback (`--disable-host-loopback`): nothing the
Host serves answers at a bridge's address. When `docker info` reports the daemon rootless
and its bridges are in another network namespace than ALE's, the Provider listens there, on
every address, at each port of the Gateway and egress proxy its Sandboxes reach, and relays
each connection to that port on the Host's loopback; a connection the port refuses is
reset. The seal is unchanged: it still admits only those ports of the Host's address. The
Provider finds the namespace as `dockerd-rootless.sh` does: the daemon is the process on
the socket the CLI uses (`SO_PEERCRED`), and the namespace is
`$ROOTLESSKIT_STATE_DIR/netns` in the daemon's mount namespace when rootlesskit detached
it, and the daemon's own otherwise. A Python of its own joins the daemon's user and network
namespaces, makes the listener and hands it back, so the listener is ALE's process's: it
closes when the last Sandbox that reaches its port ends here, or with the process. Where
ALE runs inside that namespace itself, nothing is relayed.

What a relayed connection costs, on a 384-core Host under a load of about 185 (Docker
29.9.3 with rootlesskit 3.3.0 and slirp4netns, three runs each), against a server listening
in the daemon's namespace itself, as a rootful daemon's Host does:

| | runc, direct | runc, relayed | gVisor, direct | gVisor, relayed |
|---|---|---|---|---|
| Round trip of one byte | 19–23 µs | 45–53 µs | 157–174 µs | 154–260 µs |
| Connection opened, one round trip and closed | 0.22–0.33 ms | 0.70–0.74 ms | 0.88–0.93 ms | 1.1–1.5 ms |
| From the Host | 3.5–4.1 GiB/s | 1.3–1.6 GiB/s | 789–821 MiB/s | 731–856 MiB/s |
| To the Host | 4.2–5.0 GiB/s | 1.7–2.3 GiB/s | 1.2–1.8 GiB/s | 1.2–1.5 GiB/s |

Model traffic is a small fraction of either; under gVisor netstack bounds both paths
alike. Each port's listener is made once, in about 19 ms.

Every Kata or gVisor Sandbox, and every runc one whose network is not `open`, takes a bridge
network of its own while it lives. Rather than one of the about 31 networks Docker's default
address pools give out, the Provider gives it a /29 of `10.77.0.0/16` with the Host at its
first address ([`subnets.py`](docker/subnets.py)): one of the block's 8192 that no route in
the daemon's network namespace overlaps, chosen at random. A runc helper on `--network host`
reads those routes once per Provider, in the namespace that holds a rootful daemon's bridges
or a rootless one's; when rootlesskit detaches its namespace (`--detach-netns`), the helper
sees the Host's routes, and the bridges' namespace routes only rootlesskit's `10.0.2.0/24`,
outside the block. Docker refuses a subnet that overlaps another of its networks, and the
Provider then tries another, up to 32, so Runs and test suites that create networks on one
daemon at once need neither a lock nor a setting, and Docker's pools stay for everything
else. Only the IPv4 subnet is given: a network has IPv6 only when the daemon gives it to
every new network. On a 112-core Host, 110 sealed sandboxes (50 runc, 25 Kata, 35 gVisor)
started at once beside 15 other networks, each on its own /29, reached their Gateway and
neither the Internet, another Host port nor a neighbour's listener; a rootless daemon
(Docker 29.9.3, rootlesskit with slirp4netns and `--detach-netns`) whose pools refused a
31st network created 150 at once, each on a /29 of its own (November 2026).

## Docker writable storage

Docker gives a container no writable storage of a fixed size under its containerd image
store, a fresh Docker 29's default, which accepts `--storage-opt size` and ignores it, nor
under rootless Docker, whose `overlay2` refuses it; only the classic `overlay2` driver on
xfs mounted with `pquota`, rootful, enforces one. So a Docker leaf holds a Sandbox that
names `storage_mb` to it as Kubernetes holds a Pod to its `ephemeral-storage`
([`docker/storage.py`](docker/storage.py)): every 5 s while a Run or a Node drives it, the
leaf reads what the Sandbox has written over its image, `SizeRw` of
`docker container inspect --size`, which the daemon counts on the Host, outside the
Sandbox's reach. Once that passes `storage_mb` MiB the leaf kills the container and logs, in
the phase of the command that ran last, `ALE's storage watch stopped the Sandbox: it wrote
4102 MiB over its image, past the 4096 MiB its Task declares`; the Sandbox's next call,
and one that waits on it then, raises `StorageExhaustedError` with the same sizes, which
ends the Episode as `storage_exhausted`
([Episode protocol](../../../../../../docs/specs/episode-protocol.md#agent-or-oracle)). A
Node's leaf measures as a Run's does, and the error crosses to the Run by name. Nothing is
asked of the daemon's setup: Docker 29.1.3 rootful and 29.9.3 rootless, both with the
containerd image store on ext4, counted alike, and so did every runtime:

| Written in the Sandbox | Counted |
|---|---|
| A file, under runc, under gVisor, whose `--overlay2=none` writes to the layer through its gofer, and under Kata, whose root is the layer shared over virtio-fs | its blocks, at the next measurement: 200 MiB written with `dd` were counted 0.2 to 1 s after `dd` began |
| A file deleted | no longer |
| A sparse file | the blocks it holds: 10 GiB made with `truncate` count nothing, 300 MiB made with `fallocate` count 300 MiB |
| Empty files | their directories' blocks: 100,000 count 2.5 MiB |
| A file deleted while a process holds it open | nothing, though it holds the Host's disk until it is closed |
| `/dev/shm` or another tmpfs | nothing: it is memory, which `memory_mb` holds |
| A [volume](#volumes), a volume artifact's or a container engine's storage | its blocks, at the same measurement, by a helper container that mounts the volume read-only, less what it held as the Sandbox started: Docker sizes a volume only in `docker system df -v`, which sizes every volume and layer of the daemon, in 4 to 6 s with 80 volumes, and fails when a container goes meanwhile |

What ALE writes counts as the Sandbox's own, as on a disk of that size: a ready Sandbox of
the container base has written 3.4 MiB, and 16 MiB 30 s later, the framework's Python
caches and the desktop's start, before setup and the Harness's installation add theirs.

A measurement walks the writable layer, so its cost grows with the files in it, and a
Sandbox goes on writing until the next one. Measured in November 2026 on two Hosts: Docker
29.1.3, rootful, on an NVMe disk (Linux 7.0, 112 cores under a load of about 25), and
Docker 29.9.3, rootless, on a busy disk array (Linux 5.15, 384 cores under a load of about
70):

| | Rootful, NVMe | Rootless, disk array |
|---|---|---|
| One measurement of no files, or of one file of 4 GiB | 0.04 s | 0.06 s |
| One measurement of 100,000 files of 1 KiB | 0.5 s, and about 1 s of dockerd's and containerd's CPU | 0.6 s, and 1.1 s of their CPU |
| 51 Sandboxes measured at once, 50 of 10,000 files and one of 100,000 | 0.6 s, and about 5 s of their CPU | not measured |
| One Sandbox writing with `dd` as fast as it can | runc 0.95 GB/s, in bursts of up to 2 GiB a second; gVisor 0.85 GB/s; Kata 0.4 GB/s | runc and gVisor about 0.2 GB/s for 10 s, then 0.05 GB/s |

So a Sandbox may write about 5 GiB past its limit on a fast disk before it ends, and all of
a Host's Sandboxes together no more than its disk takes in 5 s, which the Host keeps free.
What measuring leaves to the agent, such as `df` in the Sandbox showing the Host's disk and
a write past the limit succeeding rather than failing as on a full disk, is
[issue #152](https://github.com/AgentsLastExam/ale/issues/152).

## Docker on Kata Containers

The `docker:kata` leaf starts each of its sandboxes with `--runtime
io.containerd.kata.v2`: each container is then a QEMU microVM with a guest
kernel of its own, so a kernel exploit in the sandbox reaches that kernel and must still
escape QEMU to reach the Host. The image, guestd over `docker exec`, files, uploads, the
desktop and snapshots work as on runc, and the allocation records the runtime Docker
reports. Each row below was measured with Kata 4.3.1
(its Rust runtime, QEMU, guest kernel 6.18.56) under Docker 29.1.3 and containerd 2.2.2.

A Host enables it without touching the daemon: unpack Kata's `kata-static` release into
`/opt/kata`, link `/opt/kata/runtime-rs/bin/containerd-shim-kata-v2` into `/usr/local/bin`,
which is on the daemon's PATH, and load `vhost_vsock`. Docker runs a containerd shim named
in `--runtime` from its PATH, so `daemon.json` and a restart are not needed. Kata's default
configuration is used as it ships.

| Kata | What the Provider does |
|---|---|
| Moves every frame between the VM's tap (`tap0_kata`) and the container's veth with tc filters, at preference 49152, so netfilter in the sandbox's namespace never sees the guest's traffic: the runc helper's rules would neither route the Gateway nor confine the Host | The same helper puts tc filters ahead of Kata's, in that namespace, outside the VM. Port 7412 to the Host's address is rewritten to the Gateway's port, and replies back (`pedit` and `csum`; 64 MiB each way came through intact). A sandbox that is not `open` reaches no other port of that address, and its proxy port only under `allowlist`. Root in the VM writes any frame, so these filters pass only a frame from the sandbox's own MAC and address to the bridge's MAC: one with another source, one to another MAC and an ARP reply claiming another address are dropped (measured). Toward the VM they pass only a frame from the bridge's MAC and the Host's address to the sandbox's MAC, which the guest takes from the veth (measured), and the Host's own ARP. A reattaching run replaces only the Gateway and proxy filters |
| Hands the VM its networks at boot: `docker network connect` succeeds and the VM never sees the network | The sandbox boots on a bridge network of its own, `ale-net-<sandbox id>`, which no other container joins, so frames another container writes as the Host never reach it (measured). The network is not internal, an `open` Task's included: egress opens by removing the seal and leaves through the network's NAT. Sealing adds filters that drop every frame but those to and from the Host's address, ARP included, so it reaches neither the Internet nor another container; while it is open, the Host's other addresses are reached as any other host is. A sandbox that is not `open` starts sealed: the filters are in place before guestd connects, though the image's own command starts before them. Sealing again replaces each filter in place, so a drop is never missing; opening egress removes them and checks that no drop filter is left. Destroying the sandbox removes its network |
| Docker's embedded DNS server, `127.0.0.11` on any network but the default bridge, is served through netfilter in the namespace and out of the guest's reach: a lookup failed | As the sandbox starts, the Provider writes over its `/etc/resolv.conf` the one Docker gives a container on the default bridge, which names the Host's upstream servers, read once by a helper there, so the daemon needs its default bridge; the file stays the container's own and writable |
| Makes the VM exactly the container's memory limit; the guest kernel and agent take about 96 MiB plus 2.5 % of it (512 MiB declared left 395 MiB usable, against 504 MiB on runc) | Starts the VM with `memory_mb + 96 + memory_mb / 40` MiB, which leaves the Task's processes at least what runc gives them; the allocation records `memory_mb`. With the base image's desktop running in 512 MiB, one process then held 312–320 MiB against 240–288 MiB on runc: in a VM the kernel's own work for the desktop is not charged to the container |
| Gives the VM one vCPU more than `--cpus` and applies the quota in the guest's cgroup | Nothing: the quota holds, and `nproc` reports `cpus + 1` rather than the Host's count |
| Cannot take Docker's `--gpus` | Its profile attaches no GPUs |
| Drops what a filter drops: tc cannot answer with a rejection, so a sealed guest's DNS lookup took 112 s to fail, and broke sudo's first use when the guest did not yet list its own host name | A sandbox that can be sealed gets `NET_ADMIN`, and the framework removes the guest's default route while it is sealed and restores it when it opens. What runc's internal network refuses then fails at once here too; only a blocked port of the Host's address, which stays on-link, times out. The capability works inside the VM only: root there that re-adds the route makes failures slow again, and still reaches nothing. The framework removes the route through guestd, which holds only the container's own capabilities, so root that replaces the framework's Python gains nothing at its next start |
| Runs no seccomp filter in the guest (`disable_guest_seccomp`) | Nothing: the VM is the boundary, and the filters are outside it |
| Writes Docker's `/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf` into the container's own files, where runc mounts them | A snapshot keeps them; a Sandbox booted from it gets its own, a moment after it starts |
| Shares the container's root filesystem with the guest over virtio-fs | Nothing; see the costs below |
| Leaves `docker exec` running after the microVM's hypervisor was killed from outside, as by the Host's OOM killer, though Docker reports the container exited | Waits on each Kata container and ends guestd's sessions once it stops, so the loss is explained as on runc |

What it costs, measured on the `example-task` and `tool-smoke` Tasks of ale-tasks-base
(the container base with its desktop; 1 CPU, 4096 MB, `block`), five sandboxes of each per
runtime, one at a time, on a 112-core Host whose load stayed between 26 and 38 from other
work. Medians, with the range:

| | runc | Kata |
|---|---|---|
| `create` until ready, Gateway route and seal included | 8.0 s (7.4–10.1) | 10.1 s (8.7–11.4) |
| Host memory of an idle sandbox (its cgroup, QEMU and virtiofsd included) | 459 MiB (458–474) | 1119 MiB (1080–1146) |
| Host memory after writing and reading 1 GiB and reading `/usr` | 627 MiB | 3230 MiB: the guest's page cache is not returned |
| guestd `exec` of `true`, round trip | 1.5 ms (1.2–2.0) | 9.0 ms (6.5–11.5) |
| CPU: Python loop; SHA-256 | 2.53 s; 362 MiB/s | 2.06 s; 341 MiB/s |
| 1 GiB written and `fsync`ed; read back | 1256; 8209 MiB/s | 203; 1724 MiB/s |
| 4 KiB files created; read; deleted, per second | 18802; 77791; 73307 | 3122; 4198; 7079 |
| `/usr` read on first touch | 21186 files/s, 428 MiB/s | 2702 files/s, 55 MiB/s |
| Snapshot (`docker stop`, `docker commit`) | 23.6 s (20.7–24.3) | 27.0 s (25.2–30.1) |

Compute runs at native speed; the root filesystem over virtio-fs, with Kata's default
`virtio_fs_cache = "auto"`, is 5 to 20 times slower than overlayfs, and file-heavy Tasks
pay for it most.

## Docker on gVisor

The `docker:gvisor` leaf starts each of its sandboxes under ALE's build of gVisor's `runsc`
([gVisor runtime](../../../../../../images/runtimes/gvisor/README.md)): the container's
system calls go to the Sentry, a kernel in user space, so an exploit in the sandbox must
escape the Sentry and then its seccomp filter to reach the Host's kernel. The image, guestd
over `docker exec`, files, uploads, the desktop, GPUs and snapshots work as on runc, and the
allocation records the runtime Docker reports. Each row below was measured with ALE's build
of runsc release-20261109.0, with patches 0001 to 0005 and, for the rows on container
engines, 0006 to 0008, which change only nf_tables and its connection tracking, under
Docker 29.1.3 (containerd snapshotter) on Linux 7.0.

Every Host registers ALE's `runsc` in Docker three times, the same on every Host, GPUs or
not: as `runsc`, as `runsc-net-raw`, which adds `--net-raw`, and as `runsc-nested`, which
adds `--TESTONLY-nftables`, each with exactly the arguments the [runtime's
README](../../../../../../images/runtimes/gvisor/README.md#install-on-a-host) shows. A
Sandbox runs under `runsc` unless it asks for `raw_sockets` or `nested_containers`, and then
entirely under `runsc-net-raw` or `runsc-nested`; no profile gives both, so a Sandbox that
asks for both is refused before anything starts. Besides finding each name the Run's
Sandboxes run under, registered with exactly those arguments, the preflight checks that
`runsc --version` reports the build this engine runs (`+ale.0.1.4`, `VERSION` in its
`build.sh`; a stock `runsc` lacks the rootless and out-of-memory fixes) and, whenever GPUs
may be attached, a Sandbox needing them or a Node whose profiles offer the Host's, that
`runsc nvproxy list-supported-drivers` lists the Host's NVIDIA driver, and, whenever a
Sandbox may share GPUs, that `runsc flags` lists the flag that limits its GPU memory. In the
Run's first Sandbox of the leaf, a probe of its container features or one started for this
alone, and in one Sandbox of the container base a Node starts before it lends the leaf, the
kernel log must say `Starting gVisor`, and `docker cp` must see what the Sandbox wrote, as
`--overlay2=none` makes it.

| gVisor | What the Provider does |
|---|---|
| netstack, gVisor's network stack, takes the container's address and routes at boot and sends and receives every frame through a packet socket on `eth0`, below the namespace's netfilter; it never sees a network attached later, and one detached is lost for good; Docker's embedded DNS server, `127.0.0.11` on any network but the default bridge, answers nothing in netstack | The sandbox boots on a bridge network of its own, `ale-net-<sandbox id>`, which no other container joins and which is not internal, an `open` Task's included, so egress opens by removing the seal; destroying the sandbox removes it. As the sandbox starts, the Provider writes over its `/etc/resolv.conf` the one Docker gives a container on the default bridge, which names the Host's upstream servers, so the daemon needs its default bridge |
| tc's ingress hook runs after the packet socket has read a frame, so a filter there drops nothing; the egress hook on `eth0` sees every frame the Sentry sends, from any kind of socket | The network helper puts tc filters on `eth0`'s egress (a `clsact` qdisc), in the sandbox's namespace, outside it. A sandbox that is not `open` passes only frames from its own MAC and address to the bridge's MAC and the Host's address, on the Gateway's own port (the wire carries it, since netstack rewrites 7412) and, under `allowlist`, the proxy's, and starts sealed: its own ARP for that address passes and everything else is dropped, IPv6 included. A packet with a forged source, from a sandbox with raw sockets, is dropped (measured). While sealed it reaches nothing but those two ports; while open, the Host's address on its network still answers only on them, and the Host's other addresses are reached as any other host is. Sealing again replaces each filter in place, so the drop is never missing; opening removes the seal and checks that its drop filter is gone |
| Dropped frames only time out: a sealed DNS lookup took 56 s | ALE's chains in netstack refuse at once what the seal drops (a TCP reset or an ICMP port unreachable; a lookup fails in 0.01 s): `ALE_GATEWAY` sends 7412 to the Gateway's port; `ALE_HOST` refuses every other port of the Host's address, as runc's rules do, the replies to connections the Host opens included; while sealed `ALE_SEAL` refuses everything else the sandbox sends and `ALE_INPUT` drops what it did not ask for. `OUTPUT`, `FORWARD` (for what a sandbox forwards between network namespaces of its own) and `INPUT` jump to them first. Sealing changes the filters and then the chains; opening, the chains and then the filters. An `open` sandbox with no Gateway on this Host gets no chain, and no privileged exec |
| A privileged `docker exec` holds every capability in the Sentry, `CAP_NET_RAW` only under `runsc-net-raw`, so under `runsc` the image's iptables, if it has one, cannot open its raw socket; the sandbox's own processes hold `CAP_NET_ADMIN` only under `runsc-nested` | [`iptables.py`](../iptables.py), run as root on the framework's Python through `docker exec --privileged`, replaces each table in one `IPT_SO_SET_REPLACE` on a UDP socket, which needs only `CAP_NET_ADMIN`, holding `/run/xtables.lock` as iptables does, and keeps every rule the Task made. That Python is the sandbox's own file: root in the sandbox, an agent granted `sudo`, can replace it and so run with every capability at ALE's next change of egress, mount file systems whether its Task named `mount` or not, and rewrite ALE's chains. Only the filters outside bind the sandbox, and they hold: with its chains flushed, a sealed sandbox reaches neither the Internet nor anything with a raw ICMP echo (measured) |
| tc cannot drop frames on their way into the sandbox | No other container is on its network, so only the Host's frames arrive: a container with raw sockets on the default bridge that wrote as the Host got none to its veth (measured). `ALE_INPUT` drops what the sandbox did not ask for while it is sealed |
| `runsc` gives raw sockets only to a whole sandbox, from its registration's `--net-raw`; `--allow-packet-socket-write` would let a packet socket write any frame | A Sandbox that asks for `raw_sockets` runs under `runsc-net-raw`: ping works and a packet socket captures, but its write fails (`ENOTSUP`), and the seal drops what a raw socket forges |
| The Sentry, the gofer and the stubs are charged to the container, the Sentry keeps 16 MiB of it free, and the memory watcher (below) runs beside guestd: beside the framework's processes alone, the desktop ended, one process got 24 to 72 MiB less than under runc for the same Task with 60 MiB and 1 MiB per GiB added, at 512 MiB, 2, 4 and 8 GiB alike (28 runs, rootless Docker 29.9.3 on Linux 6.12); the Sentry's own memory when it kills varies that much from run to run | Starts it with `memory_mb + 108 + memory_mb / 1024` MiB, which puts runc's share in the middle of that spread; the allocation records `memory_mb` |
| The application's memory is the Sentry's memory file, charged to the container: a stub the Host's OOM killer takes at the container's limit frees nothing until the Sentry has torn it down, so the next charge takes another process, at times the Sentry, which ends the whole sandbox; under rootless Docker, Docker's default `oom_score_adj` ties the stubs with the Sentry ([Out of memory](../../../../../../images/runtimes/gvisor/README.md#out-of-memory) has the measurements) | ALE's runtime keeps a sandbox whose cgroup cannot swap, as no Sandbox's can, 16 MiB below its limit and kills there in the Sentry, one process at a time, sparing init and any process at `oom_score_adj` -1000 (patch 0009). `--oom-score-adj 500` ranks every stub above the Sentry and the gofer for the Host's OOM killer, which still acts on memory the Sentry does not see, under rootful and rootless Docker alike |
| The Sentry counts the processes it kills in `oom_kill` of cgroup v1's `memory.oom_control`, which the Host's cgroup and Docker never learn of; what a sandbox writes into a tmpfs, such as `/dev/shm`, is the Sentry's memory, which no kill frees, so filling its limit that way ends every process the Sentry may choose, guestd among them | A watcher on the framework's Python, at `oom_score_adj` -1000, writes the Sentry's `memory.oom_control` to a `docker exec` session, and a lost session after it counted a kill, or after Docker recorded one, reports a loss to memory ([Memory](#memory)) |
| `docker stop` ends a command without a `SIGTERM` handler at once (exit 143), where runc waits out the timeout; `docker cp` into a running sandbox is not reliably seen inside | Snapshots stop and commit as on runc; files go through guestd, never `docker cp` |
| `docker run --gpus` turns nvproxy, gVisor's proxy of the NVIDIA driver, on for that container alone; `--nvproxy` would turn it on, and probe the driver, for every container; `nvidia-smi` in the sandbox reports no PCI address (`[Unknown Error]`) | GPUs are attached as on runc (`--gpus "device=<uuid>,..."`, `NVIDIA_DRIVER_CAPABILITIES=all`, which `--nvproxy-allowed-driver-capabilities=all` allows), and recorded as the Host sees them; a sandbox sees the whole GPU and what its other tenants use, as under runc. A Sandbox that shares its GPUs is limited to its `gpu_memory_mb` on each ([GPU memory](#gpu-memory)) |
| `runsc` ignores the setuid bit of a program, `sudo` among them, unless it runs with `--allow-suid` | Every registration passes `--allow-suid`, so the agent elevates through `sudo` as under runc |
| `perf_event_open`, `bpf` and `io_uring_setup` fail whatever is granted (`ENODEV`, `EPERM` or `EINVAL`, `ENOSYS`) | Its profiles give none of them |
| netstack implements nf_tables only under `--TESTONLY-nftables`, without which the `iptables` of Debian, Ubuntu and Alpine, Docker and Podman fail (`Failed to initialize nft: Protocol not supported`); with it, the Sentry's netlink takes nf_tables messages from any process holding `CAP_NET_ADMIN` in a network namespace, as every process does in one it creates | A Sandbox that asks for `nested_containers` runs under `runsc-nested`, with `--cap-add NET_ADMIN`, `SYS_ADMIN` and `SYS_RESOURCE`, which stay in the Sentry: the engine's bridges and rules, its mounts and namespaces, and an init script's limits, such as Debian's raising open files. No other sandbox reaches nf_tables. netstack runs ALE's chains, which stay in its iptables tables, before the engine's nf_tables rules, as Linux runs both; unlike Linux, which tracks and NATs a connection once for both, each tracks and NATs it on its own (patch 0008) |
| The sandbox's root is the Host's overlayfs, which refuses to create the whiteouts (a 0:0 character device) by which an engine's overlay layers mark what they delete: an engine that keeps its images there fails to start a container (`symlink /proc/mounts …/etc/mtab: file exists`) or to delete a file of its image | `/var/lib/docker`, `/var/lib/containerd` and `/var/lib/containers` each get a Docker volume of their own, which starts with what the image holds there and goes with the sandbox. Its snapshot keeps each whole, and a Sandbox booted from it gets a copy, as an overlay view would hide the engine's whiteouts; the storage watch counts what is written there ([Volumes](#volumes)) |
| A dockerd that can open a packet socket but not write one, as under `--net-raw`, fails to start a container (`failed to advertise addresses: operation not supported`, the `docker.io` 29.1.3 of Ubuntu 22.04 and 24.04); one that cannot open one only warns | `runsc-nested` passes no `--net-raw`, so a nested container has no raw socket, and a Sandbox has `raw_sockets` or `nested_containers`, in a profile each |

Under `runsc-nested`, Ubuntu 22.04's and 24.04's `docker.io` 29.1.3 installed during the run
and started with `dockerd`, Docker CE 29.10.0 preinstalled and started with `service docker
start` or `dockerd`, Debian 12's `docker.io` 20.10.24 started with `service docker start`,
and `docker:29-dind`'s entrypoint each ran containers that reached the Internet, published a
port to the sandbox, resolved each other on a network of their own, built an image and
deleted files of their image, and `docker exec`, `stop` and `start` worked. Ubuntu's
`docker.io` ships no init script, so `service docker start` fails for it under runc too.
Podman 4.9.3 runs containers on the host network only: on a network of its own it fails as
it binds the container's network namespace recursively (`mount --rbind /proc/<pid>/ns/net`,
`EINVAL`, which Linux allows), and past that its bridge plugin sets hairpin mode (`operation
not supported`) and netavark 1.4 makes a netlink request that fails with `EINVAL`.

The filters and chains confine IPv4. The Provider creates a sandbox's network without IPv6;
on a daemon that enables IPv6 on every new network, the Host's IPv6 address is outside the
Host's drop and `ALE_HOST` while egress is open, though the seal drops IPv6 like everything
else.

What it costs, on a 112-core Host under a load of about 40: starting a bare container until
`docker exec` works took a median 0.28 s against runc's 0.33 s, and an idle sandbox's
runtime holds 17 MB of its cgroup. With the container base's desktop (1 CPU, 4096 MB), its
first screenshot came after 7.6 s against 4.3 s and its cgroup held 541 MB idle against
485 MB; `create` took 15 s against 8.5 to 10 s, opening or sealing egress 0.9 to 2.0 s
against 0.07 to 0.13 s, and starting the framework's Python 1.2 to 2.4 s against 0.4 to
0.9 s. A Run with no Task that asks for a container feature starts one Sandbox more, for
the preflight's check: about 15 s with that image.

## Slurm

A Run can submit directly to Slurm over an SSH login alias. The login and compute nodes must
share the submitting account's home. A Sandbox not booted from a snapshot, a separate
verifier that copies included, gets a single-node batch job of its own, its allocation,
sized for it and for the verifiers its request names, which boot from its snapshot or view
its [volumes](#slurm-volumes) after it. Each Sandbox
runs in its allocation as a job step of its own, which the batch step starts with `srun
--exact` and Slurm holds to the Sandbox's CPUs, memory and GPUs; a step that asks for no GPU
sees none of the job's. The allocation ends with the last Sandbox and snapshot in it.

The Host prepares and frames images using Docker. It sends the compressed image once per
content identity to the login user's private `~/.cache/ale/slurm`, and an allocation loads
it once into its own Podman store on the node. Compute nodes need Python 3, rootless Podman
with slirp4netns, iproute2, and cgroup v2 with enforced CPU affinity, a memory limit and
zero swap for each step. GPU partitions need NVIDIA CDI and a driver supported by the pinned
ALE gVisor runtime. The compute node pulls the build `docker:gvisor` runs from GHCR into a
private, versioned cache shared by the login and compute nodes. Tasks run without the login
home, agent socket or Slurm credentials.

```toml
[sandbox]
providers = ["slurm"]

[sandbox.slurm]
walltime_minutes = 180
queue_timeout_sec = 900

[[sandbox.slurm.partitions]]
host = "cluster-login"
name = "cpu_1"
qos = "cpu"
max_cpus = 42
max_memory_mb = 204800

[[sandbox.slurm.partitions]]
host = "cluster-login"
name = "h100_1"
qos = "short"
max_cpus = 166
max_memory_mb = 1593403
gpu_model = { name = "NVIDIA H100 80GB HBM3", memory_mb = 81559 }
max_gpus = 8
```

Each entry describes a homogeneous partition's effective per-node limits for that QoS.
Add entries with another login alias to serve another cluster. CPU-only allocations use CPU
entries; an allocation whose solver or verifier needs GPUs takes the first matching GPU
entry, as whole cards. The provider checks each step's CPU and memory limits, the started
image and the allocated GPU UUIDs against the devices the guest sees. Pending jobs have a
queue deadline. Running jobs have a Slurm wall time even when the Host is lost, and
`walltime_minutes`, within the QoS's limit, must cover every turn, validation round and
verifier of the episode. `sandbox_id` names the login host, job ID and step; the allocation
records the compute node and runtime.

A snapshot is the stopped container committed to the allocation's store on its node
([Snapshots](#snapshots)). The verifier booted from it, a [validation
round](../../../../../../docs/specs/episode-protocol.md#validation-rounds)'s, a separate
verifier that views its volumes and the next turn's solver start there in steps of the same
allocation, so nothing is copied and nothing waits in the queue again. The cost is that the
allocation holds the larger of its solver and verifier for its whole life: a solver of one
GPU for eight hours, verified on eight GPUs for one, holds eight for nine hours, 72
GPU-hours, where two allocations would hold 16. A Sandbox booted from a snapshot that does
not fit beside what runs in its allocation gets an allocation of its own, which loads the
snapshot written out with `podman save`; a snapshot the Run keeps (`sandbox_retention.solver
= "snapshot"`) is written out as it is saved, as its allocation ends with the episode, and
costs that copy. `ale sandbox destroy slurm:<host>/<job name>/<step>` removes one; `ale
sandbox list` lists those of the hosts a Run configures, and finds none without one. A chain
of 200 commits, each booted from the one before, over a one-layer image was measured, so
each profile saves 200 in a row; a container engine's storage is volumes, which a snapshot
keeps as layers beside its commit. Slurm keeps no running Sandbox and limits no writable
storage.

`srun --overlap --unbuffered` opens guestd's command and file sessions and a separate
tunnel. Blocking drops packets on the sandbox's private tap outside gVisor. Opening removes
the filters while the container keeps running. `allowlist` and `blocklist` use the same seal
and the Host's authenticated proxy through the tunnel. The runtime modes grant nested Docker or
raw sockets separately.

A Sandbox's step gets the Task's memory plus what `docker:gvisor` adds for the Sentry
([Docker on gVisor](#docker-on-gvisor)) and 32 MiB for the step's own process and conmon;
the job adds 512 MiB for its batch step, Podman's namespace and every Sandbox's control
steps. GPUs of one node talk to each other through nvproxy as on the bare node: NCCL 2.27.5
all-reduced 1 GiB across four NVLinked H100s at 356 GB/s of bus bandwidth in a Sandbox,
against 359 GB/s in a plain step of the same node (304 against 315 GB/s at 64 MiB). A profile's memory limit is the largest Sandbox that fits its partition with both.
The runtime reads the limit and usage of the nearest cgroup above the step that has a
`memory.max`, which is where Slurm limits it, so memory pressure ends a process inside the
sandbox. Confirm a site with
`ALE_TEST_SLURM_CONFIG=run.toml uv run pytest tests/conformance/test_provider_slurm.py`.

## Daytona

Daytona's sandboxes are built for a different trust model than ALE's, so `daytona/`
works around several of its defaults to hold the same contract. Each row was measured on a
live organization (daemon 0.225.2). Operating it is in
[`docs/guides/daytona.md`](../../../../../../docs/guides/daytona.md).

| Daytona | What the Provider does |
|---|---|
| Builds sandboxes only from registries registered with the organization; push credentials expire hourly | `daytona/image.py` adds one layer (`USER root`, and `iptables` if absent), pushes it by digest to Daytona's own registry, and keeps one organization registry entry, `ale-transient`, holding the current credentials. The sandbox is built `FROM` that digest, so nothing is rebuilt remotely |
| Runs its daemon, and every command sent through it, as the image's final `USER` | That layer's `USER root`, so guestd is root |
| Replaces the image's entrypoint and command with its own daemon | Starts them itself, detached, as the image's `USER` with its `ENV` and working directory, then waits for the desktop as Docker does |
| Its daemon listens inside the sandbox (2280, 22220, 22222, 33333, and any other port PID 1 opens) and runs commands for anyone, as root | `iptables` owner rules reject every non-root user, IPv4 and IPv6; a probe as an agent that may not elevate must fail on every local address, or the sandbox is destroyed. An agent that may elevate, root among them, is not probed: the daemon gives root nothing it lacks ([`daytona/AGENTS.md`](daytona/AGENTS.md#a-root-agent-and-the-daemon)) |
| Accepts no connection from the sandbox outward; its SSH gateway refuses `-R` | guestd runs over two SSH exec sessions, one for commands and one for file reads. A third carries `guestd/tunnel.py`, which delivers the Gateway on the sandbox's loopback at 7412 and, under `allowlist` or `blocklist`, the egress proxy at 7413; the sandbox names an index into the Host's list, never an address |
| Switches `networkBlockAll` and a domain allowlist at runtime (Tier 3 or 4 only), asynchronously | `block`, `allowlist` and `blocklist` all seal with `networkBlockAll`; a list reaches its hosts through the tunneled proxy and the [relay](#egress-relay), as on Docker. Daytona's domain allowlist is not used ([`daytona/AGENTS.md`](daytona/AGENTS.md)). Every switch waits for a TLS handshake with a public host to behave as the setting says |
| Accepts connections under `networkBlockAll` that it passes nothing on, and on a GPU runner names a proxy of its own in every process's environment (see below) | guestd starts without the runner's proxy variables, unless the image or Task sets them. The accepted connections are left as they are: the Provider and the conformance suite count the network as reached only when a TLS handshake with a public host completes |
| Serves GPUs from its own region, in several models, optionally as spot capacity it may reclaim | Asks for those of the Task's models Daytona offers ([GPU models](#gpu-models)) and never spot. The GPUs are read with `nvidia-smi` as the agent and recorded; another count or model, or a GPU the agent cannot open, destroys the sandbox |
| Mounts the Host driver's libraries without registering them with the linker (see [GPU driver capabilities](#gpu-driver-capabilities)) | Runs `ldconfig` as root before it reads the GPUs, as Docker's NVIDIA paths do with their linker-cache hook; a sandbox where it fails is destroyed |
| Rate-limits creates | A create answered 429 made nothing and is retried, honoring `Retry-After`, for up to five minutes. No other failed create is repeated, since it may have made a sandbox |
| Creates a sandbox even when the caller is cancelled mid-request | Every way out of a start deletes what Daytona made; a create whose reply was lost is found by its unique name |
| Gives every sandbox a 64 MiB `/dev/shm`, which the create request takes no size for and root cannot remount ([`daytona/AGENTS.md`](daytona/AGENTS.md#devshm-is-64-mib)) | Mounts a tmpfs of half the sandbox's memory over it as root, before anything else in the sandbox runs ([Memory](#memory)) |
| Organizations are shared between tools | Labels live under `ale.run.*`, and listing and destroying act only on those. Every sandbox has a lifetime (`ttlMinutes`), so one a crashed run leaves behind is deleted by Daytona |

Under `networkBlockAll` every runner accepts TCP connections to ports 80 and 443 of any
address, a GPU runner also to port 53, and passes nothing on them: a TLS handshake ends at
once, an HTTP request gets no answer, and no name resolves. Other ports get no answer.

A GPU runner, as measured on H100 sandboxes, starts every process with `HTTP_PROXY`,
`HTTPS_PROXY` and their lower-case forms set to `http://172.20.0.1:18080`, and `NO_PROXY`
to loopback. That proxy applies the sandbox's
settings too: under `networkBlockAll` it answers every request with 403. Direct connections
otherwise behave as on a CPU runner, so the agent needs neither the proxy nor its
variables.

## QEMU

`QemuProvider.create` frames the disk it is handed with `framework.framed_disk`, the
function Task preparation uses for a qcow2 `ref`, so both share one cache in `vm-builds`.
Measured on a KVM host:

- A disk that already carries this framework, such as one ALE prepared, boots as it is.
  Reading its marker (`/opt/ale/framework`, or `C:\ProgramData\ALE\framework` on Windows)
  with the VM builder's `virt-cat` takes about 3 s, and the answer is recorded next to the
  cache, so each disk pays it once per Host.
- Any other disk is framed offline into a [layer](#disk-layers) on its base
  (`images.base_layer`), once per disk, framework identity and builder image. A disk
  pulled from a registry is that base as it is ([registry disks](#registry-disks)). A local
  disk, which may be compressed and is not ALE's to keep, gets an uncompressed copy once
  per disk and Host: 17.7 GB for the 10.2 GB Windows base 0.1.2, which took about 30 s.
  A compressed disk is decompressed on every read: with the zstd-compressed Windows base
  under a Task layer a guest booted 15% slower and read the base's files 4–5 times slower.
- The resolved image keeps the request's prepared identity and observes the layer that
  boots, which the RunLock records.

A VM boots from a qcow2 overlay on that disk, in `qemu/<sandbox id>/`; the disks, overlays
and snapshots all live in the ALE cache (`ALE_CACHE_DIR`, `~/.cache/ale` by default). The
runner opens them with `cache=none,aio=native`, which bypasses the page cache, and ZFS then
bypasses its own cache as well, so the ALE cache belongs on local SSD. From a ZFS pool of
5400 rpm disks a Linux guest took 115–133 s to come up with its desktop rather than 20 s,
and a Windows guest 250–310 s rather than 52 s.

### Disk layers

A framed disk, a Linux or Windows Task image and a snapshot are each a qcow2 overlay on the
disk below it, never flattened. Beside each, `<name>.json` records the identity of its
parent and the SHA-256 of its own file, taken once when it is made (`images.record_layer`);
the base a chain starts from, a pulled disk or the copy of a local one, records the
identity the disk was resolved under. A layer names its parent by a path relative to
itself, so the chain stays whole when the ALE cache moves. `images.layer_identity` follows
a chain with `qemu-img info --backing-chain` and refuses one in which a layer sits on
another parent than its record names; every prepared disk with a backing file is checked
that way before it boots.

The runner and the VM builder are containers, so every layer of a chain is mounted in them
at its own Host path (`images.chain_mounts`), read-only except the one being written, and
the relative names resolve there as they do on the Host. A Sandbox's overlay names the top
layer by its Host path, since the runner keeps the overlay at `/storage`.

A cached disk is made again when it is damaged or its record no longer matches its chain.
`images.remove_disk` first reads the backing file of every qcow2 in the ALE cache and
refuses while one sits on the disk, then deletes the disk with its record, so a disk made
at that path later is never taken for the old one.

Linux is framed by `virt-customize` in about 30–40 s. Its setup script runs inside
libguestfs's appliance VM, because it calls the disk's own programs.

Windows is framed by `framework_windows.py`, run by the builder's Python, with nothing on
the disk executed; the install into an empty overlay takes 14–21 s on base 0.1.2, and the
framed disk boots to a ready sandbox in about 25 s. Windows cannot register a logon task
offline, so the framework is a Windows service instead. It is a registry key under the
control set the disk boots from, written into the SYSTEM hive with hivex, plus files copied
into `C:\ProgramData\ALE`:

- Checks first, from the hives alone (SAM for the account, SOFTWARE for automatic sign-in
  and UAC): each failure names the one thing the disk lacks. A disk left hibernated by
  Fast Startup cannot be written, and says so.
- The service `ALEGuestd` runs `guestd/service.py` as LocalSystem on the framework's own
  Python, copied to `C:\ProgramData\ALE\python`, as is guestd. At boot it limits
  `C:\ProgramData\ALE` to the system and Administrators, because files written offline
  carry no access list and otherwise come out writable by everyone, then gives Users read
  and execute access to `cua-driver`. It opens guestd's port to the runner's address only,
  waits for the agent's sign-in, and starts guestd and Cua Driver on the agent's desktop
  with the account's elevated token (the one UAC keeps beside the limited one), each in its
  own hidden console, restarting either that exits.
- The service reads the Host's `ale-sudo` SMBIOS serial and passes it to guestd, which
  runs agent commands with the desktop's limited token unless it is set.
- A Task image build (`QemuProvider.create_builder`) frames its own overlay on the base
  before the VM boots, so its `build.ps1` runs beside the same guestd as every sandbox, and
  `save_image` keeps that overlay, framework and build together, as the Task's layer.
- Before it is kept, `qemu/ntfs_discard.py` in the VM builder discards the overlay's clusters
  that are only NTFS free space, by the volume's `$Bitmap` (libguestfs `ntfscat-i`), with
  `qemu-io`. An overlay holds whole 64 KiB clusters, so files a build wrote and deleted stay
  in it: on Windows base 0.1.2 that was 103 of 749 MiB for a Task that installs nothing and
  90 of 2,417 MiB for a LibreOffice Task, each discarded in about 5 s. Free space is never
  zero-filled instead, which would write every free cluster into the overlay.

A Linux Task image is built the same way: the build VM's overlay is framed by
`virt-customize` before it boots, `build.sh` runs as root in `/opt/ale/environment`, and
`save_image` keeps the overlay as the Task's layer. Before it is kept, the VM builder's
`virt-sparsify --in-place` mounts each filesystem on the disk and trims it; the overlay
turns every trimmed cluster it holds into one that reads as zeros. A build that installs
`jq`, writes a 300 MiB file, syncs it and deletes it kept a 343 MiB layer, the same as
without the file, and the discard took 3 s.

### Registry disks

`ale vm-image push` publishes a qcow2, and `resolve_vm_image` in `images.py` or
`ale vm-image pull` acquires one, through the OCI distribution API directly, without the
Docker CLI or daemon:

- A published disk is a standalone, uncompressed qcow2; `ale vm-image push` refuses
  compressed clusters (`qemu-img check` counts them). The chunks are compressed on the
  wire, and a Host keeps the one copy it pulled, which reads at full speed under layers.
- The manifest is an OCI image manifest with `artifactType`
  `application/vnd.ale.vm-disk.v1` and the empty config. Its layers are the qcow2 file in
  512 MiB chunks, in order, each compressed alone at zstd level 3 as
  `application/vnd.ale.vm-disk.chunk.v1+zstd`, with the SHA-256 of the uncompressed chunk
  in its `ale.vm-disk.chunk.digest` annotation. The manifest's `ale.vm-disk.size` and
  `ale.vm-disk.chunk-size` annotations give the file and chunk sizes.
- A pull fetches eight chunks at once and writes each at its offset into
  `vm-images/<manifest digest hex>/disk.qcow2.partial` in the ALE cache, or into
  `<destination>.partial` for `ale vm-image pull`, leaving 64 KiB-aligned runs of zeros as
  holes. It checks the compressed bytes against the layer digest and the decompressed
  bytes against the annotation, and renames the file once `qemu-img` finds a standalone
  qcow2. A read that breaks continues with an HTTP range, and a pull run again keeps every
  chunk whose bytes already match the annotation. A digest-pinned disk already in the cache
  is used without asking the registry.
- The pulled disk is the base of the layers built on it, in place: `disk.json` beside it
  records it as a base under the manifest digest, and `images.remove_disk` refuses to
  replace it while a layer sits on it.
- Credentials are Docker's for the registry host, from a credential helper or `auths` in
  `$DOCKER_CONFIG/config.json` (`~/.docker` by default); without them the registry is asked
  anonymously. A registry on `localhost` or a loopback address is reached over plain HTTP,
  any other over HTTPS only.
- A push refuses a tag that exists. It compresses every chunk again but uploads only those
  the repository lacks, so a publish that stopped can be run again. It uploads four chunks
  at once, each in one request, from temporary files beside the source disk.
- GHCR documents a 10 GB limit on a layer and ten minutes for an upload (it accepted the
  14.7 GB single layer of Windows base 0.1.1). From GHCR, 512 MiB and 1 GiB chunks of the
  10 GB Windows base 0.1.2 pulled equally fast, 790–860 Mbit/s over four or eight
  connections against 400 Mbit/s over one. Four 512 MiB uploads at once finish inside the
  ten minutes on an uplink of 30 Mbit/s or more. Every zstd level up to 6 decompresses
  at about 2.9 GB/s on one core; level 19 would shrink that already compressed qcow2 by only
  0.5% more and compresses a hundred times slower.
- Through a registry on the same host, that disk published in 33 s and pulled in 22 s over
  eight connections, 27 s over four and 79 s over one, where two SHA-256 passes per byte
  bound a core without SHA instructions. As one gzip layer it took 444 s to publish with
  BuildKit and 246 s to `docker pull` and `docker cp`, and left 29 GB on disk, not 10 GB.
- Uncompressed, base 0.1.2 is a 17.7 GB file in 33 chunks: 9.52 GB on the wire, against
  9.96 GB for its qcow2-compressed form, and pulled in 28 s against 25 s on the same host.
  After pulling it and building two Windows Tasks, one that installs nothing and one with
  LibreOffice, the ALE cache held 19.6 GiB; with the compressed base and an uncompressed
  copy of it beside the pull, it held 29.1 GiB, and the first build took 101 s, not 68 s.

### Volumes in a VM

A Linux disk's framework includes `ale-volumes.service`, which runs
[`qemu/guest_volumes.py`](qemu/guest_volumes.py) as the guest boots, before guestd, the
display manager and any login. The runner hands it, as QEMU firmware configuration
(`-fw_cfg name=opt/ale/volumes`), the Sandbox's volume candidates, or a snapshot's volumes
for a VM booted from it. It mounts an overlay at each that is a directory on the disk, with
no link on its way, of that directory under `/opt/ale/volumes/<hash>/upper` on the same
disk, giving the layer the directory's owner and mode, and records what it mounted in
`/run/ale/volumes.json`, which the Provider reads once guestd answers. A Windows guest
mounts no overlay: a volume stays a directory of its disk.

A separate verifier that views a snapshot's volumes boots with an empty SCSI controller of
its own. Once guestd answers, the Provider attaches the snapshot's disk to it through the
runner's QEMU monitor (`serial=ale-view`): attached at boot, that copy of a bootable disk,
often of the very image the verifier boots, has the same partition identifiers, and Windows'
boot manager booted the verifier from it. A Linux guest gets the snapshot's disk read-only;
`volumes.py views` mounts its file system that holds `/opt/ale/volumes` read-only, without
replaying its journal, an overlay of each volume's layer and directory under a layer of its
own, and unmounts the rest of the disk. A Windows guest gets a copy-on-write overlay of
it, which it brings online, giving it a signature of its own, mounts its largest
partition, the solver's system drive, under `C:\ProgramData\ALE\viewed`, and makes each
viewed path a junction there in place of its own.

## Firecracker

`FirecrackerProvider` runs a container Task's prepared image, the one Docker runs, as a
Firecracker microVM with its own kernel, for a Run whose `sandbox.providers` names
`firecracker`. The Host needs Docker, `/dev/kvm` and `/dev/net/tun`; macOS and Windows
Hosts have no KVM, so they cannot run it. Measured on a 112-core Linux Host with
Firecracker 1.17.0, the guest kernel 6.1.155 its CI publishes, and the ALE container base:

- **The disk.** The image gets the framework layer Docker adds, is exported with `docker
  export`, and `firecracker/guest_init.py` and the image's `ENV`, `USER`, `WORKDIR`,
  `ENTRYPOINT` and `CMD` are appended. The runner's `mke2fs -d` writes the tar into an
  ext4 with neither journal nor metadata checksums, as the Host's user, so nothing is
  extracted on the Host; checksums doubled `mke2fs`'s time. The container base took 72 s
  (export 37 s, `mke2fs` 31 s) and fills 2.3 GB. The disk is kept in `firecracker-disks/`
  in the ALE cache under the image, runner and init, and every Sandbox of the image mounts
  it read-only. It is one flat filesystem, so Tasks on one base share none of it.
- **The writable disk** is a sparse ext4 the runner makes at start, the Task's
  `storage_mb` plus 512 MB or 32 GB, seeded with the Sandbox's settings and the runner's
  resolvers. The init lays it over the root with overlayfs, so a Sandbox costs what it
  writes: 27 MB once the container base's GNOME session was up.
- **Volumes.** Before it makes the overlay `/`, the init mounts at each volume an overlay of
  the image's directory and of what each read-only layer holds for it,
  `volumes/<hash>/upper`, under a layer on the writable disk, apart from the root's own, so
  a snapshot keeps it as it keeps the root and the writable disk counts it. A separate
  verifier gets the snapshot's disks attached read-only after its own, mounts the same
  layers under one of its own, and detaches the disks' mounts, out of sight.
- **The init** is PID 1 on the framework's Python. It mounts `/proc`, `/sys`, `/dev`,
  `/dev/pts`, `/dev/shm` (half the guest's memory, a VM's default and every Linux
  Sandbox's), `/dev/mqueue` and cgroup2, writes `hostname`, `hosts` and `resolv.conf`, starts
  guestd as root with its token on a private tmpfs, and runs the image's command as Docker
  would, with the request's variables. When the command exits, the microVM stops. On
  SIGINT, which the Provider sends through guestd before a snapshot, it stops every
  process, unmounts the writable disk and resets the machine, which ends Firecracker.
- **The network.** The kernel sets the guest's address (`ip=`): `172.30.0.2` on the
  runner's `tap0`, NATed to the runner's Docker network. Docker publishes guestd's port and
  the runner forwards it. Sealing adds the QEMU runner's rules on `tap0`.
- **The kernel** skips the PS/2 keyboard probe (`i8042.*`), which took half its boot;
  nothing uses the keyboard. It has no `nf_tables`, so an image cannot run Docker or
  iptables-nft inside, and the machine has no display device or GPU. A Sandbox of the
  container base answered in 1.6 s and drew its desktop in 5.5 s, where Docker took 0.75 s
  and 5.0 s.
- **The command's output** goes to `command.log` on the writable disk, outside the overlay.

## Orchard

Every macOS VM is a fresh Tart VM on the local Orchard controller and worker that
`scripts/setup_macos.py` runs; the Provider reads their address, credentials and Tart
from its state directory, so macOS needs no run settings. Setup and cleanup are in the
[macOS guide](../../../../../../docs/guides/macos-orchard.md).

A Tart disk is APFS, which only macOS reads, so `orchard/framing.py` frames an image on
the Mac, once per image, framework and account, the way the QEMU Provider frames a qcow2:
it clones the image with Tart and has `framework_macos.sh` change the clone's disk offline.
`scripts/setup_macos.py` installs that script root-owned as
`/usr/local/libexec/ale/framework-macos` with a sudoers rule for it alone, and the Provider
refuses to frame when the installed copy differs from its own. The framer attaches the disk
without mounting it, mounts only its Data volume, hidden from the Finder and without device
files or set-uid programs, follows no link on it and runs none of its programs. It checks
that the account it frames for, the Task's or `admin` for a `build.sh` VM, exists with a
home and is signed in automatically (`autoLoginUser` and `/etc/kcpassword`), then installs:

- guestd and a standalone Python under `/opt/ale` (the Python readable by every account,
  for the agent's desktop tool and the Harnesses' helper scripts);
- Cua Driver in `/Applications`, with Accessibility and Screen Recording rows in the
  system TCC database carrying the app's designated requirement, as System Settings
  writes them, and the account's replayd approval to capture without the system window
  picker, with the dates replayd checks and a reminder date far ahead;
- Remote Login and passwordless sudo for `admin`, the account Orchard logs in as.

The clone is then booted once, without the agent or the Driver, until the account is
signed in and `sfltool dumpbtm` shows its LaunchAgents registered, plus 30 seconds: macOS
posts a never-booted disk's background-item notice a few seconds after it registers the
items and reports neither the notice nor having none. Notification Center shows a notice
nobody dismissed again at every login, so the account's delivered notifications are then
cleared from its `usernoted` database, as the account, and `usernoted` is killed before it
can write them back at shutdown. `/Library/Preferences/com.apple.windowserver.displays.plist`
is deleted too: macOS restores the display mode saved there on any display of the same
identity, whatever size Tart gives it, so every Sandbox would keep the first login's
[resolution](#desktop-resolution). The guest then shuts itself down, as a `build.sh` VM's
does before its disk is kept: Orchard stops a VM with `tart stop --timeout 5`, which powers
macOS off when its signed-in desktop does not shut down at once and loses what is still in
memory. The stopped VM becomes the local Tart image `ale-framed-<key>`; every Sandbox boots
a clone of it, which on APFS copies no data.

In each Sandbox the agent works in, the Provider starts the Driver in the account's login
session (`launchctl asuser`, then `open` so LaunchServices runs it under the identity the
grants name) and waits until it captures. guestd sends desktop pointer actions with
`delivery_mode: foreground`, because the Driver otherwise refuses to move the real pointer
over a background app's window. Elevation for `[agent].sudo` is set at every boot, as on
Linux ([Agent elevation](#agent-elevation-on-linux-and-macos)). `admin` keeps passwordless
sudo for ALE, which runs as root through it, so a Task running as `admin` must declare
`sudo = true`. A verifier VM runs no agent, so it gets neither the desktop nor the elevation
and its checks, and a `build.sh` VM is kept exactly as the build left it.

Preparing an OCI reference resolves its tag through registry manifest metadata without
downloading VM disks or checking the worker cache, and checks the installed framer, the
local controller and an eligible ARM64 Tart worker; a local image `build.sh` built is only
looked up on the worker. Framing clones either. The clone pulls a reference Tart's cache
lacks with `--deduplicate`, a flag Tart's help hides: the pulled disk starts as an APFS
clone of a cached image that shares at least 1 GB with it at the same offsets, such as the
base's earlier version, and the shared chunks take no new space. Provisioning checks the
framed image and the CPU and memory the controller reports, and measures the root disk
from the guest. Provisioning (1800 s, with queueing and pulls) and guest readiness (300 s)
have separate deadlines.

Orchard credentials travel through verified TLS, never command arguments or Run records.
SIP is unchanged. `orchard/desktop.py` verifies actual capture as the Task account in every
agent Sandbox before the solver starts. Desktop input uses native screenshot pixels; cursor
read-back uses logical points, following the Driver's coordinate contract.

Commands and file reads use separate authenticated WebSocket exec channels. File reads
are bounded, screenshot transfers are integrity-checked, and command output is
acknowledged after Host consumption to avoid overrunning Orchard's exec output queue.

Its profile attaches no GPUs. Orchard credentials remain on the Host; Orchard/Tart and the
selected image are trusted infrastructure.

A kept VM stays running and holds its VM slot. Orchard keeps no labels on a VM, so each
kept VM and each snapshot has a record under the local Orchard's state
(`~/.local/share/ale/macos/retained`): its handle, the episode that first kept it, role,
Task image, account, and the image it boots with the account framing found.
`ale sandbox list` drops a record whose VM or image is gone.

Cleanup checks the VM name and UID, waits for worker stop acknowledgement, then deletes
it. Ambiguous creation requires operator reconciliation rather than adopting a same-name
VM. Deletion is not conditional, so the controller must not replace names during cleanup.
Worker disk garbage collection is asynchronous. Cancellation, command timeout or lost
command transport tears down the VM; uncollected evidence can be lost. Cleanup errors
are recorded separately from execution failures.

### Restricted networking

An `open` Task's VM has Tart's own NAT network throughout. A `block`, `allowlist` or
`blocklist` VM runs on Softnet, Tart's packet filter, which sits on the Mac between the VM and
the Mac's network, outside the guest, where the agent cannot lift it even as root. It needs:

- Softnet 0.25.0 or later, for its protocol and port rules. `scripts/setup_macos.py`
  installs it on macOS 26 or later, which it requires, as `/usr/local/libexec/ale/softnet`,
  root-owned and set-user-ID because it sets the VM's network up as root, and links it
  into the worker's PATH;
- Orchard 0.57.0 or later, whose worker changes a running VM's Softnet rules in place, and
  Tart 2.34.0 or later, which it does that through. Setup pins Orchard 0.58.0 and Tart
  2.41.1, and restarts services an earlier pin started, once both are in place, unless
  their controller holds VMs or their worker runs without it.

The Provider declares the restricted modes only where setup's Softnet is the worker's,
and provisioning also checks the running controller's and Tart's versions, refusing a
restricted Task before any VM exists; the Softnet release itself is setup's to pin.

Orchard changes a running VM's rules in place but not whether it has Softnet, so the VM
boots on Softnet with every IPv4 address allowed (`netSoftnetAllow = ["0.0.0.0/0"]`) for
the framework's phases. Softnet drops IPv6 in every phase. Sealed, the VM's rules block
`0.0.0.0/0` and allow only:

```text
in proto tcp @host port 22            Orchard's SSH from the Mac, which exec rides on
out proto tcp @host port <Gateway>    the Gateway's port on the Mac
out proto tcp @host port <proxy>      for allowlist or blocklist, the egress proxy's port
```

`@host` is the VM's gateway, the Mac's address on the VM's network. It also answers the
VM's DNS, which stays shut with every other port of the Mac; Softnet answers DHCP itself.
Softnet forgets a connection the VM sent nothing on for five minutes, so the in-VM Gateway
relay's connections, and guestd's egress relay's to the proxy, send TCP keepalive probes
after a minute of silence.

A change PUTs the VM's whole VMSpec with the new lists, then waits until the worker's
`observedGeneration` reaches the new `generation`. Softnet clears its flow table when the
rules change, which silently strands the TCP connections it carried, and the controller
sends every exec session of a VM over one pooled SSH connection. So both guest sessions
close first, and a command ends that connection from inside while the old rules still carry
its close: it stops the `admin` account's `sshd-session` processes, which serve it, found
with `ps`, since `pgrep` and `pkill` do not see them in a macOS 26 guest. Two new sessions
then open a new connection. A worker that cannot change the rules in place stops and starts
the VM instead, and Orchard records no restart of that kind (`restart_count` counts its
scheduler's restarts, `started_at` is set once), so the change fails when the `running`
condition reads false while it waits, or when the guest's `kern.bootsessionuuid` differs
afterwards.

`open_egress` stops guestd's egress relay before it opens the rules, and so does shutting
the guest down for a snapshot or a `build.sh` image. A VM driven again is sealed when its
record's `netSoftnetBlock` is not empty, and then gets this run's egress relay; its rules
stay those of the run that sealed it until this run's setup opens them.

On a shared Mac, Softnet sets `bootpd`'s lease time to 600 seconds and has it ignore DHCP
client identifiers (`com.apple.InternetSharing.default.plist`), for every VM and Internet
Sharing on the Mac; cleanup leaves that setting. Any local account can run its
set-user-ID program.
