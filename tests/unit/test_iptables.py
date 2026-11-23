"""Orbit's iptables writer against filter and nat tables captured from the kernel and gVisor.

The fixtures in ``tests/fixtures/iptables`` are those tables as IPT_SO_GET_ENTRIES returned
them, captured in November 2026 on Linux 7.0 (runc, iptables-legacy 1.8.13) and on gVisor
release-20261109.0 (runsc, the same iptables built to open a UDP socket):

- ``<kernel|gvisor>-foreign``: rules a Task might add, a user chain jumped to from INPUT and
  OUTPUT with multiport, conntrack, comment and owner matches, FORWARD's policy DROP, a DNAT
  chain and a MASQUERADE;
- ``<kernel|gvisor>-reference``: the same with Orbit's chains of :data:`STATES` ``reference``
  built by iptables;
- ``gvisor-dockerd``: what the dockerd of ``docker:dind``, on iptables-legacy, created in a
  gVisor sandbox run with ``--net-raw`` and ``CAP_NET_ADMIN``, with Orbit's sealed chains set
  by the writer.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.run import iptables as ipt
from orbit.run.providers.docker import gvisor

pytestmark = pytest.mark.unit

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "iptables"
FIXTURES = [
    "kernel-foreign",
    "kernel-reference",
    "gvisor-foreign",
    "gvisor-reference",
    "gvisor-dockerd",
]
HOST = "172.17.0.1"
_EPISODE = {"dst": HOST, "proto": "tcp", "dports": [27412, 27413], "target": "ACCEPT"}
_GATEWAY = {
    "chains": {
        "ORBIT_GATEWAY": [
            {"dst": HOST, "proto": "tcp", "dports": [7412], "target": {"DNAT": f"{HOST}:27412"}}
        ]
    },
    "jumps": {"OUTPUT": ["ORBIT_GATEWAY"]},
}
_HOST = [
    _EPISODE,
    {"dst": HOST, "proto": "tcp", "target": "REJECT_TCP_RESET"},
    {"dst": HOST, "target": "REJECT"},
]
_SEAL = [
    {"out": "lo", "target": "ACCEPT"},
    _EPISODE,
    {"proto": "tcp", "target": "REJECT_TCP_RESET"},
    {"target": "REJECT"},
]
_INPUT = [
    {"in": "lo", "target": "ACCEPT"},
    {"ctstate": ["ESTABLISHED", "RELATED"], "target": "ACCEPT"},
    {"target": "DROP"},
]
#: A chain of the reference's that no state lists, which the writer removes.
_UDP = [
    {"dst": HOST, "proto": "udp", "dports": [53], "target": "ACCEPT"},
    {"proto": "udp", "dports": [53, 5353], "target": "DROP"},
    {"ctstate": ["NEW", "INVALID"], "target": "DROP"},
]
_JUMPS = {"OUTPUT": ["ORBIT_HOST", "ORBIT_SEAL"], "INPUT": ["ORBIT_INPUT"]}


def _filter(chains: dict[str, list[dict[str, object]]], jumps: dict[str, list[str]]) -> dict:
    return {"chains": chains, "jumps": jumps}


STATES = {
    name: json.dumps({"tables": tables})
    for name, tables in {
        "sealed": {
            "nat": _GATEWAY,
            "filter": _filter(
                {"ORBIT_HOST": _HOST, "ORBIT_SEAL": _SEAL, "ORBIT_INPUT": _INPUT}, _JUMPS
            ),
        },
        "open": {
            "nat": _GATEWAY,
            "filter": _filter({"ORBIT_HOST": _HOST, "ORBIT_SEAL": [], "ORBIT_INPUT": []}, _JUMPS),
        },
        "host-only": {
            "nat": _GATEWAY,
            "filter": _filter({"ORBIT_HOST": _HOST}, {"OUTPUT": ["ORBIT_HOST"]}),
        },
        "none": {"filter": {}, "nat": {}},
        "reference": {
            "nat": _GATEWAY,
            "filter": _filter(
                {
                    "ORBIT_HOST": _HOST,
                    "ORBIT_SEAL": _SEAL,
                    "ORBIT_INPUT": _INPUT,
                    "ORBIT_UDP": _UDP,
                },
                _JUMPS,
            ),
        },
    }.items()
}


def load(fixture):
    """{table: (valid, hook_entry, underflow, entries, count)} of a fixture."""
    tables = {}
    for name, t in json.loads((FIXTURES_DIR / f"{fixture}.json").read_text()).items():
        tables[name] = (
            t["valid_hooks"],
            tuple(t["hook_entry"]),
            tuple(t["underflow"]),
            bytes.fromhex(t["entries"]),
            t["num_entries"],
        )
    return tables


def split(blob):
    out, offset = [], 0
    while offset < len(blob):
        size = struct.unpack_from("=H", blob, offset + 90)[0]
        out.append((offset, blob[offset : offset + size]))
        offset += size
    assert offset == len(blob)
    return out


def check(valid, hooks, under, blob, count):
    """The checks the kernel's translate_table makes, and that the table is laid out as
    iptables lays it out (built-in chains, user chains each ending in RETURN, ERROR
    last)."""
    entries = split(blob)
    assert len(entries) == count
    starts = {offset: i for i, (offset, _) in enumerate(entries)}
    for offset, e in entries:
        assert offset % 8 == 0 and len(e) % 8 == 0
        target_offset, next_offset = struct.unpack_from("=HH", e, 88)
        assert target_offset >= 112 and target_offset + 32 <= next_offset == len(e)
        m = 112
        while m < target_offset:  # matches tile the space up to the target
            size = struct.unpack_from("=H", e, m)[0]
            assert size >= 32 and size % 8 == 0
            m += size
        assert m == target_offset
        name, value = ipt._target(e)
        target_size = struct.unpack_from("=H", e, target_offset)[0]
        assert target_offset + target_size <= next_offset
        if name == b"":
            assert target_size == 40 and next_offset == target_offset + 40
            if value >= 0:  # a jump lands on the first rule of a user chain
                assert value in starts and ipt._target(entries[starts[value] - 1][1])[0] == b"ERROR"
            else:
                assert value in (ipt.ACCEPT, ipt.DROP, ipt.RETURN)
        elif name == b"ERROR":
            assert target_size == 64
    assert ipt._target(entries[-1][1]) == (b"ERROR", "ERROR")
    for h in range(5):
        if valid >> h & 1:
            assert hooks[h] in starts and under[h] in starts and hooks[h] <= under[h]
            policy = entries[starts[under[h]]][1]
            assert policy[:84] == bytes(84) and ipt._target(policy)[1] in (ipt.ACCEPT, ipt.DROP)
    return entries


def decode(valid, hooks, under, blob, count):
    """{chain: (policy, [rule])}, each rule its entry with comefrom and counters zeroed and
    a jump's verdict replaced by the name of the chain it lands in."""
    entries = check(valid, hooks, under, blob, count)
    heads = {}  # offset of a user chain's first rule: its name
    for i, (_, e) in enumerate(entries[:-1]):
        name, value = ipt._target(e)
        if name == b"ERROR":
            heads[entries[i + 1][0]] = value

    def norm(e):
        e = e[:92] + bytes(20) + e[112:]
        name, value = ipt._target(e)
        if name == b"" and value >= 0:
            at = struct.unpack_from("=H", e, 88)[0] + 32
            return e[:at] + bytes(4) + e[at + 4 :], heads[value]
        return e, None

    chains, starts = {}, {offset: i for i, (offset, _) in enumerate(entries)}
    for h, hook in enumerate(ipt.HOOKS):
        if valid >> h & 1:
            rules = [norm(e) for _, e in entries[starts[hooks[h]] : starts[under[h]]]]
            chains[hook] = (norm(entries[starts[under[h]]][1]), rules)
    for i, (_, e) in enumerate(entries[:-1]):
        name, value = ipt._target(e)
        if name == b"ERROR":
            end = next(
                j for j in range(i + 1, len(entries)) if ipt._target(entries[j][1])[0] == b"ERROR"
            )
            body = entries[i + 1 : end]
            assert ipt._target(body[-1][1]) == (b"", ipt.RETURN) and body[-1][1][:84] == bytes(84)
            chains[value] = (None, [norm(e) for _, e in body[:-1]])
    return chains


