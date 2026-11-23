"""orbit-exec: a caller's command on another node of a multi-node Sandbox, as ssh runs one.

Each test starts real daemons on loopback ports, as ``python -I peers.py serve`` runs on
every node, and drives the client they write with the POSIX shells this machine has.
"""

from __future__ import annotations

import json
import os
import pwd
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbit.run.guestd import peers

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform != "linux", reason="multi-node Sandboxes are Linux containers"),
]

SECRET = "s" * 43
TIMEOUT = 30


def _shells() -> list[list[str]]:
    """``/bin/sh`` and every other POSIX shell found here, each once."""
    found: dict[str, list[str]] = {}
    for shell in (["/bin/sh"], ["dash"], ["bash", "--posix"], ["busybox", "sh"]):
        path = shutil.which(shell[0])
        if path is not None:
            found.setdefault(os.path.realpath(path), shell)
    return list(found.values())


@pytest.fixture(params=_shells(), ids=lambda shell: os.path.basename(shell[0]))
def shell(request: pytest.FixtureRequest) -> list[str]:
    return request.param


@dataclass
class Node:
    name: str
    address: tuple[str, int]
    root: Path
    daemon: subprocess.Popen[bytes]

    @property
    def client(self) -> Path:
        return self.root / "bin" / "orbit-exec"

    @property
    def spool(self) -> Path:
        return self.root / "spool"

    @property
    def env_file(self) -> Path:
        return self.root / "env.json"

    def tampered(self, old: str, new: str, path: Path) -> Path:
        """A copy of the client with one line changed, as a caller may write its own."""
        script = self.client.read_text()
        assert script.count(old) == 1, old
        path.write_text(script.replace(old, new))
        return path

    def exec(
        self,
        *args: str,
        shell: list[str] | None = None,
        stdin: bytes = b"",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        client: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*(shell or ["/bin/sh"]), str(client or self.client), *args],
            input=stdin,
            env=env,
            capture_output=True,
            cwd=cwd,
            timeout=TIMEOUT,
        )


Launch = Callable[..., dict[str, Node]]


@pytest.fixture
def launch(tmp_path: Path) -> Iterator[Launch]:
    """Start daemons that all know one another; each test's are stopped after it."""
    started: list[Node] = []

    def start(*names: str, secrets: dict[str, str] | None = None) -> dict[str, Node]:
        addresses = {name: _free_address() for name in names}
        table = ",".join(f"{name}={host}:{port}" for name, (host, port) in addresses.items())
        # The daemon's environment is every command's; keep the host's proxies out of it.
        base = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        nodes = {}
        for name in names:
            root = tmp_path / name
            root.mkdir()
            env = {**base, "ORBIT_TEST_NODE": name}
            env[peers.SECRET_VARIABLE] = (secrets or {}).get(name, SECRET)
            with (root / "daemon.log").open("wb") as log:
                daemon = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        peers.__file__,
                        "serve",
                        "--spool",
                        str(root / "spool"),
                        "--client",
                        str(root / "bin" / "orbit-exec"),
                        "--env-file",
                        str(root / "env.json"),
                        "--listen",
                        "{}:{}".format(*addresses[name]),
                        "--peers",
                        table,
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            nodes[name] = Node(name, addresses[name], root, daemon)
            started.append(nodes[name])
        for node in nodes.values():
            # The daemon writes its client once it listens.
            _until(lambda node=node: node.client.exists() or node.daemon.poll() is not None)
            assert node.daemon.poll() is None, (node.root / "daemon.log").read_text()
        return nodes

    yield start
    for node in started:
        node.daemon.terminate()
    for node in started:
        try:
            node.daemon.wait(10)
        except subprocess.TimeoutExpired:
            node.daemon.kill()
            node.daemon.wait()


@pytest.fixture
def nodes(launch: Launch) -> dict[str, Node]:
    return launch("node0", "node1")


