"""Set Orbit's iptables chains in a Linux Sandbox, keeping every other rule in the table.

The engine runs this source as root with CAP_NET_ADMIN on the framework's Python
(``python3 -I -c``) with one JSON argument, such as ``{"tables": {"nat": {"chains":
{"ORBIT_GATEWAY": [rule, ...]}, "jumps": {"OUTPUT": ["ORBIT_GATEWAY"]}}, "filter":
{...}}}``. In each table named, each ORBIT_ chain listed gets exactly its rules, other
ORBIT_ chains and jumps into them go, and each built-in chain under "jumps" starts with
jumps to the chains listed. A table is read with IPT_SO_GET_INFO and IPT_SO_GET_ENTRIES and
replaced with one IPT_SO_SET_REPLACE on a UDP socket: gVisor's netstack checks only
CAP_NET_ADMIN, and runsc gives an exec the CAP_NET_RAW a raw socket needs only under
--net-raw. Entries Orbit does not own are copied as opaque bytes, since netstack reads some
back unlike they were set and a kernel table can hold extensions unknown here; only offsets
into the table move. Rules are built as iptables 1.8 builds them. It uses only the standard
library and imports no engine code, so it runs in any Sandbox the framework is in, which
ships no iptables.
"""

import ctypes
import errno
import fcntl
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Sequence
from typing import Any

#: The nat chain that sends a Docker sandbox's fixed Gateway port to the port the Host
#: serves the Gateway on: in a runc sandbox's network namespace, where the network helper
#: sets it, and in a gVisor sandbox's netstack, where this script does.
GATEWAY_CHAIN = "ORBIT_GATEWAY"

#: A table's Orbit chains, each name with its entries, and the jumps each built-in chain
#: starts with, as :func:`plan` makes them.
Chains = dict[str, list[bytes]]
Jumps = dict[str, list[str]]

HOOKS = ("PREROUTING", "INPUT", "FORWARD", "OUTPUT", "POSTROUTING")
TABLES = {  # Orbit's tables and their built-in chains
    "filter": {"INPUT", "FORWARD", "OUTPUT"},
    "nat": {"PREROUTING", "INPUT", "OUTPUT", "POSTROUTING"},
}
#: struct ipt_entry: ipt_ip (src, dst, smsk, dmsk, iniface, outiface, their masks, proto,
#: flags, invflags), nfcache, target_offset, next_offset, comefrom, counters.
ENTRY = struct.Struct("=4s4s4s4s16s16s16s16sHBBIHHI16x")
GETINFO = struct.Struct("=32sI5I5III")  # struct ipt_getinfo
#: struct ipt_replace, ending in the counters pointer.
REPLACE = struct.Struct("=32sIII5I5IIQ")
IPT_SO_GET_INFO = IPT_SO_SET_REPLACE = 64
IPT_SO_GET_ENTRIES = 65
ACCEPT, DROP, RETURN = -2, -1, -5  # standard verdicts; one >= 0 is the offset of a jump
CHAIN = re.compile(r"ORBIT_[A-Z0-9_]{1,24}")
INTERFACE = re.compile(r"[A-Za-z0-9_.-]{1,15}")
RULE = {"dst", "proto", "dports", "out", "in", "ctstate", "target"}
PROTOS = {"tcp": 6, "udp": 17}
CTSTATES = {"INVALID": 1, "ESTABLISHED": 2, "RELATED": 4, "NEW": 8}

#: The C library, loaded only when the writer runs: the engine imports this module for
#: :func:`plan` and :func:`rule` on Hosts whose sockets these are not.
_libc: ctypes.CDLL


def _extension(name: bytes, revision: int, data: bytes) -> bytes:
    """An xt_entry_match or xt_entry_target: size, name, revision, data padded to 8
    bytes."""
    data += bytes(-len(data) % 8)
    return struct.pack("=H29sB", 32 + len(data), name, revision) + data


