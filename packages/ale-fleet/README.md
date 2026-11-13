# ale-fleet

`ale-fleet` pools the Sandbox capacity of several machines behind one control plane. The
contract is [the Fleet spec](../../docs/specs/fleet.md); [the Fleet guide](../../docs/guides/fleet.md)
sets one up.

It builds on `ale-run`: a node agent lends the engine's own Providers and prepares Task
images with the engine's code. `ale-run` never imports this package; a Run reaches the
Fleet through its FleetProvider (`ale.run.providers.fleet.provider`), and the three speak the
protocol in `ale.core.fleet`.

| Module | Owns |
|---|---|
| `control.py` | the control plane: registration, placement, the queue, leases and reconciliation |
| `placement.py` | scoring Nodes for a demand and assigning a Run's plan |
| `store.py` | the ledger's SQLite file |
| `server.py` | the control plane's HTTP API and the page |
| `status.py`, `page.py` | the status document, and the page rendered from it |
| `node.py` | the node agent: probing, journaling and serving the Node's Sandboxes |
| `images.py` | the images a Node prepares and holds |
| `cli.py` | the `ale-fleet` command |

The control plane modules (all but `node.py`, `images.py` and `cli.py`) never import a
Provider; `just lint` checks that, and that `ale-run` never imports `ale.fleet`.