def test_a_command_on_another_node_returns_its_output_and_status(
    nodes: dict[str, Node], shell: list[str]
) -> None:
    result = nodes["node0"].exec(
        "node1", "echo out $ORBIT_TEST_NODE; echo err >&2; exit 3", shell=shell
    )
    assert (result.stdout, result.stderr, result.returncode) == (b"out node1\n", b"err\n", 3)


def test_arguments_are_joined_by_spaces_and_run_by_the_remote_shell(
    nodes: dict[str, Node], shell: list[str]
) -> None:
    """As ssh: ``orbit-exec node1 echo 'a  b'`` runs ``echo a  b`` there."""
    result = nodes["node0"].exec("node1", "echo", "a  b", "'c  d'", "$ORBIT_TEST_NODE", shell=shell)
    assert result.stdout == b"a b c  d node1\n"
    assert result.returncode == 0


def test_stdin_reaches_the_command_and_streams_keep_every_byte(
    nodes: dict[str, Node], shell: list[str]
) -> None:
    data = os.urandom(3 * 1024 * 1024)
    result = nodes["node0"].exec("node1", "cat", shell=shell, stdin=data)
    assert result.returncode == 0, result.stderr
    assert result.stdout == data
    small = nodes["node0"].exec("node1", "cat; echo", shell=shell, stdin=b"data")
    assert small.stdout == b"data\n"


def test_a_caller_with_its_stdin_closed_gives_the_command_an_empty_one(
    nodes: dict[str, Node], shell: list[str]
) -> None:
    client = [*shell, str(nodes["node0"].client), "node1", "cat; echo done"]
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$@" <&-', "sh", *client], capture_output=True, timeout=TIMEOUT
    )
    assert (result.stdout, result.returncode) == (b"done\n", 0)


