"""Model gateway: controlled egress for evaluated-agent model traffic.

Its only interface to a sandbox is a reachable base URL plus a per-episode bearer
token. It must never import a provider (Constitution IV): wiring the route into a
sandbox is the provider's plumbing job.
"""
