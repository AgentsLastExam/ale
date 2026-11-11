"""The control plane's rules: register Nodes, place Sandboxes and reconcile with rosters.

Everything here is synchronous and takes the time from one clock, so a test drives it
step by step; :mod:`ale.run.fleet.server` puts it behind HTTP and runs :meth:`tick`.

The ledger is only ever an estimate of what the Nodes hold. Each heartbeat carries a
Node's full roster, and every disagreement resolves toward what the Node reports:

* a Sandbox the Node holds that the ledger does not, or that its Run released, is
  destroyed (one the ledger has never heard of after a grace period, in case the ledger
  is the one that is behind);
* a Sandbox the ledger expects that the Node no longer lists is lost;
* a Node that stops sending heartbeats loses every Sandbox it had, and one that comes back
  after a restart (a new boot ID) has none.

Leases are the Node's: a Run renews them with the Node directly, and the Node destroys
a Sandbox whose lease ran out, whether or not the control plane is up.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import NoReturn

from ale.run.fleet.placement import Choice, NodeView, Weights, choose, unservable
from ale.run.fleet.protocol import (
    PROTOCOL_VERSION,
    AcquireRequest,
    Grant,
    Heartbeat,
    HeartbeatReply,
    NodeDeclaration,
    Placement,
    RefusedRequest,
    SandboxStatus,
    sign_grant,
)
from ale.run.fleet.store import Event, NodeRecord, RunRecord, SandboxRecord, Store

__all__ = ["ControlPlane", "FleetError", "Timing", "UnknownSandboxError"]


class FleetError(Exception):
    """A request the control plane refuses; the message says why."""


class UnknownSandboxError(FleetError):
    pass


@dataclasses.dataclass(frozen=True)
class Timing:
    node_timeout_sec: float = 30.0
    """A Node silent this long is offline, and its Sandboxes are lost."""
    abandon_sec: float = 90.0
    """A queued or placed Sandbox its Run has not asked about for this long lapses."""
    grant_sec: float = 300.0
    """How long a grant lets the Run start creating the Sandbox on its Node."""
    orphan_grace_sec: float = 30.0
    """How long a Node may report a Sandbox the ledger never heard of before it is destroyed."""
    history_sec: float = 24 * 3600.0
    """How long finished Sandboxes stay in memory, for status pages."""


class ControlPlane:
    def __init__(
        self,
        store: Store,
        token: str,
        *,
        weights: Weights = Weights(),
        timing: Timing = Timing(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._token = token
        self.weights = weights
        self.timing = timing
        self._clock = clock
        now = clock()
        self.nodes = {record.declaration.name: record for record in store.nodes()}
        self.sandboxes = {
            record.sandbox_id: record for record in store.sandboxes(since=now - timing.history_sec)
        }
        self.runs = {record.run_id: record for record in store.runs()}
        self._orphans: dict[tuple[str, str], float] = {}
        # No Node or Run has spoken to this process yet: give each its timeout to check in
        # before its Sandboxes count as lost or lapse.
        for record in self.nodes.values():
            record.last_seen = now
        for sandbox in self.sandboxes.values():
            sandbox.seen_at = max(sandbox.seen_at, now)

    # --- events -------------------------------------------------------------------

    def _event(self, kind: str, *, node: str = "", sandbox: str = "", detail: str = "") -> None:
        self._store.add_event(
            Event(at=self._clock(), kind=kind, node=node, sandbox=sandbox, detail=detail)
        )

    def events(self, limit: int = 200) -> list[Event]:
        return self._store.events(limit)

    def _save(self, record: SandboxRecord) -> None:
        self._store.put_sandbox(record, now=self._clock())

    def _set(self, record: SandboxRecord, status: SandboxStatus, reason: str = "") -> None:
        if record.status is status and record.reason == reason:
            return
        record.status = status
        record.reason = reason
        if status.final and record.ended_at is None:
            record.ended_at = self._clock()
        self._save(record)

    # --- Nodes ----------------------------------------------------------------------

    def register(self, declaration: NodeDeclaration) -> None:
        if declaration.protocol != PROTOCOL_VERSION:
            raise FleetError(
                f"{declaration.name} speaks Fleet protocol {declaration.protocol}; "
                f"this control plane speaks {PROTOCOL_VERSION}"
            )
        now = self._clock()
        previous = self.nodes.get(declaration.name)
        if previous is not None and previous.declaration.boot_id != declaration.boot_id:
            for record in self._on_node(declaration.name):
                if record.status.holds_resources or record.status is SandboxStatus.LOST:
                    self._end_on_node(record, "the Node restarted")
            self._event("node.restarted", node=declaration.name)
        elif previous is None:
            self._event("node.registered", node=declaration.name)
        record = NodeRecord(
            declaration=declaration,
            registered_at=now if previous is None else previous.registered_at,
            last_seen=now,
            draining=previous.draining if previous is not None else False,
        )
        self.nodes[declaration.name] = record
        self._store.put_node(record)

    def refuse(self, declaration: NodeDeclaration, reason: str) -> NoReturn:
        """Refuse a Node's registration, recorded as an event, with the reason it is given."""
        self._event("node.refused", node=declaration.name, detail=reason)
        raise FleetError(f"{declaration.name} cannot join the Fleet: {reason}")

    def _on_node(self, name: str) -> list[SandboxRecord]:
        return [record for record in self.sandboxes.values() if record.node == name]

    def _end_on_node(self, record: SandboxRecord, reason: str) -> None:
        """The Node no longer has it: a released Sandbox is done, any other is lost."""
        if record.released or record.status is SandboxStatus.PLACED:
            self._set(record, SandboxStatus.RELEASED, reason)
        else:
            if record.status is not SandboxStatus.LOST:
                self._event(
                    "sandbox.lost", node=record.node or "", sandbox=record.sandbox_id, detail=reason
                )
            self._set(record, SandboxStatus.LOST, reason)

    def heartbeat(self, name: str, beat: Heartbeat) -> HeartbeatReply:
        node = self.nodes.get(name)
        if node is None or node.declaration.boot_id != beat.boot_id:
            return HeartbeatReply(registered=False)
        now = self._clock()
        if not node.online:
            self._event("node.online", node=name)
        node.last_seen = now
        node.online = True
        node.images = beat.images
        node.disk_free_gb = beat.disk_free_gb
        self._store.put_node(node)

        destroy: list[str] = []
        listed = {entry.sandbox_id: entry for entry in beat.roster}
        for entry in beat.roster:
            record = self.sandboxes.get(entry.sandbox_id)
            if record is None:
                first = self._orphans.setdefault((name, entry.sandbox_id), now)
                if now - first >= self.timing.orphan_grace_sec:
                    destroy.append(entry.sandbox_id)
                    self._event("sandbox.orphan", node=name, sandbox=entry.sandbox_id)
                continue
            if record.node != name or record.released or record.status.final:
                destroy.append(entry.sandbox_id)
                continue
            record.lease_expires_at = entry.lease_expires_at
            if entry.create_sec is not None:
                record.create_sec = entry.create_sec
            if entry.state == "ready":
                if record.ready_at is None:
                    record.ready_at = now
                record.status, record.reason = SandboxStatus.READY, ""
            elif entry.state == "creating":
                record.status, record.reason = SandboxStatus.STARTING, ""
            else:
                # A Node destroys a Sandbox only when told to or when its lease ran out.
                record.released = True
                record.status = SandboxStatus.RELEASING
                record.reason = record.reason or "its lease ran out"
            self._save(record)
        self._orphans = {
            key: first
            for key, first in self._orphans.items()
            if key[0] != name or (key[1] in listed and key[1] not in destroy)
        }
        for record in self._on_node(name):
            if record.sandbox_id in listed:
                continue
            if record.status is SandboxStatus.PLACED and not record.released:
                continue  # the Run has not asked the Node yet
            if record.status.holds_resources:
                if record.lease_expires_at is not None and record.lease_expires_at <= now:
                    # The Node destroyed it between two heartbeats, as its lease ran out.
                    record.released = True
                    self._end_on_node(record, "its lease ran out")
                else:
                    self._end_on_node(record, "the Node no longer lists it")
        self.schedule()
        return HeartbeatReply(registered=True, destroy=tuple(destroy), draining=node.draining)

    def drain(self, name: str, draining: bool) -> None:
        node = self.nodes.get(name)
        if node is None:
            raise FleetError(f"no Node named {name}")
        if node.draining != draining:
            node.draining = draining
            self._store.put_node(node)
            self._event("node.draining" if draining else "node.resumed", node=name)
        self.schedule()

    def views(self) -> list[NodeView]:
        holds: dict[str, list] = {}
        startup: dict[str, dict[str, list[float]]] = {}
        for record in self.sandboxes.values():
            if record.node is None:
                continue
            if record.status.holds_resources and record.holds is not None:
                holds.setdefault(record.node, []).append(record.holds)
            if record.create_sec is not None and record.provider is not None:
                startup.setdefault(record.node, {}).setdefault(record.provider, []).append(
                    record.create_sec
                )
        return [
            NodeView(
                declaration=node.declaration,
                online=node.online,
                draining=node.draining,
                images=frozenset(image.key for image in node.images),
                holds=tuple(holds.get(name, ())),
                startup_sec={
                    provider: sum(values[-20:]) / len(values[-20:])
                    for provider, values in startup.get(name, {}).items()
                },
            )
            for name, node in sorted(self.nodes.items())
        ]

    # --- Runs -----------------------------------------------------------------------

    def _prepare_sec(self) -> dict[str, float]:
        """How long the Nodes that hold each image took to prepare it, on average."""
        times: dict[str, list[float]] = {}
        for node in self.nodes.values():
            for image in node.images:
                times.setdefault(image.key, []).append(image.prepare_sec)
        return {key: sum(values) / len(values) for key, values in times.items()}

    def _avoided(self, record: SandboxRecord) -> tuple[str, ...]:
        """The Nodes a Sandbox may not go to: those that refused it, and those its Run cannot
        reach."""
        run = self.runs.get(record.run_id)
        return (*record.refused, *(run.unreachable if run is not None else ()))

    def acquire(self, request: AcquireRequest) -> Placement:
        now = self._clock()
        record = self.sandboxes.get(request.sandbox_id)
        if record is not None:
            if (record.run_id, record.demand, record.episode_id, record.role) != (
                request.run_id,
                request.demand,
                request.episode_id,
                request.role,
            ):
                raise FleetError(f"{request.sandbox_id} was requested with other terms")
            return self.placement(request.sandbox_id)
        if request.run_id not in self.runs:
            run = RunRecord(run_id=request.run_id, created_at=now)
            self.runs[run.run_id] = run
            self._store.put_run(run)
        record = SandboxRecord(
            sandbox_id=request.sandbox_id,
            run_id=request.run_id,
            demand=request.demand,
            episode_id=request.episode_id,
            role=request.role,
            created_at=now,
            seen_at=now,
        )
        self.sandboxes[record.sandbox_id] = record
        remaining = self._views_except(self._avoided(record))
        if (reason := unservable(request.demand, remaining)) is not None:
            self._set(record, SandboxStatus.FAILED, reason)
            self._event("sandbox.failed", sandbox=record.sandbox_id, detail=reason)
        else:
            self._save(record)
            self.schedule()
        return self.placement(record.sandbox_id, touch=False)

    def placement(self, sandbox_id: str, *, touch: bool = True) -> Placement:
        record = self.sandboxes.get(sandbox_id)
        if record is None:
            raise UnknownSandboxError(f"no Sandbox {sandbox_id}")
        if touch and not record.released:
            record.seen_at = self._clock()
        node = self.nodes.get(record.node) if record.node else None
        placed = record.status in {
            SandboxStatus.PLACED,
            SandboxStatus.STARTING,
            SandboxStatus.READY,
        }
        grant = None
        if placed and node is not None and not record.released:
            grant = sign_grant(
                Grant(
                    sandbox_id=record.sandbox_id,
                    run_id=record.run_id,
                    node=node.declaration.name,
                    demand=record.demand,
                    expires_at=self._clock() + self.timing.grant_sec,
                ),
                self._token,
            )
        return Placement(
            sandbox_id=record.sandbox_id,
            status=record.status,
            reason=record.reason,
            node=record.node,
            address=node.declaration.address if node is not None else None,
            grant=grant,
        )

    def refused(self, sandbox_id: str, refusal: RefusedRequest) -> Placement:
        """A Node refused the Sandbox before creating it: queue it again, elsewhere.

        A Node its Run could not reach at all is one that Run reaches for no other Sandbox
        either, so none of the Run's Sandboxes is placed there again.
        """
        record = self._record(sandbox_id)
        run = self.runs.get(record.run_id)
        if refusal.unreachable and run is not None and refusal.node not in run.unreachable:
            run.unreachable = (*run.unreachable, refusal.node)
            self._store.put_run(run)
            self._event(
                "run.unreachable",
                node=refusal.node,
                sandbox=sandbox_id,
                detail=f"{record.run_id} cannot reach it: {refusal.reason}",
            )
        if record.node == refusal.node and record.status is SandboxStatus.PLACED:
            if not refusal.unreachable:
                self._event(
                    "sandbox.refused", node=refusal.node, sandbox=sandbox_id, detail=refusal.reason
                )
            record.refused = (*record.refused, refusal.node)
            record.node = record.provider = record.holds = None
            record.placed_at = None
            why = "its Run cannot reach" if refusal.unreachable else "refused it:"
            self._set(record, SandboxStatus.QUEUED, f"{refusal.node} {why} {refusal.reason}")
            remaining = self._views_except(self._avoided(record))
            if (reason := unservable(record.demand, remaining)) is not None:
                self._set(record, SandboxStatus.FAILED, reason)
            self.schedule()
        return self.placement(sandbox_id)

    def _views_except(self, names: tuple[str, ...]) -> list[NodeView]:
        return [view for view in self.views() if view.name not in names]

    def release(self, sandbox_id: str, *, reason: str = "released by its Run") -> Placement:
        record = self._record(sandbox_id)
        if not record.released and not record.status.final:
            record.released = True
            if record.status.holds_resources:
                self._set(record, SandboxStatus.RELEASING, reason)
            else:
                self._set(record, SandboxStatus.RELEASED, reason)
            self.schedule()
        return self.placement(sandbox_id, touch=False)

    def destroy(self, sandbox_id: str) -> Placement:
        """An operator's forced release: the Node destroys it at its next heartbeat."""
        self._event("admin.destroy", sandbox=sandbox_id)
        return self.release(sandbox_id, reason="destroyed by an operator")

    def cancel_run(self, run_id: str) -> int:
        """Drop every Sandbox a Run still has queued."""
        cancelled = 0
        for record in list(self.sandboxes.values()):
            if record.run_id == run_id and record.status is SandboxStatus.QUEUED:
                self.release(record.sandbox_id, reason="cancelled by an operator")
                cancelled += 1
        if cancelled:
            self._event("admin.cancel", detail=f"{run_id}: {cancelled} queued Sandboxes")
        return cancelled

    def _record(self, sandbox_id: str) -> SandboxRecord:
        record = self.sandboxes.get(sandbox_id)
        if record is None:
            raise UnknownSandboxError(f"no Sandbox {sandbox_id}")
        return record

    # --- time -----------------------------------------------------------------------

    def tick(self) -> None:
        """Mark silent Nodes offline, lapse what nobody asks about, and place what fits."""
        now = self._clock()
        for name, node in self.nodes.items():
            if node.online and now - node.last_seen > self.timing.node_timeout_sec:
                node.online = False
                self._store.put_node(node)
                self._event("node.offline", node=name)
                for record in self._on_node(name):
                    if record.status is SandboxStatus.PLACED and not record.released:
                        record.node = record.provider = record.holds = None
                        record.placed_at = None
                        self._set(record, SandboxStatus.QUEUED, f"{name} went offline")
                    elif record.status.holds_resources:
                        self._end_on_node(record, "the Node went offline")
        for record in list(self.sandboxes.values()):
            idle = now - record.seen_at > self.timing.abandon_sec
            if record.status is SandboxStatus.QUEUED and idle:
                record.released = True
                self._set(record, SandboxStatus.RELEASED, "its Run stopped asking for it")
            elif record.status is SandboxStatus.PLACED and idle and not record.released:
                record.released = True
                self._set(record, SandboxStatus.RELEASING, "its Run stopped asking for it")
            if (record.status.final or record.status is SandboxStatus.LOST) and now - (
                record.ended_at or record.seen_at
            ) > self.timing.history_sec:
                del self.sandboxes[record.sandbox_id]
        self.schedule()

    def schedule(self) -> None:
        """Place queued Sandboxes, one per Run in turn, longest-waiting Run first.

        Each Run's requests are tried in order, and a request that fits nowhere does not
        hold back a later one of the same Run that fits elsewhere.
        """
        queued = sorted(
            (r for r in self.sandboxes.values() if r.status is SandboxStatus.QUEUED),
            key=lambda record: record.created_at,
        )
        if not queued:
            return
        views = {view.name: view for view in self.views()}
        prepare_sec = self._prepare_sec()
        by_run: dict[str, list[SandboxRecord]] = {}
        for record in queued:
            by_run.setdefault(record.run_id, []).append(record)
        progressed = True
        while progressed:
            progressed = False
            for run_id in sorted(by_run, key=lambda run: self.runs[run].last_served_at):
                for record in by_run[run_id]:
                    if record.status is not SandboxStatus.QUEUED:
                        continue
                    choice, reason = choose(
                        record.demand,
                        views.values(),
                        refused=self._avoided(record),
                        prepare_sec=prepare_sec,
                        weights=self.weights,
                    )
                    if choice is None:
                        if record.reason != reason:
                            record.reason = reason
                            self._save(record)
                        continue
                    self._place(record, choice)
                    view = views[choice.node]
                    views[choice.node] = dataclasses.replace(
                        view, holds=(*view.holds, choice.holds)
                    )
                    progressed = True
                    break

    def _place(self, record: SandboxRecord, choice: Choice) -> None:
        now = self._clock()
        record.node = choice.node
        record.provider = choice.profile.provider
        record.holds = choice.holds
        record.placed_at = now
        self._set(record, SandboxStatus.PLACED, f"placed on {choice.node}")
        run = self.runs[record.run_id]
        # Strictly increasing, so two placements in one pass still take turns.
        run.last_served_at = max(now, max(r.last_served_at for r in self.runs.values()) + 1e-6)
        self._store.put_run(run)
        self._event(
            "sandbox.placed",
            node=choice.node,
            sandbox=record.sandbox_id,
            detail=f"score {choice.score:.1f}s",
        )
