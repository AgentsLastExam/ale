# ale-fleet agent guide

Read [`README.md`](README.md) and `docs/specs/fleet.md` before changing the Fleet.

- The control plane places data: it never imports a Provider or the node agent, and knows a
  Sandbox only by its demand, profile and lease, never by its episode.
- The node agent drives the Node's Providers through the Provider contract, and prepares
  images through the engine's own preparation, as a local Run would.
- The engine never imports `ale.fleet`. What a Run needs from the Fleet goes through the
  FleetProvider in `ale-run` and the protocol in `ale.core.fleet`.
- A protocol change raises `PROTOCOL_VERSION` when an older peer would misread it.

Run `just lint && just test`, and `tests/conformance/test_provider_fleet.py` on a machine
with Docker for any change to the node agent, the FleetProvider or the protocol.