def test_the_command_starts_in_the_callers_directory_or_else_its_home(
    nodes: dict[str, Node], shell: list[str], tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = nodes["node0"].exec("node1", "pwd", shell=shell, cwd=work)
    assert result.stdout.decode().strip() == os.path.realpath(work)

    account = pwd.getpwuid(os.getuid())
    home = account.pw_dir if os.path.isdir(account.pw_dir) else "/"
    gone = tmp_path / "gone"
    gone.mkdir()
    # Called from a directory that no longer exists, as one missing on the other node is.
    enter_and_remove = (
        "import os, sys; os.chdir(sys.argv[1]); os.rmdir(sys.argv[1]); "
        "os.execvp(sys.argv[2], sys.argv[2:])"
    )
    client = [*shell, str(nodes["node0"].client), "node1", "pwd; echo $HOME"]
    result = subprocess.run(
        [sys.executable, "-c", enter_and_remove, str(gone), *client],
        capture_output=True,
        timeout=TIMEOUT,
    )
    lines = result.stdout.decode().splitlines()
    assert lines == [os.path.realpath(home), account.pw_dir], result.stderr


def test_a_command_on_the_callers_own_node_runs_there(nodes: dict[str, Node]) -> None:
    result = nodes["node0"].exec("node0", "echo $ORBIT_TEST_NODE")
    assert (result.stdout, result.returncode) == (b"node0\n", 0)


def test_requests_run_concurrently(nodes: dict[str, Node]) -> None:
    waiting = subprocess.Popen(
        ["/bin/sh", str(nodes["node0"].client), "node1", "cat"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        result = nodes["node0"].exec("node1", "echo meanwhile")
        assert (result.stdout, result.returncode) == (b"meanwhile\n", 0)
        out, _ = waiting.communicate(b"later", timeout=TIMEOUT)
        assert (out, waiting.returncode) == (b"later", 0)
    finally:
        waiting.kill()
        waiting.wait()


def test_an_unknown_node_fails_as_the_channel(nodes: dict[str, Node]) -> None:
    result = nodes["node0"].exec("node9", "true")
    assert result.returncode == 255
    assert b"node9" in result.stderr


def test_a_daemon_refuses_a_request_without_the_secret(launch: Launch, tmp_path: Path) -> None:
    nodes = launch("node0", "node1", "rogue", secrets={"rogue": "r" * 43})
    marker = tmp_path / "ran"
    # The daemon that holds another secret refuses node0's request: the channel fails.
    result = nodes["node0"].exec("rogue", f"touch {marker}")
    assert result.returncode == 255
    # So is any other connection: closed at once, before anything runs.
    request = {
        "secret": "x" * 43,
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cwd": str(tmp_path),
        "command": f"touch {marker}",
    }
    with socket.create_connection(nodes["node1"].address, timeout=TIMEOUT) as conn:
        conn.sendall(json.dumps(request).encode() + b"\n")
        assert conn.recv(1) == b""
    assert not marker.exists()
    assert "x" * 43 not in (nodes["node1"].root / "daemon.log").read_text()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
def test_a_caller_that_goes_ends_its_commands_process_group(
    nodes: dict[str, Node], shell: list[str], sig: signal.Signals, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    client = subprocess.Popen(
        [*shell, str(nodes["node0"].client), "node1", f"sleep 300 & echo $! >{pidfile}; wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _until(lambda: pidfile.exists() and pidfile.read_text().strip() != "")
        pid = int(pidfile.read_text())
        assert _alive(pid)
        client.send_signal(sig)
        client.wait(TIMEOUT)
        _until(lambda: not _alive(pid))
        # The request is gone from the spool, by the client's own trap or by the daemon.
        _until(lambda: not os.listdir(nodes["node0"].spool))
    finally:
        client.kill()
        client.wait()


def test_a_node_no_daemon_serves_fails_as_the_channel(launch: Launch) -> None:
    """As a one-node Sandbox booted from a snapshot of a multi-node one: client, no daemon."""
    node0 = launch("node0", "node1")["node0"]
    node0.daemon.terminate()
    node0.daemon.wait(10)
    # Every shell at once: each waits its ten seconds for the daemon to take its request.
    clients = [
        subprocess.Popen(
            [*shell, str(node0.client), "node1", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for shell in _shells()
    ]
    for client in clients:
        out, err = client.communicate(timeout=TIMEOUT)
        assert (out, err, client.returncode) == (
            b"",
            b"orbit-exec: no daemon serves this node\n",
            255,
        )
    assert not os.listdir(node0.spool)


def test_the_callers_exported_variables_reach_the_command(nodes: dict[str, Node]) -> None:
    """Orbit gives a Task's variables to commands on node 0 alone; orbit-exec carries them."""
    nodes["node1"].env_file.write_text(json.dumps({"ORBIT_TEST_SHARED": "from the env file"}))
    caller = {"ORBIT_TEST_EXPORTED": "a  b", "ORBIT_TEST_SHARED": "from the caller"}
    result = nodes["node0"].exec(
        "node1",
        'echo "$ORBIT_TEST_EXPORTED|$ORBIT_TEST_SHARED|$ORBIT_TEST_NODE"',
        env={**os.environ, **caller},
    )
    # What the caller did not export is the node's own.
    assert (result.stdout, result.returncode) == (b"a  b|from the caller|node1\n", 0)


def test_what_describes_the_callers_own_node_comes_from_the_target(
    nodes: dict[str, Node],
) -> None:
    proxy = "http://node1:3128"
    nodes["node1"].env_file.write_text(
        json.dumps({"ORBIT_NODE_RANK": "1", "http_proxy": proxy, "HTTPS_PROXY": proxy})
    )
    caller = {
        "ORBIT_NODE_RANK": "0",
        "http_proxy": "http://node0:3128",
        "HTTPS_PROXY": "http://node0:3128",
        "NO_PROXY": "node0",
        "HOME": "/elsewhere",
    }
    result = nodes["node0"].exec(
        "node1",
        'echo "$ORBIT_NODE_RANK $http_proxy $HTTPS_PROXY ${NO_PROXY-unset} $HOME"',
        env={**os.environ, **caller},
    )
    home = pwd.getpwuid(os.getuid()).pw_dir
    assert result.stdout.decode() == f"1 {proxy} {proxy} unset {home}\n"


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            'mkfifo "$dir/in"',
            'chmod 0777 "$dir" && mkfifo "$dir/in"',
            "others may write its directory",
        ),
        (
            """printf '%s' "$line" >"$dir/command\"""",
            'ln -s {tmp}/command "$dir/command"',
            "its command is not a file of its own",
        ),
        (
            """printf '%s' "$node" >"$dir/node\"""",
            'ln -s {tmp}/node "$dir/node"',
            "its node is not a file of its own",
        ),
        (
            'mkfifo "$dir/in" "$dir/out" "$dir/err" "$dir/lease"',
            'mkfifo "$dir/in" "$dir/out" "$dir/err" {tmp}/lease && ln -s {tmp}/lease "$dir/lease"',
            "its lease is not a FIFO of its own",
        ),
    ],
    ids=["writable-directory", "linked-command", "linked-node", "linked-fifo"],
)
def test_a_request_root_could_be_misled_by_is_refused(
    nodes: dict[str, Node], tmp_path: Path, old: str, new: str, reason: str
) -> None:
    """The daemon reads and opens only what the caller owns, never through a link: a request
    whose files lead elsewhere is refused, and its client says why."""
    marker = tmp_path / "ran"
    (tmp_path / "command").write_text(f"touch {marker}")
    (tmp_path / "node").write_text("node1")
    node0 = nodes["node0"]
    client = node0.tampered(old, new.format(tmp=tmp_path), tmp_path / "orbit-exec")
    result = node0.exec("node1", f"touch {marker}", client=client)
    assert result.returncode == 255
    assert (
        result.stderr.decode() == f"orbit-exec: this node's daemon refused the request: {reason}\n"
    )
    assert not marker.exists()
    _until(lambda: not os.listdir(node0.spool))


def test_a_pid_naming_another_accounts_process_is_refused(
    nodes: dict[str, Node], tmp_path: Path
) -> None:
    """The environment forwarded is read from the client's own process, never another's."""
    foreign = _foreign_pid()
    if foreign is None:
        pytest.skip("no process of another account to name")
    node0 = nodes["node0"]
    old = """printf '%s' "$$" >"$dir/pid\""""
    client = node0.tampered(old, f'printf {foreign} >"$dir/pid"', tmp_path / "orbit-exec")
    result = node0.exec("node1", "true", client=client)
    assert result.returncode == 255
    assert b"refused the request: its pid names another account's process" in result.stderr


def test_commands_get_the_env_file_read_afresh_each_time(nodes: dict[str, Node]) -> None:
    """The framework writes the egress proxy there while the Sandbox is sealed."""
    node1 = nodes["node1"]
    account = pwd.getpwuid(os.getuid())
    show = "echo ${http_proxy-unset} $HOME"
    node1.env_file.write_text(json.dumps({"http_proxy": "http://10.0.0.1:3128", "HOME": "/x"}))
    result = nodes["node0"].exec("node1", show)
    assert result.stdout.decode() == f"http://10.0.0.1:3128 {account.pw_dir}\n"
    node1.env_file.write_text("not json")
    assert nodes["node0"].exec("node1", show).stdout.decode() == f"unset {account.pw_dir}\n"
    node1.env_file.unlink()
    assert nodes["node0"].exec("node1", show).stdout.decode() == f"unset {account.pw_dir}\n"


def _foreign_pid() -> int | None:
    """A process of another account than this one, as root's init is."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid != os.getuid():
                    return int(entry.name)
            except OSError:
                continue
    return None


def _free_address() -> tuple[str, int]:
    """A free port on an address of its own in 127.0.0.0/8, which no parallel test picks."""
    host = "127.{}.{}.{}".format(*(random.randint(1, 254) for _ in range(3)))
    with socket.socket() as probe:
        probe.bind((host, 0))
        return host, probe.getsockname()[1]


def _until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _alive(pid: int) -> bool:
    """Running, rather than exited and waiting for whoever reaps it."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"