def foreign(chains):
    """The chains and rules Orbit does not own."""
    return {
        name: (policy, [r for r in rules if not (r[1] or "").startswith("ORBIT_")])
        for name, (policy, rules) in chains.items()
        if not name.startswith("ORBIT_")
    }


def apply(table, state, name):
    valid, hooks, under, blob, _ = table
    chains, jumps = ipt.plan(STATES[state]).get(name, ({}, {}))
    new_hooks, new_under, new_blob, count = ipt.rewrite(valid, hooks, under, blob, chains, jumps)
    return valid, tuple(new_hooks), tuple(new_under), new_blob, count


def expected_chains(state, name):
    chains, jumps = ipt.plan(STATES[state]).get(name, ({}, {}))
    return {
        c: [(e[:92] + bytes(20) + e[112:], None) for e in rules] for c, rules in chains.items()
    }, jumps


def test_fixtures_are_valid():
    for fixture in FIXTURES:
        for table in load(fixture).values():
            decode(*table)


def test_rule_bytes_match_iptables_on_the_kernel():
    """Every rule shape serializes byte for byte as iptables-legacy 1.8.13 sets it."""
    for name, table in load("kernel-reference").items():
        chains = decode(*table)
        want, _ = expected_chains("reference", name)
        for chain, rules in want.items():
            assert chains[chain][1] == rules, (name, chain)