def _entry(
    target: bytes,
    matches: bytes = b"",
    dst: bytes | None = None,
    proto: int = 0,
    iniface: str = "",
    outiface: str = "",
) -> bytes:
    def mask(name: str) -> bytes:  # the name and its terminating NUL
        return b"\xff" * (len(name) + 1) if name else b""

    ip = (bytes(4), dst or bytes(4), bytes(4), b"\xff" * 4 if dst else bytes(4))
    names = (iniface.encode(), outiface.encode(), mask(iniface), mask(outiface))
    start = ENTRY.size + len(matches)
    head = ENTRY.pack(*ip, *names, proto, 0, 0, 0, start, start + len(target), 0)
    return head + matches + target


def _standard(verdict: int) -> bytes:
    return _entry(_extension(b"", 0, struct.pack("=i", verdict)))


def _head(chain: str) -> bytes:
    """The ERROR entry that starts and names a user chain."""
    return _entry(_extension(b"ERROR", 0, chain.encode().ljust(30, b"\0")))


def _port(value: object) -> int:
    if type(value) is not int or not 0 < value < 65536:
        raise ValueError(f"not a port: {value!r}")
    return value


def rule(spec: dict[str, Any], table: str) -> bytes:
    """The table entry of a rule: optional "dst", "proto" (tcp, udp), "dports" (one port is
    a tcp/udp match, more a multiport match), "out" and "in" (interfaces) and "ctstate", and
    a "target": ACCEPT, DROP, REJECT, REJECT_TCP_RESET or {"DNAT": "a.b.c.d:port"}."""
    if not isinstance(spec, dict) or not set(spec) <= RULE:
        raise ValueError(f"not a rule: {spec!r}")
    proto, matches = spec.get("proto"), b""
    if proto is not None and proto not in PROTOS:
        raise ValueError(f"unknown proto {proto!r}")
    if any(not INTERFACE.fullmatch(spec[key]) for key in ("in", "out") if key in spec):
        raise ValueError(f"not an interface in {spec!r}")
    if "dports" in spec:
        ports = [_port(port) for port in spec["dports"]]
        if not proto or not 0 < len(ports) <= 15:
            raise ValueError(f"dports needs a proto and 1 to 15 ports: {spec!r}")
        if len(ports) == 1:  # xt_tcp / xt_udp: source and destination port ranges
            matches += _extension(proto.encode(), 0, struct.pack("=4H4x", 0, 65535, *ports * 2))
        else:  # xt_multiport_v1: destination, count, ports, per-port flags, invert
            data = struct.pack("=BB15H16x", 1, len(ports), *ports, *[0] * (15 - len(ports)))
            matches += _extension(b"multiport", 1, data)
    if "ctstate" in spec:
        states = spec["ctstate"]
        if not isinstance(states, list) or not states or not set(states) <= set(CTSTATES):
            raise ValueError(f"unknown ctstate {states!r}")
        # xt_conntrack_mtinfo3: addresses, expiry, protocol and ports unused, then
        # match_flags (XT_CONNTRACK_STATE), invert_flags, state_mask and more unused fields.
        mask = sum(CTSTATES[state] for state in set(states))
        matches += _extension(b"conntrack", 3, bytes(146) + struct.pack("=HHH10x", 1, 0, mask))
    target = spec.get("target")
    if target in ("ACCEPT", "DROP"):
        verdict = _extension(b"", 0, struct.pack("=i", ACCEPT if target == "ACCEPT" else DROP))
    elif target in ("REJECT", "REJECT_TCP_RESET") and table == "filter":
        if target == "REJECT_TCP_RESET" and proto != "tcp":
            raise ValueError(f"REJECT_TCP_RESET needs proto tcp: {spec!r}")
        # IPT_TCP_RESET or IPT_ICMP_PORT_UNREACHABLE.
        code = 7 if target == "REJECT_TCP_RESET" else 3
        verdict = _extension(b"REJECT", 0, struct.pack("=I", code))  # ipt_reject_info
    elif isinstance(target, dict) and set(target) == {"DNAT"} and table == "nat" and proto:
        host, _, port = str(target["DNAT"]).rpartition(":")
        address, port = ipaddress.IPv4Address(host).packed, _port(int(port))
        # nf_nat_range2: flags (MAP_IPS | PROTO_SPECIFIED), addresses, ports (big-endian)
        data = struct.pack("=I16s16s", 3, address, address) + struct.pack(">HH4x", port, port)
        verdict = _extension(b"DNAT", 2, data)
    else:
        raise ValueError(f"target {target!r} is not one for the {table} table: {spec!r}")
    dst = None if spec.get("dst") is None else ipaddress.IPv4Address(str(spec["dst"])).packed
    return _entry(
        verdict, matches, dst, PROTOS.get(proto, 0), spec.get("in", ""), spec.get("out", "")
    )


