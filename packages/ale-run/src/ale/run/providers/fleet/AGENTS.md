# Fleet Provider: problems and decisions

How this Provider works is in the Providers [README](../README.md#snapshots) and the
[Fleet spec](../../../../../../../docs/specs/fleet.md). This file records the problems
peculiar to it: what we saw, what we measured, and what we decided.

## A Sandbox booted from a snapshot went to another leaf than the one that saved it

**Problem.** A Sandbox that boots from a snapshot with other needs than the one saved, such
as a continuation with other resources or container features, could fail on the Fleet though
its snapshot's Node and leaf would serve it: the Node refused it with `snapshot <id> is
docker:kata's, not docker:runc's`. Found reading placement in
November 2026 and reproduced in the control plane's and the node agent's unit tests, with a
Node that lends `docker:runc` and then `docker:kata`.

**Context.** Placement took the Node's first profile that served a demand, and held a boot
from a snapshot only to the Node that keeps it. A demand with fewer container features, or
other resources, than the one saved can match an earlier leaf of that Node. Only the leaf
that saved a snapshot holds it, so the node agent refuses any other.

**Solution.** A Node's view in placement (`NodeView.snapshots`) maps each snapshot it keeps
to the leaf that saved it, from the ledger's `SnapshotRecord.provider` in the control plane
and from `SnapshotHolding.provider` in the node agent's admission, and `choose` takes only
that leaf's profiles for a boot from it. The control plane decides such a boot by that leaf
and that Node's whole capacity alone, and fails one they cannot serve or hold at once, with
the reason, rather than leaving it queued.

## A save its Run gave up on could leave a snapshot on its Node

**Problem.** The node agent's handler goes on when the Run's request is cut short, as the
bound on a snapshot verifier's save or the Run's cancellation cuts it: aiohttp's server, as
the agent runs it, cancels no handler whose client is gone. A save that finished then was
kept on the Node, which reports it at its heartbeat, and the control plane recorded it as
its Node's: a snapshot no result names (November 2026, found reviewing snapshot
verification).

**Solution.** The Run releases the Sandbox as it gives the save up, and the node agent
deletes a snapshot whose save finishes after its Sandbox's lease ended.

**Decisions.**
- What remains open: a save that finishes between the Run's giving it up and its release
  reaching the Node is kept, recorded as its Node's, and listed by `ale sandbox list` for an
  operator to destroy. Closing it would need the control plane to tell such a snapshot from
  one a crashed Run's retention kept, which it keeps on purpose.