def test_netstack_reads_some_rules_back_differently():
    """gVisor reads multiport (and owner) back as revision 0 and DNAT revision 2 with the
    size of revision 1: those entries differ from what was set, everything else matches."""
    differ = []
    for name, table in load("gvisor-reference").items():
        chains = decode(*table)
        want, _ = expected_chains("reference", name)
        for chain, rules in want.items():
            assert len(chains[chain][1]) == len(rules)
            for (got, _), (sent, _) in zip(chains[chain][1], rules, strict=True):
                if got != sent:
                    diff = [i for i in range(len(sent)) if got[i] != sent[i]]
                    differ.append((chain, len(got) == len(sent), diff))
    multiport_revision = [ipt.ENTRY.size + 31]
    dnat_target_size = [ipt.ENTRY.size + 48]  # target_size 72 for an 80-byte target
    assert sorted(differ) == [
        ("ORBIT_GATEWAY", True, dnat_target_size),
        ("ORBIT_HOST", True, multiport_revision),
        ("ORBIT_SEAL", True, multiport_revision),
        ("ORBIT_UDP", True, multiport_revision),
    ], differ


def test_apply_sets_chains_and_jumps_and_preserves_the_rest():
    for fixture in FIXTURES:
        for state in ("sealed", "open", "host-only", "none"):
            for name, table in load(fixture).items():
                before, after = decode(*table), decode(*apply(table, state, name))
                assert foreign(after) == foreign(before), (fixture, state, name)
                want, jumps = expected_chains(state, name)
                assert {c: r for c, (_, r) in after.items() if c.startswith("ORBIT_")} == want
                # jumps first, in order, and only those
                for hook, (_, rules) in after.items():
                    listed = jumps.get(hook, [])
                    assert [r[1] for r in rules[: len(listed)]] == listed, (fixture, state, hook)
                    assert not any((r[1] or "").startswith("ORBIT_") for r in rules[len(listed) :])


def test_idempotent():
    for fixture in FIXTURES:
        for state in ("sealed", "open", "host-only", "none"):
            for name, table in load(fixture).items():
                once = apply(table, state, name)
                assert apply(once, state, name) == once, (fixture, state, name)


def test_toggle_seal_open_seal():
    for fixture in FIXTURES:
        for name, table in load(fixture).items():
            sealed = apply(table, "sealed", name)
            opened = apply(sealed, "open", name)
            assert apply(opened, "sealed", name) == sealed
            chains = decode(*opened)
            if name == "filter":
                assert chains["ORBIT_SEAL"][1] == [] and chains["ORBIT_INPUT"][1] == []
                assert [r[1] for r in chains["OUTPUT"][1][:2]] == ["ORBIT_HOST", "ORBIT_SEAL"]


