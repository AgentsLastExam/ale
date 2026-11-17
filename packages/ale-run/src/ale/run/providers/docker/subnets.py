"""The subnets of the networks the Docker Provider creates for its Sandboxes.

A Kata or gVisor Sandbox, and a runc one whose network is not ``open``, has a bridge
network of its own while it lives. Docker's default address pools give out about 31
networks, so each of these networks takes a /29 of :data:`BLOCK` instead, which those pools
leave alone, and which no route in the daemon's network namespace overlaps.
"""

from __future__ import annotations

import ipaddress
import json
from ipaddress import IPv4Network

__all__ = ["ATTEMPTS", "BLOCK", "OVERLAPS", "PREFIX", "ROUTES", "free"]

#: Where every network's subnet comes from: a /16 outside Docker's default address pools
#: (``172.17.0.0/16`` to ``172.31.0.0/16`` and ``192.168.0.0/16``) and outside the ranges
#: other tools and clouds take by default: rootlesskit's ``10.0.2.0/24``, Podman's
#: ``10.88.0.0/16`` and up, Kubernetes' ``10.96.0.0/12`` and GCP's ``10.128.0.0/9``.
BLOCK = IPv4Network("10.77.0.0/16")
#: Each network's prefix: the Host's address and five more, for the helper that reads the
#: bridge's MAC and for the sandbox, which join it one after the other.
PREFIX = 29
#: How many subnets, chosen at random, one network's creation tries before it gives up.
ATTEMPTS = 32
#: What Docker answers when a subnet overlaps one of its other networks.
OVERLAPS = "Pool overlaps with other one on this address space"
#: ``ip``'s arguments that print every IPv4 route of every table as JSON.
ROUTES = ("-j", "-4", "route", "show", "table", "all")


def free(routes: str) -> list[IPv4Network]:
    """The /29s of :data:`BLOCK` that no route but a default one overlaps, given the routes
    ``ip`` printed with :data:`ROUTES`."""
    size = 1 << (32 - PREFIX)
    start = int(BLOCK.network_address)
    end = int(BLOCK.broadcast_address)
    taken: set[int] = set()
    for route in json.loads(routes):
        if route["dst"] == "default":
            continue
        routed = ipaddress.ip_network(route["dst"], strict=False)
        first = max(int(routed.network_address), start)
        last = min(int(routed.broadcast_address), end)
        if first <= last:
            taken.update(range((first - start) // size, (last - start) // size + 1))
    return [
        IPv4Network((start + index * size, PREFIX))
        for index in range(BLOCK.num_addresses // size)
        if index not in taken
    ]