def _target(entry: bytes) -> tuple[bytes, Any]:
    """An entry's target name, with a standard target's verdict or an ERROR's chain name."""
    at = struct.unpack_from("=H", entry, 88)[0]
    name = entry[at + 2 : at + 31].split(b"\0")[0]
    if name == b"":
        return name, struct.unpack_from("=i", entry, at + 32)[0]
    if name == b"ERROR":
        return name, entry[at + 32 : at + 62].split(b"\0")[0].decode()
    return name, None


def rewrite(
    valid: int,
    hook_entry: Sequence[int],
    underflow: Sequence[int],
    table: bytes,
    chains: Chains,
    jumps: Jumps,
) -> tuple[list[int], list[int], bytes, int]:
    """The hook entries, underflows, entries and entry count of ``table``, as
    IPT_SO_GET_ENTRIES returned it, with Orbit's ``chains`` (name: entries) and
    ``jumps``."""
    old: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(table):
        size = struct.unpack_from("=H", table, offset + 90)[0]  # next_offset
        old.append((offset, table[offset : offset + size]))
        offset += size
    if not old or _target(old[-1][1]) != (b"ERROR", "ERROR"):
        raise ValueError("the table does not end with its ERROR entry")
    # Each Orbit chain, from the ERROR entry naming it to the next.
    spans: list[range] = []
    start: int | None = None
    for offset, entry in old:
        name, value = _target(entry)
        if name == b"ERROR":
            spans += [range(start, offset)] if start is not None else []
            start = offset if value.startswith("ORBIT_") else None

    def owned(offset: int) -> bool:
        return any(offset in span for span in spans)

    # The new entries in order, each with what it jumps to: an old offset or an Orbit chain.
    out: list[tuple[bytes, int | str | None]] = []
    moved: dict[int, int] = {}
    firsts: dict[str, int] = {}
    hooks, size = list(hook_entry), 0
    for offset, entry in old:
        for hook, name in enumerate(HOOKS):
            if valid >> hook & 1 and hook_entry[hook] == offset:
                hooks[hook] = size
                out += [(_standard(0), chain) for chain in jumps.get(name, ())]
                size += len(_standard(0)) * len(jumps.get(name, ()))
        if offset == old[-1][0]:  # Orbit's chains go last, before the table's closing entry
            for chain, rules in chains.items():
                firsts[chain] = size + len(_head(chain))
                block = [_head(chain), *rules, _standard(RETURN)]
                out += [(new, None) for new in block]
                size += sum(map(len, block))
        moved[offset] = size  # where a jump to this old entry lands now
        name, value = _target(entry)
        jump = value if name == b"" and value >= 0 else None
        if not owned(offset) and not (jump is not None and owned(jump)):
            out.append((entry, jump))
            size += len(entry)
    entries = []
    for entry, jump in out:
        if jump is not None:
            if jump not in moved and jump not in firsts:
                raise ValueError(f"a rule jumps to offset {jump}, where no entry starts")
            at = struct.unpack_from("=H", entry, 88)[0] + 32
            verdict = firsts[jump] if isinstance(jump, str) else moved[jump]
            entry = entry[:at] + struct.pack("=i", verdict) + entry[at + 4 :]
        entries.append(entry)
    under = [moved[u] if valid >> hook & 1 else u for hook, u in enumerate(underflow)]
    return hooks, under, b"".join(entries), len(entries)