def test_removing_everything_restores_the_original_bytes():
    for fixture in ("kernel-foreign", "gvisor-foreign"):
        for name, table in load(fixture).items():
            for state in ("sealed", "open", "host-only"):
                assert apply(apply(table, state, name), "none", name) == table, (fixture, state)


def test_stale_orbit_chains_and_their_jumps_are_removed():
    """iptables-built Orbit chains (ORBIT_UDP among them) give way to the listed ones only, and
    the result equals setting the same state on the table that never had them."""
    for kind in ("kernel", "gvisor"):
        for name, table in load(f"{kind}-reference").items():
            got = decode(*apply(table, "host-only", name))
            assert {c for c in got if c.startswith("ORBIT_")} == {
                "ORBIT_HOST" if name == "filter" else "ORBIT_GATEWAY"
            }
            clean = decode(*apply(load(f"{kind}-foreign")[name], "host-only", name))
            if kind == "kernel":
                assert got == clean
            else:  # the foreign rules are the same; Orbit's own differ only where netstack's
                assert foreign(got) == foreign(clean)  # read-back of multiport/DNAT does


def test_offsets_are_relocated():
    """Jumps inserted at the top of INPUT and OUTPUT move everything after them; Orbit's
    chains go last, so foreign chains keep their place relative to the built-ins."""
    _, hooks, under, blob, count = load("kernel-foreign")["filter"]
    _, new_hooks, new_under, new_blob, new_count = apply(
        load("kernel-foreign")["filter"], "sealed", "filter"
    )
    jump = 152
    # INPUT, FORWARD, OUTPUT
    assert new_hooks[1:4] == (hooks[1], hooks[2] + jump, hooks[3] + jump)
    assert new_under[1:4] == (under[1] + jump, under[2] + jump, under[3] + 3 * jump)
    old = {ipt._target(e)[1]: o for o, e in split(blob) if ipt._target(e)[0] == b"ERROR"}
    new = {ipt._target(e)[1]: o for o, e in split(new_blob) if ipt._target(e)[0] == b"ERROR"}
    assert new["FOREIGN"] == old["FOREIGN"] + 3 * jump
    # the foreign jump OUTPUT -> FOREIGN now carries the moved offset
    verdicts = [ipt._target(e)[1] for o, e in split(new_blob) if new_hooks[3] <= o < new_under[3]]
    first_rule = new["FOREIGN"] + 176
    assert verdicts[2] == first_rule and verdicts[:2] == [
        new["ORBIT_HOST"] + 176,
        new["ORBIT_SEAL"] + 176,
    ]
    chains, _ = ipt.plan(STATES["sealed"])["filter"]
    assert new_count == count + 3 + sum(2 + len(rules) for rules in chains.values())
    added = sum(176 + sum(map(len, rules)) + 152 for rules in chains.values())
    assert (
        len(new_blob) == len(blob) + 3 * jump + added
        and new["ERROR"] == old["ERROR"] + 3 * jump + added
    )


#: The states the Provider sets in a gVisor sandbox's netstack, whose filter jumps include
#: FORWARD's: sealed, open, and open with only the Gateway routed.
NETSTACK = {
    "sealed": gvisor.netstack(
        HOST, gateway_port="27412", ports=["27412"], isolated=True, sealed=True
    ),
    "open": gvisor.netstack(
        HOST, gateway_port="27412", ports=["27412"], isolated=True, sealed=False
    ),
    "routed": gvisor.netstack(HOST, gateway_port="27412", ports=[], isolated=False, sealed=False),
}


def _netstack(table, state, name):
    valid, hooks, under, blob, _ = table
    chains, jumps = ipt.plan(json.dumps(NETSTACK[state])).get(name, ({}, {}))
    new_hooks, new_under, new_blob, count = ipt.rewrite(valid, hooks, under, blob, chains, jumps)
    return valid, tuple(new_hooks), tuple(new_under), new_blob, count


