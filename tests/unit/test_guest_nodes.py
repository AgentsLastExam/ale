"""ssh between the nodes of a Sandbox of several (guestd/nodes.py), without a Sandbox: what a
node's sshd is configured with and what ``join`` leaves on a node."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

import pytest

from orbit.run.guestd import nodes

KEYS = {
    "host": ["HOST PRIVATE\n", "ssh-ed25519 AAAAhost orbit-host"],
    "root": ["ROOT PRIVATE\n", "ssh-ed25519 AAAAroot orbit-root"],
    "account": ["AGENT PRIVATE\n", "ssh-ed25519 AAAAagent orbit-account"],
}


def test_a_session_gets_the_containers_variables_then_the_nodes():
    container = {
        "PATH": "/opt/conda/bin:/usr/bin",
        "TASK_TOKEN": "t",
        "HOME": "/root",
        "HOSTNAME": "node0",
        "SSH_AUTH_SOCK": "/tmp/a",
        "MULTI": "one\ntwo",
        "ORBIT_NODE_RANK": "0",
    }
    given = {"ORBIT_NODE_RANK": "1", "HTTP_PROXY": "http://127.0.0.1:2"}
    original = nodes.container_variables
    try:
        nodes.container_variables = lambda: container  # type: ignore[assignment]
        variables = nodes.session_variables(given)
    finally:
        nodes.container_variables = original  # type: ignore[assignment]
    assert variables == {
        "PATH": "/opt/conda/bin:/usr/bin",
        "TASK_TOKEN": "t",
        "ORBIT_NODE_RANK": "1",
        "HTTP_PROXY": "http://127.0.0.1:2",
    }


def test_sshd_gets_every_variable_in_its_one_setenv_quoted():
    line = nodes.set_env({"B": 'say "hi" \\ # $x', "A": "two words", "C": ""})
    assert line == 'SetEnv "A=two words" "B=say \\"hi\\" \\\\ # $x" "C="'
    config = nodes.sshd_config("10.214.0.2", {"A": "1"})
    assert "ListenAddress 10.214.0.2\n" in config
    assert config.count("SetEnv") == 1 and 'SetEnv "A=1"' in config
    assert "PasswordAuthentication no" in config and "PermitRootLogin prohibit-password" in config


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node's paths below ``tmp_path``, a fake sshd, and root and the agent as this test's
    own account, homes of their own."""
    me = pwd.getpwuid(os.getuid())
    accounts = {
        name: pwd.struct_passwd((name, "x", me.pw_uid, me.pw_gid, "", str(home), "/bin/sh"))
        for name, home in (("root", tmp_path / "root"), ("agent", tmp_path / "home"))
    }
    for account in accounts.values():
        Path(account.pw_dir).mkdir()
    monkeypatch.setattr(nodes.pwd, "getpwnam", lambda name: accounts[name])
    monkeypatch.setattr(nodes.pwd, "getpwuid", lambda uid: accounts["root"])
    monkeypatch.setattr(nodes, "ETC", tmp_path / "etc/ssh/orbit")
    monkeypatch.setattr(nodes, "KNOWN_HOSTS", tmp_path / "etc/ssh/ssh_known_hosts")
    monkeypatch.setattr(nodes, "EMPTY", tmp_path / "var/empty")
    monkeypatch.setattr(nodes, "BIN", tmp_path / "usr/local/bin")
    monkeypatch.setattr(nodes, "SSH", tmp_path / "opt/orbit-ssh")
    monkeypatch.setattr(nodes, "container_variables", lambda: {"PATH": str(tmp_path / "bin")})
    sshd = tmp_path / "opt/orbit-ssh/sbin/sshd"
    sshd.parent.mkdir(parents=True)
    sshd.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/sshd.calls\n')
    sshd.chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "scp").write_text("")
    (tmp_path / "bin" / "scp").chmod(0o755)
    return tmp_path


def _join(rank: int = 1, variables: dict[str, str] | None = None) -> None:
    nodes.join(
        dict(
            rank=rank,
            names=["node0", "node1"],
            addresses=["10.214.0.1", "10.214.0.2"],
            account="agent",
            keys=KEYS,
            variables=variables or {"ORBIT_NODE_RANK": str(rank)},
        )
    )