def _get(sock: socket.socket, option: int, data: bytes, size: int) -> bytes:
    buffer, length = ctypes.create_string_buffer(data, size), ctypes.c_uint32(size)
    if _libc.getsockopt(sock.fileno(), socket.IPPROTO_IP, option, buffer, ctypes.byref(length)):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return buffer.raw[: length.value]


def apply(sock: socket.socket, table: str, chains: Chains, jumps: Jumps) -> None:
    """Read ``table``, set Orbit's chains in it and replace it, again if it changed
    meanwhile."""
    name = table.encode()
    for attempt in range(5):
        try:
            _, valid, *info = GETINFO.unpack(_get(sock, IPT_SO_GET_INFO, name, GETINFO.size))
            hook_entry, underflow, (count, size) = info[:5], info[5:10], info[10:]
            # struct ipt_get_entries, then the entries.
            query = struct.pack("=32sI", name, size)
            entries = _get(sock, IPT_SO_GET_ENTRIES, query, 40 + size)[40:]
            hooks, under, new, number = rewrite(
                valid, hook_entry, underflow, entries, chains, jumps
            )
            # Gets the old table's counters.
            counters = ctypes.create_string_buffer(16 * count)
            pointer = ctypes.addressof(counters)
            head = REPLACE.pack(name, valid, number, len(new), *hooks, *under, count, pointer)
            sock.setsockopt(socket.IPPROTO_IP, IPT_SO_SET_REPLACE, head + new)
            return
        except OSError as error:  # EAGAIN: the kernel's table changed since it was read
            if error.errno != errno.EAGAIN or attempt == 4:
                raise


def _lock() -> None:
    """Hold the xtables lock, as iptables does, so that no iptables in the Sandbox (a
    dockerd's) replaces the table between this read and write: netstack would not notice."""
    path = os.environ.get("XTABLES_LOCKFILE", "/run/xtables.lock")
    try:
        lock = os.open(path, os.O_CREAT | os.O_NONBLOCK, 0o600)
    except OSError:
        return  # no iptables can hold it either
    for _ in range(100):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return  # held until this process exits
        except BlockingIOError:
            time.sleep(0.1)
    sys.exit(f"could not take the xtables lock {path}: it has been held for 10 s")


def plan(argument: str) -> dict[str, tuple[Chains, Jumps]]:
    """Each table's Orbit chains (name: entries) and jumps, from the JSON state."""
    try:
        plans: dict[str, tuple[Chains, Jumps]] = {}
        for table, spec in json.loads(argument)["tables"].items():
            if table not in TABLES or not set(spec) <= {"chains", "jumps"}:
                raise ValueError(f"unknown table or keys: {table} {sorted(spec)}")
            chains, jumps = spec.get("chains", {}), spec.get("jumps", {})
            if not set(jumps) <= TABLES[table]:
                raise ValueError(f"no such built-in chain in {table}: {sorted(jumps)}")
            if not all(CHAIN.fullmatch(chain) for chain in chains):
                raise ValueError(f"Orbit's chains are named ORBIT_[A-Z0-9_]: {sorted(chains)}")
            if not all(isinstance(to, list) and set(to) <= set(chains) for to in jumps.values()):
                raise ValueError(f"jumps to chains not listed in {table}: {jumps!r}")
            plans[table] = (
                {c: [rule(r, table) for r in rules] for c, rules in chains.items()},
                jumps,
            )
        return plans
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"malformed: {error!r}") from None


def main(argument: str) -> None:
    global _libc
    try:
        plans = plan(argument)
    except ValueError as error:
        sys.exit(f"not an iptables state: {error}")
    _libc = ctypes.CDLL(None, use_errno=True)
    _lock()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for table, (chains, jumps) in plans.items():
            try:
                apply(sock, table, chains, jumps)
            except (OSError, ValueError) as error:
                sys.exit(f"could not set the {table} table: {error}")


if __name__ == "__main__":
    main(sys.argv[1])