def test_the_providers_netstack_states_keep_every_rule_and_lead_each_hook_they_jump_from():
    """FORWARD's jumps among them, in each fixture, FORWARD's policy DROP and dockerd's own
    FORWARD chains included; setting a state again changes nothing, and removing Orbit's
    chains leaves the table as it was."""
    assert NETSTACK["sealed"]["tables"]["filter"]["jumps"]["FORWARD"] == [
        "ORBIT_HOST",
        "ORBIT_SEAL",
    ]
    for fixture in FIXTURES:
        for state in NETSTACK:
            for name, table in load(fixture).items():
                set_ = _netstack(table, state, name)
                before, after = decode(*table), decode(*set_)
                assert foreign(after) == foreign(before), (fixture, state, name)
                _, jumps = ipt.plan(json.dumps(NETSTACK[state])).get(name, ({}, {}))
                for hook, (_, rules) in after.items():
                    listed = jumps.get(hook, [])
                    assert [r[1] for r in rules[: len(listed)]] == listed, (fixture, state, hook)
                    assert not any((r[1] or "").startswith("ORBIT_") for r in rules[len(listed) :])
                assert _netstack(set_, state, name) == set_, (fixture, state, name)
                if fixture.endswith("-foreign"):
                    assert apply(set_, "none", name) == table, (fixture, state, name)


def test_rejects_states_outside_the_vocabulary():
    def bad(state):
        try:
            ipt.plan(json.dumps({"tables": state}))
        except ValueError:
            return True
        return False

    def rule(**r):
        return {"filter": {"chains": {"ORBIT_X": [r]}}}

    assert not bad(rule(target="ACCEPT"))
    assert bad(rule(target="ACCEPT", dport=80))  # a misspelt key must not widen a rule
    assert bad(rule(proto="icmp", target="ACCEPT"))
    assert bad(rule(dports=[80], target="ACCEPT"))  # ports need a proto
    # multiport takes 15
    assert bad(rule(proto="tcp", dports=list(range(1, 17)), target="ACCEPT"))
    assert bad(rule(proto="tcp", dports=[0], target="ACCEPT"))
    assert bad(rule(proto="tcp", dports=[True], target="ACCEPT"))
    assert bad(rule(target="REJECT_TCP_RESET"))  # tcp-reset needs proto tcp
    assert bad(rule(proto="tcp", target={"DNAT": "1.2.3.4:80"}))  # DNAT is nat-only
    # REJECT is filter-only
    assert bad({"nat": {"chains": {"ORBIT_X": [{"target": "REJECT"}]}}})
    assert bad(
        {"nat": {"chains": {"ORBIT_X": [{"target": {"DNAT": "1.2.3.4:80"}}]}}}
    )  # needs a proto
    assert bad({"nat": {"chains": {"ORBIT_X": [{"proto": "tcp", "target": {"DNAT": "1.2.3.4"}}]}}})
    assert bad(rule(dst="1.2.3", target="ACCEPT"))
    assert bad(rule(out="a" * 16, target="ACCEPT"))
    assert bad(rule(ctstate=["ESTABLISHED", "BOGUS"], target="ACCEPT"))
    assert bad(rule(target="RETURN"))
    assert bad({"filter": {"chains": {"DOCKER": []}}})  # only ORBIT_ chains are Orbit's
    assert bad({"filter": {"chains": {"ORBIT_X": []}, "jumps": {"OUTPUT": ["ORBIT_Y"]}}})
    assert bad({"filter": {"chains": {"ORBIT_X": []}, "jumps": {"POSTROUTING": ["ORBIT_X"]}}})
    assert bad({"filter": {"chains": {"ORBIT_X": []}, "jumps": {"OUTPUT": "ORBIT_X"}}})
    assert bad({"mangle": {}})
    assert bad({"filter": {"rules": []}})
    assert bad([])


def test_the_writer_runs_alone_and_refuses_a_state_outside_its_vocabulary():
    """It is run as source on the framework's Python, isolated from the engine, and touches
    no table for a state it does not understand."""
    source = Path(ipt.__file__).read_text()
    refused = subprocess.run(
        [sys.executable, "-I", "-c", source, json.dumps({"tables": {"mangle": {}}})],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode == 1
    assert "not an iptables state: unknown table or keys: mangle" in refused.stderr