def test_join_leaves_keys_the_known_host_and_clients_and_starts_sshd(node, monkeypatch):
    (node / "etc/ssh").mkdir(parents=True)
    (node / "etc/ssh/ssh_known_hosts").write_text("github.com ssh-ed25519 AAAAgh\n")
    _join()
    etc = node / "etc/ssh/orbit"
    assert (etc / "host").read_text() == "HOST PRIVATE\n"
    assert stat.S_IMODE((etc / "host").stat().st_mode) == 0o600
    assert (etc / "authorized/agent").read_text() == "ssh-ed25519 AAAAagent orbit-account\n"
    assert (etc / "authorized/root").read_text() == "ssh-ed25519 AAAAroot orbit-root\n"
    own = node / "home/.ssh"
    assert stat.S_IMODE(own.stat().st_mode) == 0o700
    assert (own / "id_ed25519").read_text() == "AGENT PRIVATE\n"
    assert stat.S_IMODE((own / "id_ed25519").stat().st_mode) == 0o600
    assert (node / "root/.ssh/id_ed25519").read_text() == "ROOT PRIVATE\n"
    assert (node / "etc/ssh/ssh_known_hosts").read_text().splitlines() == [
        "github.com ssh-ed25519 AAAAgh",
        f"node0,node1,10.214.0.1,10.214.0.2 ssh-ed25519 AAAAhost {nodes.MARK}",
    ]
    # The image's scp stays; ssh and sftp, which it lacks, are Orbit's.
    links = node / "usr/local/bin"
    assert sorted(path.name for path in links.iterdir()) == ["sftp", "ssh"]
    assert os.readlink(links / "ssh") == str(node / "opt/orbit-ssh/bin/ssh")
    config = (etc / "sshd_config").read_text()
    assert "ListenAddress 10.214.0.2" in config and '"ORBIT_NODE_RANK=1"' in config
    calls = (node / "sshd.calls").read_text().splitlines()
    assert calls == [f"-f {etc}/sshd_config -t", f"-f {etc}/sshd_config -E {etc}/log"]

    # Again, as the Sandbox is sealed: the same node, its proxy now, and sshd rereads.
    signals = []
    monkeypatch.setattr(nodes, "_running", lambda: 4242)
    monkeypatch.setattr(nodes.os, "kill", lambda pid, signal: signals.append((pid, signal)))
    _join(variables={"ORBIT_NODE_RANK": "1", "HTTP_PROXY": "http://127.0.0.1:2"})
    assert signals == [(4242, nodes.signal.SIGHUP)]
    assert '"HTTP_PROXY=http://127.0.0.1:2"' in (etc / "sshd_config").read_text()
    assert (node / "etc/ssh/ssh_known_hosts").read_text().count(nodes.MARK) == 1


def test_join_stops_where_sshd_refuses_its_configuration(node):
    (node / "opt/orbit-ssh/sbin/sshd").write_text("#!/bin/sh\necho bad option >&2\nexit 1\n")
    with pytest.raises(SystemExit, match="bad option"):
        _join()


def test_join_changes_nothing_through_a_link_the_account_left_for_its_keys(node):
    """An account that made its ``.ssh`` a link to a directory of root's, or its key a link to
    a file of root's, gets a directory and a key of its own, and root's stay as they were."""
    target = node / "etc-of-root"
    target.mkdir(mode=0o755)
    (node / "shadow").write_text("root:secret\n")
    (node / "home/.ssh").symlink_to(target)
    _join()
    own = node / "home/.ssh"
    assert own.is_dir() and not own.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755 and not any(target.iterdir())
    (own / "id_ed25519").unlink()
    (own / "id_ed25519").symlink_to(node / "shadow")
    _join()
    assert (node / "shadow").read_text() == "root:secret\n"
    assert not (own / "id_ed25519").is_symlink()
    assert (own / "id_ed25519").read_text() == "AGENT PRIVATE\n"


def test_sshd_starts_without_the_keys_in_its_environment(node, monkeypatch):
    seen = []
    real = nodes.subprocess.run

    def run(argv, **options):
        seen.append(options.get("env") or {})
        return real(argv, **options)

    monkeypatch.setenv(nodes.VARIABLE, "secret keys")
    monkeypatch.setattr(nodes.subprocess, "run", run)
    _join()
    assert seen and all(nodes.VARIABLE not in env and env for env in seen)


def test_a_node_whose_image_lacks_the_openssh_says_its_task_was_prepared_for_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(nodes, "SSH", tmp_path / "opt/orbit-ssh")
    with pytest.raises(SystemExit, match="none of whose Sandboxes spans several nodes"):
        nodes.main(["keys"])
