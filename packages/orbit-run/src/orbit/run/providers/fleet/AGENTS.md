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
  reaching the Node is kept, recorded as its Node's, and listed by `orbit sandbox list` for
  an operator to destroy. Closing it would need the control plane to tell such a snapshot
  from one a crashed Run's retention kept, which it keeps on purpose.

## A snapshot verifier's solver could land where its verifier cannot boot

**Problem.** A snapshot verifier boots on the Node and leaf that saved its solver's
snapshot. Placing the solver by its own needs could choose a leaf that does not serve the
verifier, such as one without GPUs for a verifier that retrains on them, or a Node too small
for it, and the verifier then failed after the agent's whole run.

**Context.** The Run's selection, whose needs nest the verifier's, shows only that some leaf
the Nodes lend serves both. Placement then chose the solver's Node and leaf by its demand,
which carried no word of the Sandbox that would later boot from its snapshot.

**Solution.** The solver's request names its verifier's resources
(`SandboxRequest.snapshot_verifier`), which its demand carries (`SandboxDemand.verifier`)
and its needs nest (`SandboxNeeds.verifier`). One profile must serve both, by the same rule
(`unmet`) in the Run's selection, in placement and in the node agent's admission, and
placement takes for the solver only a Node whose whole capacity holds the verifier as well.

**Decisions.**
- One profile, not one leaf with two profiles, serves both: every leaf's profiles today are
  supersets where a solver and its verifier differ, and one rule keeps the Run's selection,
  a leaf's own admission and the Fleet's placement in agreement.
- Selection compares profiles, which carry no GPU counts, so a verifier asking for more GPUs
  than any one Node has is refused when its solver is placed, before the agent runs, rather
  than when the Run starts.

## A solver that validation rounds save is placed for every save

**Problem.** Each validation round saves the solver on its leaf and boots the next turn's
solver from that snapshot, on the same leaf and Node; under separate verification no
verifier boots from it, so only the count of saves still to come says a solver needs a leaf
that saves snapshots, and how many in a row.

**Solution.** The solver's request carries that count (`SandboxRequest.planned_saves`),
which its demand carries too (`SandboxDemand.planned_saves`, protocol 5), and its needs
count its saves in a row from it as
[Provider selection](../../../../../../../docs/specs/sandbox-image.md#provider-selection)
says, so placement and the node agent's admission choose only a leaf that saves that many
(`CapabilityProfile.max_saves`). Under shared verification each round's verifier boots from
the snapshot too, so the request names its resources (`SandboxRequest.round_verifier`,
carried as `SandboxDemand.round_verifier`), and one profile serves both.
