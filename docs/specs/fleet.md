# Fleet

A Run drives every Sandbox from the one machine that runs it, and a machine can hold only
so many: a Mac starts at most two macOS VMs, and a Linux machine is bounded by its CPUs,
memory and disk. The Fleet pools many machines behind one control plane. Each machine is a
**Node**; its **node agent** drives the machine's own Providers, and the control plane
keeps the ledger of what every Node holds, places each Sandbox, queues what does not fit
and reconciles its ledger with what the Nodes report.

The control plane is thin and the Nodes are thick: isolation, image identity, the guest
service and the network policy stay with the Providers that already enforce them, on the
Node, so the Fleet adds scheduling without a second implementation of any guarantee. It
boots nothing before a request: an idle macOS VM would hold half of its Mac.

One control plane serves the Fleet, with its ledger in one SQLite file (`ale fleet serve`).
Every peer authenticates with the one Fleet token, `ALE_FLEET_TOKEN`.

## Network

ALE builds no network of its own: how the machines are connected is the operator's choice,
such as one LAN or Tailscale. The Fleet needs only that

- every Node and every Run's machine reaches the control plane;
- every Run's machine reaches each node agent's port.

Nodes never need to reach each other, and a Run's machine accepts no connection: the Run
opens every connection it uses. The Fleet does not encrypt, so its ports belong on a private
network.

Connectivity is checked when a Node joins. Its registration proves it reaches the control
plane, and before the control plane counts the Node it connects back to the address the
node agent advertised and checks that the agent answering there is the one registering. When
it cannot, it refuses the registration with the reason, typically an address the other
machines cannot reach. A loopback address reaches only the Node's own machine, so it is
refused unless the control plane itself listens only on loopback and serves that one machine.
A Run that cannot connect to a Node it was placed on treats it as that Node refusing
([Lifecycle](#lifecycle)); a connection lost midway is an infrastructure failure instead.

## Nodes

A node agent registers by sending its **declaration** and then sends a heartbeat every
few seconds. The declaration is probed, never configured: it holds what each Provider's
own `preflight` and `capabilities` reported on the machine, and an operator can only narrow
it (labels, resources kept for the machine itself, fewer Providers).

| Part | Holds |
|---|---|
| `resources` | CPUs, memory, disk, GPUs by model, and `macos_vm_slots` |
| `profiles` | One **Sandbox profile** per combination the Node can start: Provider, kind, operating system, architecture, desktop, network modes, snapshots, GPUs, CPU and memory limits, and the macOS VM slots each Sandbox holds |
| `framework` | The identity of the framework its engine installs into every Sandbox |
| `boot_id` | Changes whenever the node agent restarts, which ends every Sandbox it held |

Profiles are real combinations, not independent lists: a Mac that runs macOS and arm64
Linux does not run x86-64 Windows, and a declaration that listed systems and
architectures separately would say it did. macOS VM slots are counted per physical machine,
apart from CPU and memory, because Apple's Virtualization framework refuses a third macOS VM
whichever runtime starts it.

A heartbeat carries the Node's full **roster** (every Sandbox it holds, with its state and
lease expiry), the images it prepared, each by its key with its size and how long preparing
it took, and its free disk. A Node silent for 30 seconds is offline.

## Placement

A Run asks for each Sandbox with a **demand**: kind, operating system, architecture,
network mode, resources, the image's key, and the Run's framework identity. A Sandbox runs
the framework of the engine driving it, so only a Node with the same framework identity
serves it. Every Node prepares the images it serves itself, and Nodes never copy images from
one another; an image's **key** is the identity of what it is prepared from, which the Run
knows before any Node prepares it, and a Node keeps each image it prepared under its key.

1. **Filter.** A Node is a candidate when it is online and not draining, has a profile that
   matches the demand, has not refused this Sandbox, can be reached by its Run, and has the
   resources free now: CPUs, memory, macOS VM slots and GPUs of an accepted model. A demand
   that no registered Node could serve even when idle fails at once.
2. **Score.** The estimated time to a ready Sandbox: on a Node that lacks the image, the
   time preparing it took the Nodes that did (five minutes while none has), plus the Node's
   measured create time for that Provider. Preparing an image takes minutes where creating a
   Sandbox takes seconds, so a Task's episodes go to the Nodes that already hold its image;
   ties go to a fixed per-image order of Nodes (rendezvous hashing), which keeps each image on
   the same few Nodes.
3. **Reserve** the resources in the ledger and sign a **grant**: the Sandbox, its Run, the
   Node and the demand, valid for a few minutes. A Node creates a Sandbox only on a grant
   that names it, so every Sandbox a Node holds is one the ledger reserved, and it admits
   the grant's demand rather than whatever the creating request claims.
4. **Queue** what fits nowhere. The queue serves Runs in turn, longest-waiting first, so a
   large Run cannot starve a small one, and a Run's request that fits nowhere does not hold
   back its later requests that fit elsewhere.

A Sandbox holds its Node's resources from placement until the Node no longer lists it, so a
macOS VM that is shutting down still counts against its Mac.

## Lifecycle

| Status | Meaning |
|---|---|
| `queued` | Waiting for a Node; it lapses if its Run stops asking about it |
| `placed` | Reserved on a Node, with a grant for the Run; it lapses if its Run stops asking before creating it |
| `starting`, `ready` | As the Node's roster reports |
| `releasing` | Released by its Run, not yet gone from the Node's roster |
| `released` | Gone |
| `failed` | No registered Node could serve the demand |
| `lost` | The Node stopped reporting it |

- **Idempotent requests.** The Run names each Sandbox. Asking again with the same name
  returns the same Sandbox, so a lost reply never makes a second; the same name with other
  terms is refused.
- **Refusal.** A Node that refuses a grant before creating anything (its own admission
  check disagrees with the ledger) sends the Sandbox back to the queue, never to that Node.
  A Run that cannot reach the Node it was placed on refuses it the same way; that Run's
  Sandboxes then go there no more, and the control plane records it as an event.
- **Leases belong to the Node.** Every Sandbox has a lease its Run renews with the Node
  directly. The Node destroys a Sandbox whose lease ran out, whether or not the control
  plane is up, so a Run that crashes leaves nothing behind. The roster reports each lease.
- **Reconciliation** resolves every disagreement toward what the Node reports. A Sandbox the
  Node holds that the ledger does not know is destroyed after a short grace (the ledger may
  be the one behind); one its Run released is destroyed at once, by the heartbeat's reply. A
  `starting` or `ready` Sandbox missing from the roster is `lost`, unless its lease had run
  out, in which case its Node destroyed it and it is `released`.
- **Lost Nodes.** An offline Node's Sandboxes are `lost`, and its unstarted placements go
  back to the queue. A Node that returns with the same boot ID and lists a lost Sandbox
  restores it; a Node that restarted has a new boot ID and none of its old Sandboxes. A lost
  Sandbox is not moved: whether its episode runs again elsewhere is the Run's retry policy.
- **Restarts.** The ledger is written through on every change, so a restarted control plane
  resumes where it stopped and gives every Node its timeout to check in again.
