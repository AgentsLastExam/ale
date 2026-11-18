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
