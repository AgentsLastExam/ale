"""The Fleet: one control plane that places Sandboxes on many Nodes.

A Run on one machine reaches the Sandbox capacity of many: each Node runs a node agent
that drives the machine's own Providers, and the control plane keeps the ledger of what
every Node holds, places each Sandbox, queues what does not fit and reconciles its records
with what the Nodes report. ``docs/specs/fleet.md`` is the contract.
"""
