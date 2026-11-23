# orbit-fleet agent guide

Read [`README.md`](README.md) and `docs/specs/fleet.md` before changing the Fleet.

- The control plane places data: it never imports a Provider or the node agent, and knows a
  Sandbox only by its demand, profile and lease, never by its episode.
- The node agent drives the Node's leaves through the Provider contract, and prepares
  images through the engine's own preparation, as a local Run would.
- The engine never imports `orbit.fleet`. What a Run needs from the Fleet goes through the
  FleetProvider in `orbit-run` and the protocol in `orbit.core.fleet`.
- `PROTOCOL_VERSION` stays `0.1.0` while the Fleet is developed, every peer running one
  checkout; once Nodes are deployed and updated on their own, a change an older peer would
  misread raises it.
- Problems peculiar to the Fleet as a Provider, with their decisions, are in
  [`providers/fleet/AGENTS.md`](../orbit-run/src/orbit/run/providers/fleet/AGENTS.md).

Run `just lint && just test`, and `tests/conformance/test_provider_fleet.py` on a machine
with Docker for any change to the node agent, the FleetProvider or the protocol.
