# Providers

Providers turn prepared Task images into Sandboxes. ALE ships the following backends:

| Module | Backend | Prepared input |
|---|---|---|
| `docker.py` | local Docker container, or a Kata microVM per container ([Kata](#docker-on-kata-containers)) | immutable OCI image identity |
| `qemu.py` | QEMU/KVM guest runner | materialized qcow2 identity |
| `firecracker.py` | Linux microVM on Firecracker/KVM | immutable OCI image identity, converted once to an ext4 disk |
| `daytona.py` | Daytona cloud container | immutable OCI image identity, pushed by digest |
| `orchard.py` | macOS VM on Orchard/Tart | digest-pinned OCI image, or a local image `build.sh` built |
| `fleet.py` | a Sandbox of any of these on a Fleet Node ([Fleet](../../../../../../docs/specs/fleet.md)) | what the Node's Provider takes; each Node prepares its own images |

`daytona_api.py` is its REST client and `daytona_image.py` adapts and publishes images.
`firecracker_init.py` is the init every microVM boots; it runs in the guest.

They implement the `ale.core.sandbox.Provider` and `Sandbox` contracts: capability
preflight, image verification, lifecycle, command/file transport, egress changes,
observed identity, resource allocation and optional retention. Harness and episode
protocol code must use those contracts rather than backend-specific methods.

The normative contracts are
[`../../../../../../docs/specs/sandbox-image.md`](../../../../../../docs/specs/sandbox-image.md)
and [`../../../../../../docs/specs/security.md`](../../../../../../docs/specs/security.md).

## Gateway address

Every Sandbox dials its Gateway at `SANDBOX_GATEWAY_PORT` (7412), whatever port the Host
serves it on.

| Provider | Where the fixed port leads |
|---|---|
| Docker | `ale-gateway.internal` names the Host's address on the container's bridge. The helper that holds `NET_ADMIN` in the container's network namespace keeps an `ALE_GATEWAY` nat chain sending that address's port 7412 to the Gateway's port; a reattaching run flushes and refills it. `open` Tasks run the helper for this rule alone. Under Kata the helper rewrites the port with tc filters instead ([Kata](#docker-on-kata-containers)) |
| QEMU | The runner's `ALE_GATEWAY` nat chain sends `172.30.0.1:7412` from the guest bridge to the Host at the Gateway's port, and a reattaching run refills it |
| Firecracker | As QEMU, from the guest's tap device in the runner |
| Daytona | `guestd/tunnel.py` listens at 7412 on the sandbox's loopback |
| Orchard | A relay the Provider starts as root at boot listens at `127.0.0.1:7412` in the VM and connects to the Gateway's port at the Mac, the VM's default IPv4 route; it lives until the VM is deleted |
| Fleet | The Node's Provider's own route, to the port where the node agent listens for the Run; the agent carries each connection over the Run's tunnel to its Gateway |

## Snapshots

| Provider | How a snapshot is saved, booted and destroyed |
|---|---|
| Docker | `docker stop`, then `docker commit` to `ale-snapshot:<sandbox id>`. The image keeps the container's command and labels, with `ale.retention=snapshot`, `ale.prepared` naming the prepared image, and no GPU label. A container boots from its image ID; `docker rmi` destroys it and fails while a container still uses it |
| QEMU | The guest shuts down: Windows with `shutdown.exe /s`, Linux with `sync` and then `systemctl poweroff`, since its own power-off leaves files written just before it empty. The overlay itself becomes `qemu-snapshots/<sandbox id>/disk.qcow2` in the ALE cache, a [layer](#disk-layers) on the disk it booted from, beside `snapshot.json` with its episode, role, OS and prepared image; nothing is copied. When a guest booted from a snapshot is saved as one, its overlay first takes in that snapshot's clusters (`qemu-img rebase` onto the snapshot's parent, which reads only what the snapshot holds), so a snapshot never sits on another one. A VM boots from it through a fresh overlay, once the disk below it is the one it was taken on. It needs every disk below it, so snapshots are destroyed before `vm-builds` is cleared. Destroying it fails while a runner mounts it |
| Firecracker | The Provider has the guest's init (PID 1) stop every process and unmount the writable disk, so the disk needs no journal replay, and waits for the runner to exit. The writable disk then becomes one more read-only layer: `firecracker-snapshots/<sandbox id>/` in the ALE cache holds the root disk, any layers under it and a sparse copy of the new one, hard-linked where the filesystem allows, so it outlives the disk cache without copying it, beside `snapshot.json` with its episode, role, OS, prepared image and digest. A microVM boots from it with the layers stacked under a fresh writable disk, eight at most, since Firecracker's devices are few. Destroying it fails while a runner mounts it |
| Daytona | None: snapshots count against an organization quota shared with other tools, so `accepts` refuses them |
| Orchard | None: Orchard keeps no VM past its episode, so `accepts` refuses retention and snapshots |
| Fleet | The Node's Provider saves it on that Node, as in its row, and Sandboxes that boot from it are placed there. The Fleet's handle for it is `fleet:snapshot:<sandbox>`; destroying it has the Node's Provider destroy it at the Node's next heartbeat |

## Reattaching by handle

A Sandbox's `handle` names it to `reattach` and `discard`, by which another process on the
same machine, such as a restarted Fleet node agent, drives it again or destroys it.

| Provider | How `reattach` and `discard` find a Sandbox by its handle |
|---|---|
| Docker | `docker:<container>`: `docker inspect` finds the container, whose labels must name it ALE's, and it must be running the recorded image. Reattaching re-applies the [Gateway route](#gateway-address) as a reattaching run does and reads the seal back from the container: a runc one is sealed when it is off the default bridge, a Kata one when the seal's drop filter is on `tap0_kata`. `discard` removes the container and its network; one that is already gone needs nothing |
| QEMU | `qemu:<runner>`: the same for the runner container, whose labels must name it ALE's and whose overlay must sit on the recorded disk. Reattaching refills the runner's `ALE_GATEWAY` chain and reads the seal back from its `FORWARD` drop rule. `discard` removes the runner and its overlay |
| Orchard | `orchard:<name>:<uid>`: the VM by its name, which must still have that UID and run the image framing recorded on this Mac. Reattaching opens new guest sessions; the Gateway relay started at boot outlives the old ones. `discard` deletes the VM unless Orchard replaced it |
| Daytona, Firecracker, Fleet | None: their Sandboxes have no handle, and `reattach` refuses |

## GPU models

A Task's `gpu_types` decides which GPU models a Sandbox may get
([GPUs](../../../../../../docs/specs/task-authoring.md#gpus)). Every Provider checks the
models it got with `ale.run.gpu.accepts_model`, which applies that rule.

| Provider | How it gives only the models the Task accepts |
|---|---|
| Docker | Reads the Host's GPUs with `nvidia-smi` and leases only those among `container.gpus`, or among all when it is unset, of an accepted model. Too few refuses the Sandbox before it starts, and the container must then show exactly the leased UUIDs. Under Kata every GPU request is refused: `--gpus` does not reach the microVM |
| QEMU | A device bound to `vfio-pci` has no Host driver to name its model, so the guest's `nvidia-smi` is read once it boots; a model the Task does not accept destroys the Sandbox before any Task code runs. The pool is leased without regard to model, so a pool of several models can refuse a Task while an accepted GPU is free |
| Daytona | Maps the names to Daytona's GPU types as Harbor does: `h100` and `nvidia-h100-80gb` to `H100`, `h200` and `nvidia-h200-141gb` to `H200`, `rtx-pro-6000` and `nvidia-rtx-pro-6000` to `RTX-PRO-6000`, `rtx-4090` and `rtx-5090` to themselves, ignoring case. It asks for them in the Task's order, which Daytona tries in turn, and leaves out names with no type; a Task left with none is refused before anything is created. The models attached are then checked against the Task's own names, as on every Provider. `MI355X` is never asked for, as `nvidia-smi` cannot see it to record it. A Task naming no model sends no type, so Daytona chooses |
| Firecracker | Refuses every GPU request: Firecracker passes no GPU through |
| Orchard | Refuses every GPU request |

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
command, Orchard once guestd answers in an agent's VM. The script runs on the framework's
Python:

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

## Docker on Kata Containers

`container.runtime = "kata"` (`--set container.runtime=kata`) starts every Docker sandbox
with `--runtime io.containerd.kata.v2`: each container is then a QEMU microVM with a guest
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
| Moves every frame between the VM's tap (`tap0_kata`) and the container's veth with tc filters, at preference 49152, so netfilter in the sandbox's namespace never sees the guest's traffic: the runc helper's rules would neither route the Gateway nor confine the Host | The same helper puts tc filters ahead of Kata's, in that namespace, outside the VM. Port 7412 to the Host's address is rewritten to the Gateway's port, and replies back (`pedit` and `csum`; 64 MiB each way came through intact). A sandbox that is not `open` reaches no other port of that address, and its proxy port only under `allowlist`. A reattaching run replaces only the Gateway and proxy filters |
| Hands the VM its networks at boot: `docker network connect` succeeds and the VM never sees the network | No internal network: the sandbox stays on the default bridge, and sealing adds filters that drop every frame but those to and from the Host's address, ARP included, so it reaches neither the Internet nor another container on the bridge. A sandbox that is not `open` starts sealed: the filters are in place before guestd connects, though the image's own command starts before them. Opening egress removes them and checks that no drop filter is left |
| Docker's embedded DNS server, `127.0.0.11` on a user-defined network, is in the namespace and out of the guest's reach | The default bridge's `resolv.conf` names the Host's upstream servers, which the guest reaches |
| Makes the VM exactly the container's memory limit; the guest kernel and agent take about 96 MiB plus 2.5 % of it (512 MiB declared left 395 MiB usable, against 504 MiB on runc) | Starts the VM with `memory_mb + 96 + memory_mb / 40` MiB, which leaves the Task's processes at least what runc gives them; the allocation records `memory_mb`. With the base image's desktop running in 512 MiB, one process then held 312–320 MiB against 240–288 MiB on runc: in a VM the kernel's own work for the desktop is not charged to the container |
| Gives the VM one vCPU more than `--cpus` and applies the quota in the guest's cgroup | Nothing: the quota holds, and `nproc` reports `cpus + 1` rather than the Host's count |
| Cannot take Docker's `--gpus` | Refuses GPU requests |
| Drops what a filter drops: tc cannot answer with a rejection, so a sealed guest's DNS lookup took 112 s to fail, and broke sudo's first use when the guest did not yet list its own host name | A sandbox that can be sealed gets `NET_ADMIN`, and the framework removes the guest's default route while it is sealed and restores it when it opens. What runc's internal network refuses then fails at once here too; only a blocked port of the Host's address, which stays on-link, times out. The capability works inside the VM only: root there that re-adds the route makes failures slow again, and still reaches nothing |
| Runs no seccomp filter in the guest (`disable_guest_seccomp`) | Nothing: the VM is the boundary, and the filters are outside it |
| Writes Docker's `/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf` into the container's own files, where runc mounts them | A snapshot keeps them; a Sandbox booted from it gets its own, a moment after it starts |
| Shares the container's root filesystem with the guest over virtio-fs | Nothing; see the costs below |

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

## Daytona

Daytona's sandboxes are built for a different trust model than ALE's, so `daytona.py`
works around several of its defaults to hold the same contract. Each row was measured on a
live organization (daemon 0.225.2). Operating it is in
[`docs/guides/daytona.md`](../../../../../../docs/guides/daytona.md).

| Daytona | What the Provider does |
|---|---|
| Builds sandboxes only from registries registered with the organization; push credentials expire hourly | `daytona_image.py` adds one layer (`USER root`, and `iptables` if absent), pushes it by digest to Daytona's own registry, and keeps one organization registry entry, `ale-transient`, holding the current credentials. The sandbox is built `FROM` that digest, so nothing is rebuilt remotely |
| Runs its daemon, and every command sent through it, as the image's final `USER` | That layer's `USER root`, so guestd is root |
| Replaces the image's entrypoint and command with its own daemon | Starts them itself, detached, as the image's `USER` with its `ENV` and working directory, then waits for the desktop as Docker does |
| Its daemon listens inside the sandbox (2280, 22220, 22222, 33333, and any other port PID 1 opens) and runs commands for anyone, as root | `iptables` owner rules reject every non-root user, IPv4 and IPv6; a probe as the agent must fail on every local address, or the sandbox is destroyed |
| Accepts no connection from the sandbox outward; its SSH gateway refuses `-R` | guestd runs over two SSH exec sessions, one for commands and one for file reads. A third carries `guestd/tunnel.py`, which delivers the Gateway on the sandbox's loopback; the sandbox names an index into the Host's list, never an address |
| Switches `networkBlockAll` and a domain allowlist at runtime (Tier 3 or 4 only), asynchronously | `block` uses `networkBlockAll`. `allowlist` uses `domainAllowList` with the hosts as declared, which Daytona matches as ALE does. `accepts` refuses addresses, ports and more than 100 hosts. Every switch waits for a TLS handshake with a public host the Task did not declare to behave as the setting says |
| On a GPU runner, accepts connections under `networkBlockAll` that it passes nothing on, and names a proxy of its own in every process's environment (see below) | guestd starts without the runner's proxy variables, unless the image or Task sets them. The accepted connections are left as they are, as under the allowlist on every runner: the conformance suite counts the network as reached only when a TLS handshake with a public host completes |
| Serves GPUs from its own region, in several models, optionally as spot capacity it may reclaim | Asks for those of the Task's models Daytona offers ([GPU models](#gpu-models)) and never spot. The GPUs are read with `nvidia-smi` as the agent and recorded; another count or model, or a GPU the agent cannot open, destroys the sandbox |
| Mounts the Host driver's libraries without registering them with the linker (see [GPU driver capabilities](#gpu-driver-capabilities)) | Runs `ldconfig` as root before it reads the GPUs, as Docker's NVIDIA paths do with their linker-cache hook; a sandbox where it fails is destroyed |
| Rate-limits creates | A create answered 429 made nothing and is retried, honoring `Retry-After`, for up to five minutes. No other failed create is repeated, since it may have made a sandbox |
| Creates a sandbox even when the caller is cancelled mid-request | Every way out of a start deletes what Daytona made; a create whose reply was lost is found by its unique name |
| Organizations are shared between tools | Labels live under `ale.run.*`, and listing and destroying act only on those. Every sandbox has a lifetime (`ttlMinutes`), so one a crashed run leaves behind is deleted by Daytona |

Daytona's allowlist, as measured: a name not on the list does not resolve. `example.com`
admits that host alone. `*.example.com` admits every name below it, at any depth, and
`example.com` itself. Ports 80 and
443 go through Daytona's proxy, which forwards by the name in the TLS handshake or HTTP
request to that name's own host, so sending an allowed name to another address reaches only
the allowed host; other ports of unlisted addresses are unreachable. The proxy accepts every
TCP connection to port 443 before it decides, which is why egress is checked with a TLS
handshake rather than a connect.

A GPU runner enforces the same settings differently, as measured on H100 sandboxes. Under
`networkBlockAll` a CPU runner refuses every connection, while a GPU runner accepts TCP
connections to ports 80, 443 and 53 of any address and passes nothing on them: a TLS
handshake ends at once, an HTTP request gets no answer, and no name resolves. Every process
in a GPU sandbox also starts with `HTTP_PROXY`, `HTTPS_PROXY` and their lower-case forms set
to `http://172.20.0.1:18080`, and `NO_PROXY` to loopback. That proxy applies the sandbox's
settings too: under `networkBlockAll` it answers every request with 403, under the
allowlist it forwards only to allowed hosts, on any port. Direct connections otherwise
behave as on a CPU runner, so the agent needs neither the proxy nor its variables. Under
`networkBlockAll` an agent on a GPU runner therefore sees a connection to the Internet open
and then fail where a CPU runner refuses it at once; under the allowlist every runner does
that for a host the list leaves out.

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
  per disk and Host: about 4.5 GB for the 2.3 GB Linux VM base, 17.7 GB for the 10.2 GB
  Windows base 0.1.2, which took about 30 s. A compressed disk is decompressed on every
  read: with the zstd-compressed Windows base under a Task layer a guest booted 15% slower
  and read the base's files 4–5 times slower.
- The resolved image keeps the request's prepared identity and observes the layer that
  boots, which the RunLock records.

A VM boots from a qcow2 overlay on that disk, in `qemu/<sandbox id>/`; the disks, overlays
and snapshots all live in the ALE cache (`ALE_CACHE_DIR`, `~/.cache/ale` by default). The
runner opens them with `cache=none,aio=native`, which bypasses the page cache, and ZFS then
bypasses its own cache as well, so the ALE cache belongs on local SSD. From a ZFS pool of
5400 rpm disks a Linux guest took 115–133 s to come up with its desktop rather than 20 s,
and a Windows guest 250–310 s rather than 52 s.

### Disk layers

A framed disk, a Windows Task image and a snapshot are each a qcow2 overlay on the disk
below it, never flattened. Beside each, `<name>.json` records the identity of its parent and
the SHA-256 of its own file, taken once when it is made (`images.record_layer`); the base a
chain starts from, a pulled disk or the copy of a local one, records the identity the disk
was resolved under. A layer names its parent by a path relative to itself, so the chain
stays whole when the ALE cache moves. `images.layer_identity` follows a chain with
`qemu-img info --backing-chain` and refuses one in which a layer sits on another parent
than its record names; every prepared disk with a backing file is checked that way before
it boots.

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
- Before it is kept, `ntfs_discard.py` in the VM builder discards the overlay's clusters
  that are only NTFS free space, by the volume's `$Bitmap` (libguestfs `ntfscat-i`), with
  `qemu-io`. An overlay holds whole 64 KiB clusters, so files a build wrote and deleted stay
  in it: on Windows base 0.1.2 that was 103 of 749 MiB for a Task that installs nothing and
  90 of 2,417 MiB for a LibreOffice Task, each discarded in about 5 s. Free space is never
  zero-filled instead, which would write every free cluster into the overlay.

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

## Firecracker

`FirecrackerProvider` runs a container Task's prepared image, the one Docker runs, as a
Firecracker microVM with its own kernel. It is selected with `container.provider =
"firecracker"`. The Host needs Docker, `/dev/kvm` and `/dev/net/tun`; macOS and Windows
Hosts have no KVM, so they cannot run it. Measured on a 112-core Linux Host with
Firecracker 1.17.0, the guest kernel 6.1.155 its CI publishes, and the ALE container base:

- **The disk.** The image gets the framework layer Docker adds, is exported with `docker
  export`, and `firecracker_init.py` and the image's `ENV`, `USER`, `WORKDIR`,
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
- **The init** is PID 1 on the framework's Python. It mounts `/proc`, `/sys`, `/dev`,
  `/dev/pts`, `/dev/shm` (half the guest's memory, a VM's default, where Docker gives
  64 MB), `/dev/mqueue` and cgroup2, writes `hostname`, `hosts` and `resolv.conf`, starts
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

A Tart disk is APFS, which only macOS reads, so `orchard_framing.py` frames an image on
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
`sudo = true`. A separate verifier VM runs no agent, so it gets neither the desktop nor the
elevation and its checks, and a `build.sh` VM is kept exactly as the build left it.

Preparing an OCI reference resolves its tag through registry manifest metadata without
downloading VM disks or checking the worker cache, and checks the installed framer, the
local controller and an eligible ARM64 Tart worker; a local image `build.sh` built is only
looked up on the worker. Framing clones either. Provisioning checks the framed image and
the CPU and memory the controller reports, and measures the root disk from the guest.
Provisioning (1800 s, with queueing and pulls) and guest readiness (300 s) have separate
deadlines.

Orchard credentials travel through verified TLS, never command arguments or Run records.
SIP is unchanged. `orchard_desktop.py` verifies actual capture as the Task account in every
agent Sandbox before the solver starts. Desktop input uses native screenshot pixels; cursor
read-back uses logical points, following the Driver's coordinate contract.

Commands and file reads use separate authenticated WebSocket exec channels. File reads
are bounded, screenshot transfers are integrity-checked, and command output is
acknowledged after Host consumption to avoid overrunning Orchard's exec output queue.

Only explicit `network.mode: open` is supported. Restricted networking needs integration
and validation across Orchard/Tart versions and worker network configurations. GPU,
retention, snapshot and reset requests are rejected. Orchard credentials remain on the
Host; Orchard/Tart and the selected image are trusted infrastructure.

Cleanup checks the VM name and UID, waits for worker stop acknowledgement, then deletes
it. Ambiguous creation requires operator reconciliation rather than adopting a same-name
VM. Deletion is not conditional, so the controller must not replace names during cleanup.
Worker disk garbage collection is asynchronous. Cancellation, command timeout or lost
command transport tears down the VM; uncollected evidence can be lost. Cleanup errors
are recorded separately from execution failures.
