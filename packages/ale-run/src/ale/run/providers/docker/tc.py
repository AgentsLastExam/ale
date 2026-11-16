"""tc filters in a Docker sandbox's network namespace, as the network helper applies them.

Kata's microVM keeps its sandbox's packets from the namespace's netfilter, so a Kata sandbox
is confined by tc filters on the devices its frames cross instead. Each filter is a device,
a hook (``ingress`` or ``egress``), a preference, lower running first, and what it matches
and does.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["DROP_ALL", "Filter", "Peers", "delete", "remove", "replace", "show"]

Filter = tuple[str, str, int, str]

#: What drops every frame a seal does not pass. A flower filter with no key rather than
#: matchall, whose filters tc cannot replace in place.
DROP_ALL = "protocol all flower action drop"


@dataclass(frozen=True)
class Peers:
    """A sandbox's addresses on its network, and the Host's there, which the filters that
    pass frames between the sandbox and the Host match exactly: root in a sandbox that can
    write any frame, a VM's or one with raw sockets, gets nothing else past them."""

    host_ip: str
    host_mac: str
    """The bridge's own address, which every frame between the sandbox and the Host is to or
    from."""
    ip: str
    mac: str
    """The sandbox's, which Docker gave its veth and a Kata VM's guest takes (measured)."""

    @property
    def to_host(self) -> str:
        """The flower keys of an IP frame the sandbox sends the Host."""
        return f"src_mac {self.mac} dst_mac {self.host_mac} src_ip {self.ip} dst_ip {self.host_ip}"

    @property
    def arp(self) -> str:
        """The flower keys of the sandbox's own ARP for the Host's address."""
        return f"src_mac {self.mac} arp_sip {self.ip} arp_sha {self.mac} arp_tip {self.host_ip}"

    @property
    def from_host(self) -> str:
        """The flower keys of an IP frame the Host sends the sandbox."""
        return f"src_mac {self.host_mac} dst_mac {self.mac} src_ip {self.host_ip}"

    @property
    def host_arp(self) -> str:
        """The flower keys of the Host's own ARP, whatever it is to: its requests are
        broadcast."""
        return f"src_mac {self.host_mac} arp_sip {self.host_ip} arp_sha {self.host_mac}"


def replace(filters: Iterable[Filter]) -> list[str]:
    """Put them in place, replacing what is at their preferences, so a filter is never
    missing while one that does the same is put back."""
    return [
        f"tc filter replace dev {device} {hook} pref {pref} handle 1 {spec}"
        for device, hook, pref, spec in filters
    ]


def delete(filters: Iterable[Filter]) -> list[str]:
    """Delete them, where they are: idempotent."""
    return [
        f"{{ tc filter del dev {device} {hook} pref {pref} 2>/dev/null || true; }}"
        for device, hook, pref, _ in filters
    ]


def remove(filters: Iterable[Filter]) -> list[str]:
    """Delete them, then check that no drop filter among them is left: a sandbox unsealed
    with one still in place would fail its framework phase for want of a network it was
    promised."""
    filters = list(filters)
    return [
        *delete(filters),
        *(
            f'test -z "$({show(device, hook, pref)})"'
            for device, hook, pref, spec in filters
            if spec.endswith("action drop")
        ),
    ]


def show(device: str, hook: str, pref: int) -> str:
    return f"tc filter show dev {device} {hook} pref {pref}"
