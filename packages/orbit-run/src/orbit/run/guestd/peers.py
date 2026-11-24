"""orbit-exec: commands on the other nodes of a multi-node Sandbox.

A multi-node Sandbox spans several machines, one container on each, named ``node0`` to
``nodeN-1`` and joined by a network that reaches only one another. The agent works on node 0
and has to start processes on the others, as ``torchrun`` on every node, ``mpirun --mca
plm_rsh_agent orbit-exec`` or ``pdsh -R exec`` do, yet images run no sshd and the agent holds
no scheduler credentials. So every node runs this daemon as root, and the agent runs
``orbit-exec NODE COMMAND [ARG...]``, which behaves as ssh does: the arguments, joined by
spaces, run by ``/bin/sh -c`` on NODE as the caller's account, in its working directory or
else its home, with its stdin, stdout and stderr, and the command's exit status is the
client's; 255 means the channel itself failed.

Unlike ssh, the command gets the variables its caller exported, since Orbit gives a Task's
variables to the agent's commands on node 0 alone. Those that describe the caller's own node
or session (its home, account, host, directory, rank and egress proxy) are left out. So a
command's environment is NODE's daemon's own, then the variables the framework keeps for
NODE in ``--env-file`` (its egress proxy while the Sandbox is sealed, its rank), then the
caller's, then the account's HOME, USER, LOGNAME and SHELL.

The client is a POSIX ``sh`` script, since an image need have nothing more and the
framework's Python is root's alone. It asks its own node's daemon through a spool directory
anyone may add to and only root may list. The kernel names the caller there, as the owner
of the request directory, so an account can run commands only as itself. The daemons carry
requests between nodes over TCP, each proving with the Sandbox's secret, which only root
sees, that it is one of them.

A request directory holds the FIFOs ``in``, ``out``, ``err`` and ``lease``, the files
``node``, ``cwd``, ``command`` and ``pid`` (the client's own), and last a ``ready`` marker.
The daemon reads the environment the client started with from ``/proc/<pid>/environ``, once
it has checked that the process is the caller's. It marks a request it takes with ``taken``,
which holds why when it refuses it; a client that sees no mark within ten seconds is on a
node no daemon serves, such as a one-node Sandbox booted from a snapshot of a multi-node one,
and fails. The client holds ``lease`` open for its whole life: when it closes, the caller has
gone, and the command's process group on the other node is ended. The daemon writes the exit
status to ``status`` before it closes ``out`` and ``err``.

Between daemons a request is one JSON line, followed both ways by frames of a one-byte kind
and a 4-byte big-endian length: stdin and its end from the asking node; stdout, stderr and
last the exit status from the node that runs the command. A connection that ends before the
status is a caller that has gone.

Standard library only, and no engine imports: this file is copied into guests.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hmac
import json
import os
import pwd
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["CLIENT", "SECRET_VARIABLE", "Daemon", "main"]

#: The variable that carries the Sandbox's secret to the daemon, never to a command.
SECRET_VARIABLE = "ORBIT_PEERS_SECRET"

STDIN, STDIN_EOF, STDOUT, STDERR, EXIT = range(1, 6)
_HEADER = struct.Struct("!BI")
_CHUNK = 64 * 1024
#: Bytes held for a reader that is not keeping up, past which nothing more is read for it.
_BACKLOG = 1024 * 1024
#: The longest single argument Linux passes to a program, which ``sh -c`` takes a command as.
_TEXT_LIMIT = 128 * 1024
#: The most environment Linux gives a program by default: a quarter of an 8 MiB stack.
_ENVIRON_LIMIT = 2 * 1024 * 1024
#: Room for a command and an environment at their largest, escaped as JSON.
_LINE_LIMIT = 16 * 1024 * 1024
#: What describes the caller's own node or session, which a command on another node does not
#: take from it.
_NODE_LOCAL = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "HOSTNAME",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "ORBIT_NODE_RANK",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    }
)
_SPOOL_POLL_SEC = 0.05
#: How long a client whose request is ready has to open its streams before it counts as gone.
_ARRIVAL_SEC = 30.0
#: How old an entry that never became a ready request is when it is removed.
_STALE_SEC = 60.0
#: For reaching another node's daemon, and for a connection's request line to arrive.
_CONNECT_SEC = 10.0
#: Between SIGTERM and SIGKILL for the command of a caller that has gone.
_GRACE_SEC = 5.0
#: How poll reports that the other end of a connection went away.
_HANGUP = select.POLLHUP | select.POLLERR | getattr(select, "POLLRDHUP", 0)
_CHANNEL_FAILED = 255

CLIENT = r"""#!/bin/sh
# orbit-exec NODE COMMAND [ARG...] runs COMMAND on NODE, another node of this Sandbox, as
# ssh would: the arguments, joined by spaces, run by /bin/sh -c there as this account, in
# this directory or else its home, with this stdin, stdout and stderr, and its exit status
# is this one's; 255 means the channel failed. Orbit writes this file.
if [ "$#" -lt 2 ]; then
	echo 'usage: orbit-exec NODE COMMAND [ARG...]' >&2
	exit 255
fi
node=$1
shift
IFS=' '
line="$*"
dir=$(mktemp -d @SPOOL@/XXXXXXXXXX) || exit 255
helpers=
trap 'kill $helpers 2>/dev/null; rm -rf "$dir"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mkfifo "$dir/in" "$dir/out" "$dir/err" "$dir/lease" &&
	printf '%s' "$node" >"$dir/node" &&
	printf '%s' "$line" >"$dir/command" &&
	{ pwd 2>/dev/null || :; } >"$dir/cwd" &&
	printf '%s' "$$" >"$dir/pid" &&
	: >"$dir/ready" || exit 255
# A node that runs no daemon never takes the request, and its FIFOs would never open.
tries=0
while [ ! -e "$dir/taken" ]; do
	if [ "$tries" -ge 100 ]; then
		echo 'orbit-exec: no daemon serves this node' >&2
		exit 255
	fi
	sleep 0.1
	tries=$((tries + 1))
done
if [ -s "$dir/taken" ]; then
	echo "orbit-exec: this node's daemon refused the request: $(cat "$dir/taken")" >&2
	exit 255
fi
# An asynchronous list reads /dev/null unless it is given stdin under another number.
if ! { command exec 4<&0; } 2>/dev/null; then
	exec 4</dev/null
fi
# The daemon takes the lease closing as this caller gone, so only this shell holds it. Each
# FIFO opens once the daemon has opened its other end.
command exec 3>"$dir/lease" 5>"$dir/in" || exit 255
cat <&4 >&5 3>&- 4<&- 5>&- &
helpers=$!
exec 4<&- 5>&-
cat "$dir/out" 3>&- &
out=$!
cat "$dir/err" >&2 3>&- &
err=$!
helpers="$helpers $out $err"
wait "$out"
wait "$err"
status=$(cat "$dir/status" 2>/dev/null)
case $status in
'' | *[!0-9]*) exit 255 ;;
esac
exit "$status"
"""


class _Malformed(Exception):
    """A spool entry that is not a request its owner could have made with the client."""


@dataclass
class _Call:
    """A caller's request from the spool, with its streams open (-1 once closed)."""

    uid: int
    node: str
    cwd: str
    command: str
    env: dict[str, str]
    lease: int
    stdin: int
    stdout: int
    stderr: int

    def close(self) -> None:
        self.lease = _closed(self.lease)
        self.stdin = _closed(self.stdin)
        self.stdout = _closed(self.stdout)
        self.stderr = _closed(self.stderr)


class _Outbox:
    """Bytes for a nonblocking descriptor, written as fast as it takes them."""

    def __init__(self, fd: int, data: bytes = b"") -> None:
        self.fd = fd
        self.data = bytearray(data)

    def flush(self) -> None:
        """Write what the descriptor takes now; an ``OSError`` means its reader has gone."""
        with contextlib.suppress(BlockingIOError):
            while self.data:
                del self.data[: os.write(self.fd, self.data)]


class Daemon:
    """One node's daemon: its callers' requests out to the nodes, and the nodes' in."""

    def __init__(
        self,
        *,
        spool: Path,
        client: Path,
        listen: tuple[str, int],
        peers: dict[str, tuple[str, int]],
        secret: str,
        env_file: Path | None = None,
    ) -> None:
        self._peers = peers
        self._secret = secret
        self._env_file = env_file
        self._server = socket.create_server(listen)
        spool.mkdir(parents=True, exist_ok=True)
        # Anyone may add an entry and remove only their own; only root lists them.
        os.chmod(spool, 0o1733)
        self._spool = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
        self._active: set[str] = set()
        self._lock = threading.Lock()
        _install_client(client, spool)

    def serve_forever(self) -> None:
        threading.Thread(target=self._watch, daemon=True).start()
        while True:
            try:
                conn, address = self._server.accept()
            except OSError:
                time.sleep(_SPOOL_POLL_SEC)  # out of descriptors: the next one may not be
                continue
            try:
                threading.Thread(
                    target=self._serve_peer, args=(conn, address[0]), daemon=True
                ).start()
            except RuntimeError:
                conn.close()  # out of threads: this request fails, the next may not

    # --- callers on this node -------------------------------------------------------------

    def _watch(self) -> None:
        while True:
            try:
                names = os.listdir(self._spool)
            except OSError:
                names = []
            for name in names:
                with self._lock:
                    if name in self._active:
                        continue
                try:
                    self._consider(name)
                except Exception as error:
                    _log(f"cannot take spool entry {name}: {error!r}")
            time.sleep(_SPOOL_POLL_SEC)

    def _consider(self, name: str) -> None:
        try:
            entry = os.stat(name, dir_fd=self._spool, follow_symlinks=False)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            folder = os.open(name, flags, dir_fd=self._spool)
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            self._remove(name)  # a link, a file, anything but a directory: no client made it
            return
        try:
            os.stat("ready", dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            os.close(folder)
            if time.time() - entry.st_mtime > _STALE_SEC:
                self._remove(name)
            return
        with self._lock:
            self._active.add(name)
        try:
            threading.Thread(target=self._take, args=(name, folder), daemon=True).start()
        except BaseException:
            os.close(folder)
            with self._lock:
                self._active.discard(name)
            raise

    def _take(self, name: str, folder: int) -> None:
        """Serve one request until its client exits, then remove it."""
        call = None
        try:
            call = _open_call(folder)
            if call is None:
                return  # its client never came for its streams
            status = self._exchange(call)
            if status is None:
                return  # its client has gone
            _create(folder, "status", b"%d\n" % status)
            call.stdout = _closed(call.stdout)
            call.stderr = _closed(call.stderr)
            _await_close(call.lease)
        except _Malformed as error:
            _log(f"refused uid {os.fstat(folder).st_uid}'s request: {error}")
            with contextlib.suppress(OSError):
                _create(folder, "taken", str(error).encode())
            _await_cleanup(folder)
        finally:
            if call is not None:
                call.close()
            os.close(folder)
            self._remove(name)
            with self._lock:
                self._active.discard(name)

    def _exchange(self, call: _Call) -> int | None:
        """Run the call on its node; its status, or ``None`` once its client has gone."""

        def fail(message: str) -> int:
            with contextlib.suppress(OSError):
                os.write(call.stderr, f"orbit-exec: {message}\n".encode())
            return _CHANNEL_FAILED

        address = self._peers.get(call.node)
        if address is None:
            names = ", ".join(self._peers)
            return fail(f"no node {call.node!r} in this Sandbox, which has {names}")
        try:
            account = pwd.getpwuid(call.uid)
        except KeyError:
            return fail(f"uid {call.uid} has no account here")
        request = {
            "secret": self._secret,
            "user": account.pw_name,
            "uid": call.uid,
            "gid": account.pw_gid,
            "cwd": call.cwd,
            "command": call.command,
            "env": call.env,
        }
        line = json.dumps(request).encode() + b"\n"
        if len(line) > _LINE_LIMIT:
            return fail("the command and environment are too large to send")
        try:
            conn = socket.create_connection(address, timeout=_CONNECT_SEC)
        except OSError as error:
            return fail(f"cannot reach {call.node}: {error.strerror or error}")
        with conn:
            conn.setblocking(False)
            return _relay(conn, call, line)

    def _remove(self, name: str) -> None:
        try:
            shutil.rmtree(name, dir_fd=self._spool)  # never follows a link its owner placed
        except FileNotFoundError:
            pass
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=self._spool)

    # --- other nodes' daemons -------------------------------------------------------------

    def _serve_peer(self, conn: socket.socket, host: str) -> None:
        with conn:
            try:
                request, rest = _receive_request(conn)
            except (OSError, ValueError) as error:
                _log(f"dropped a request from {host}: {error}")
                return
            secret = request.get("secret")
            if not isinstance(secret, str) or not hmac.compare_digest(
                secret.encode(errors="surrogatepass"), self._secret.encode()
            ):
                _log(f"refused a request from {host}: wrong secret")
                return
            try:
                user, uid, gid, cwd, command, env = (
                    _field(request, "user", str),
                    _field(request, "uid", int),
                    _field(request, "gid", int),
                    _field(request, "cwd", str),
                    _field(request, "command", str),
                    _field(request, "env", dict),
                )
                if not all(isinstance(v, str) for v in env.values()):
                    raise ValueError("the request's env holds a value that is not a string")
            except ValueError as error:
                _log(f"dropped a request from {host}: {error}")
                return
            try:
                process = self._spawn(user, uid, gid, cwd, command, env)
            except (KeyError, OSError, ValueError) as error:
                detail = f"no account {user!r} here" if isinstance(error, KeyError) else error
                with contextlib.suppress(OSError):
                    conn.sendall(
                        _frame(STDERR, f"orbit-exec: cannot start the command: {detail}\n".encode())
                        + _frame(EXIT, bytes([_CHANNEL_FAILED]))
                    )
                return
            conn.setblocking(False)
            _run(conn, process, rest)

    def _spawn(
        self, user: str, uid: int, gid: int, cwd: str, command: str, forwarded: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        """Start ``command`` as the account, in this node's environment and its caller's."""
        account = pwd.getpwnam(user)
        env = dict(os.environ)
        env.update(_env_file_variables(self._env_file))
        env.update(forwarded)
        env.update(
            HOME=account.pw_dir,
            USER=account.pw_name,
            LOGNAME=account.pw_name,
            SHELL=account.pw_shell or "/bin/sh",
        )
        identity: dict[str, Any] = {}
        if uid != os.geteuid():
            groups = os.getgrouplist(account.pw_name, gid)
            identity = {"user": uid, "group": gid, "extra_groups": groups}
        if not (os.path.isabs(cwd) and os.path.isdir(cwd)):
            cwd = account.pw_dir if os.path.isdir(account.pw_dir) else "/"
        return subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            **identity,
        )


def _relay(conn: socket.socket, call: _Call, request: bytes) -> int | None:
    """Pump a call's FIFOs through ``conn``; its status, or ``None`` once its client has gone."""
    peer = _Outbox(conn.fileno(), request)
    sinks = {STDOUT: _Outbox(call.stdout), STDERR: _Outbox(call.stderr)}
    inbox = bytearray()
    status: int | None = None
    while status is None or any(sink.data for sink in sinks.values()):
        poller = select.poll()
        poller.register(call.lease, select.POLLIN)
        if status is None:
            held = sum(len(sink.data) for sink in sinks.values())
            mask = select.POLLIN if held < _BACKLOG else 0
            poller.register(conn, mask | (select.POLLOUT if peer.data else 0))
            if call.stdin >= 0 and len(peer.data) < _BACKLOG:
                poller.register(call.stdin, select.POLLIN)
        for sink in sinks.values():
            if sink.data:
                poller.register(sink.fd, select.POLLOUT)
        for fd, event in poller.poll():
            if fd == call.lease:
                with contextlib.suppress(BlockingIOError):
                    if not os.read(call.lease, _CHUNK):
                        return None
            elif fd == call.stdin:
                try:
                    data = os.read(call.stdin, _CHUNK)
                except BlockingIOError:
                    continue
                peer.data += _frame(STDIN, data) if data else _frame(STDIN_EOF)
                if not data:
                    call.stdin = _closed(call.stdin)
            elif fd == conn.fileno():
                if status is not None:
                    continue
                try:
                    if event & select.POLLOUT:
                        peer.flush()
                    if event & ~select.POLLOUT:
                        data = conn.recv(_CHUNK)
                        if not data:
                            raise ConnectionError("closed")
                        inbox += data
                        for kind, payload in _frames(inbox):
                            if kind == EXIT and len(payload) == 1:
                                status = payload[0]
                                break
                            if kind in sinks:
                                sinks[kind].data += payload
                except OSError:
                    sinks[STDERR].data += f"orbit-exec: lost {call.node}\n".encode()
                    status = _CHANNEL_FAILED
            else:
                try:
                    next(sink for sink in sinks.values() if sink.fd == fd).flush()
                except OSError:
                    return _CHANNEL_FAILED  # the caller stopped reading; the command ends
    return status


def _run(conn: socket.socket, process: subprocess.Popen[bytes], inbox: bytearray) -> None:
    """Stream a peer's command through ``conn``; end its process group if the peer goes."""
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    caller = _Outbox(conn.fileno())
    stdin: _Outbox | None = _Outbox(process.stdin.fileno())
    os.set_blocking(stdin.fd, False)
    stdin_ended = False
    outputs = {process.stdout.fileno(): STDOUT, process.stderr.fileno(): STDERR}
    exited = False
    try:
        while not exited or caller.data:
            for kind, payload in _frames(inbox):
                if kind == STDIN and stdin is not None:
                    stdin.data += payload
                elif kind == STDIN_EOF:
                    stdin_ended = True
            if stdin is not None and stdin_ended and not stdin.data:
                process.stdin.close()
                stdin = None
            if not outputs and not exited and process.poll() is not None:
                exited = True
                caller.data += _frame(EXIT, bytes([_status(process.returncode)]))
            poller = select.poll()
            mask = select.POLLOUT if caller.data else 0
            if stdin is None or len(stdin.data) < _BACKLOG:
                mask |= select.POLLIN
            poller.register(conn, mask | _HANGUP)
            if stdin is not None and stdin.data:
                poller.register(stdin.fd, select.POLLOUT)
            if len(caller.data) < _BACKLOG:
                for fd in outputs:
                    poller.register(fd, select.POLLIN)
            for fd, event in poller.poll(None if outputs else _SPOOL_POLL_SEC * 1000):
                if fd == conn.fileno():
                    if event & select.POLLOUT:
                        caller.flush()
                    if event & select.POLLIN:
                        data = conn.recv(_CHUNK)
                        if not data:
                            raise ConnectionError("the caller has gone")
                        inbox += data
                    elif event & _HANGUP:
                        raise ConnectionError("the caller has gone")
                elif stdin is not None and fd == stdin.fd:
                    try:
                        stdin.flush()
                    except OSError:  # the command closed its stdin: the rest goes nowhere
                        process.stdin.close()
                        stdin = None
                elif fd in outputs:
                    data = os.read(fd, _CHUNK)
                    if data:
                        caller.data += _frame(outputs[fd], data)
                    else:
                        del outputs[fd]
    except OSError:
        _end(process)
    except BaseException:
        _end(process)
        raise
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            with contextlib.suppress(OSError):
                pipe.close()


def _end(process: subprocess.Popen[bytes]) -> None:
    """End a command's whole process group: SIGTERM, and SIGKILL for what outlasts the grace."""
    group = process.pid
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + _GRACE_SEC
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        time.sleep(_SPOOL_POLL_SEC)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(_GRACE_SEC)


def _status(returncode: int) -> int:
    return returncode & 0xFF if returncode >= 0 else 128 - returncode


def _frame(kind: int, payload: bytes = b"") -> bytes:
    return _HEADER.pack(kind, len(payload)) + payload


def _frames(inbox: bytearray) -> Iterator[tuple[int, bytes]]:
    """Take the complete frames off the front of ``inbox``."""
    while len(inbox) >= _HEADER.size:
        kind, length = _HEADER.unpack_from(inbox)
        if length > _CHUNK:
            raise ConnectionError(f"a frame of {length} bytes, longer than any sent")
        end = _HEADER.size + length
        if len(inbox) < end:
            return
        payload = bytes(inbox[_HEADER.size : end])
        del inbox[:end]
        yield kind, payload


def _receive_request(conn: socket.socket) -> tuple[dict[str, Any], bytearray]:
    """A peer's request line, and the bytes that followed it."""
    conn.settimeout(_CONNECT_SEC)
    received = bytearray()
    while True:
        chunk = conn.recv(_CHUNK)
        if not chunk:
            raise ValueError("the connection closed before the request")
        received += chunk
        if b"\n" in chunk:
            break
        if len(received) > _LINE_LIMIT:
            raise ValueError("the request is too long")
    line, _, rest = received.partition(b"\n")
    request = json.loads(line)
    if not isinstance(request, dict):
        raise ValueError("the request is not an object")
    return request, bytearray(rest)


def _field(request: dict[str, Any], name: str, kind: type) -> Any:
    value = request.get(name)
    if not isinstance(value, kind) or isinstance(value, bool) or (kind is int and value < 0):
        raise ValueError(f"the request's {name} is not a {kind.__name__}")
    return value


def _env_file_variables(path: Path | None) -> dict[str, str]:
    """The variables the framework keeps in ``path``, read afresh for every command."""
    if path is None:
        return {}
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)}


# --- the spool ----------------------------------------------------------------------------


def _open_call(folder: int) -> _Call | None:
    """Take a ready request and open its streams; ``None`` when its client never came for them.

    Everything is read and opened relative to the request directory, never through a link,
    and must belong to the directory's owner, so a caller cannot point root at anything it
    could not open itself.
    """
    info = os.fstat(folder)
    uid = info.st_uid
    if info.st_mode & 0o022:
        raise _Malformed("others may write its directory")
    node, cwd, command, pid = (
        _read_text(folder, name, uid) for name in ("node", "cwd", "command", "pid")
    )
    env = _caller_environment(pid, uid)
    fifos = {name: _fifo(folder, name, uid) for name in ("lease", "in", "out", "err")}
    try:
        _create(folder, "taken", b"")
    except OSError as error:
        raise _Malformed(f"cannot mark it taken: {error.strerror}") from None
    fds: list[int] = []
    try:
        # Opening these lets the client open its ends; it opens its readers of ``out`` and
        # ``err`` only once it holds ``lease`` and ``in``.
        for name in ("lease", "in"):
            fd = _open_fifo(folder, name, fifos[name], os.O_RDONLY)
            assert fd is not None  # a reader never waits for a writer
            fds.append(fd)
        deadline = time.monotonic() + _ARRIVAL_SEC
        for name in ("out", "err"):
            while (fd := _open_fifo(folder, name, fifos[name], os.O_WRONLY)) is None:
                if time.monotonic() > deadline:
                    for fd in fds:
                        os.close(fd)
                    return None
                time.sleep(_SPOOL_POLL_SEC)
            fds.append(fd)
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    return _Call(uid, node, cwd.removesuffix("\n"), command, env, *fds)


def _read_text(folder: int, name: str, uid: int) -> str:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        raise _Malformed(f"its {name} is not a file of its own") from None
    with open(fd, "rb") as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            raise _Malformed(f"its {name} is not a file of its own")
        data = file.read(_TEXT_LIMIT + 1)
    if len(data) > _TEXT_LIMIT:
        raise _Malformed(f"its {name} is longer than a command can be")
    return os.fsdecode(data)


def _caller_environment(pid: str, uid: int) -> dict[str, str]:
    """What the caller exported: the environment its client started with, less what
    describes the caller's own node or session.

    A ``cat`` with the client's own uid and gid reads it: only those credentials, or
    CAP_SYS_PTRACE, which a container's root lacks, may. So the kernel, too, refuses another
    account's process, even one that took the pid since it was checked.
    """
    if not pid.isdigit():
        raise _Malformed("its pid names no process")
    try:
        process = os.stat(f"/proc/{pid}")
    except OSError:
        raise _Malformed("its pid names no process") from None
    if process.st_uid != uid:
        raise _Malformed("its pid names another account's process")
    identity: dict[str, Any] = {}
    if uid != os.geteuid():
        identity = {"user": uid, "group": process.st_gid, "extra_groups": []}
    try:
        reader = subprocess.Popen(
            ["cat", f"/proc/{pid}/environ"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            **identity,
        )
    except OSError as error:
        raise _Malformed(f"cannot read its environment: {error.strerror}") from None
    with reader:
        assert reader.stdout is not None
        data = reader.stdout.read(_ENVIRON_LIMIT + 1)
        if len(data) > _ENVIRON_LIMIT:
            reader.kill()
            raise _Malformed("its environment is larger than a program's can be")
    if reader.returncode != 0:
        raise _Malformed("cannot read its environment")
    env = {}
    for entry in data.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if separator and name and os.fsdecode(name) not in _NODE_LOCAL:
            env[os.fsdecode(name)] = os.fsdecode(value)
    return env


def _fifo(folder: int, name: str, uid: int) -> os.stat_result:
    try:
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except OSError:
        info = None
    if info is None or not stat.S_ISFIFO(info.st_mode) or info.st_uid != uid:
        raise _Malformed(f"its {name} is not a FIFO of its own")
    return info


def _exists(folder: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_fifo(folder: int, name: str, before: os.stat_result, mode: int) -> int | None:
    """Open the FIFO checked as ``before``; ``None`` for a writer while nothing reads it."""
    try:
        fd = os.open(name, mode | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=folder)
    except OSError as error:
        if error.errno == errno.ENXIO:
            return None
        raise _Malformed(f"its {name} went away: {error.strerror}") from None
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise _Malformed(f"its {name} changed while it was opened")
    return fd


def _create(folder: int, name: str, data: bytes) -> None:
    """Create a file in a caller's request directory, never through anything it placed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(name, flags, 0o644, dir_fd=folder)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _await_cleanup(folder: int) -> None:
    """Give a refused client the time to read why and remove its request."""
    deadline = time.monotonic() + _ARRIVAL_SEC
    while _exists(folder, "taken") and time.monotonic() < deadline:
        time.sleep(_SPOOL_POLL_SEC)


def _await_close(lease: int) -> None:
    """Wait for the client to close its lease, as it does when it exits."""
    poller = select.poll()
    poller.register(lease, select.POLLIN)
    while True:
        poller.poll()
        with contextlib.suppress(BlockingIOError):
            if not os.read(lease, _CHUNK):
                return


def _closed(fd: int) -> int:
    """Close ``fd`` if it is open; -1, for the field that held it."""
    if fd >= 0:
        os.close(fd)
    return -1


def _install_client(path: Path, spool: Path) -> None:
    """Write the client whole, so that no caller ever runs part of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    partial.write_text(CLIENT.replace("@SPOOL@", shlex.quote(str(spool))))
    partial.chmod(0o755)
    partial.replace(path)


def _log(message: str) -> None:
    print(f"orbit-exec: {message}", file=sys.stderr, flush=True)


def _address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"not HOST:PORT: {text!r}")
    return host, int(port)


def _peer_table(text: str) -> dict[str, tuple[str, int]]:
    table: dict[str, tuple[str, int]] = {}
    for item in text.split(","):
        name, separator, address = item.partition("=")
        if not name or not separator:
            raise argparse.ArgumentTypeError(f"not NAME=HOST:PORT: {item!r}")
        table[name] = _address(address)
    return table


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="peers.py", description="orbit-exec's daemon on one node of a multi-node Sandbox"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    serve = actions.add_parser(
        "serve", help=f"run this node's daemon, with the Sandbox's secret in ${SECRET_VARIABLE}"
    )
    serve.add_argument("--spool", type=Path, required=True, help="the callers' spool directory")
    serve.add_argument("--client", type=Path, required=True, help="where to write the client")
    serve.add_argument(
        "--env-file", type=Path, help="a JSON object of variables every command gets, if present"
    )
    serve.add_argument("--listen", type=_address, required=True, help="HOST:PORT for the nodes")
    serve.add_argument(
        "--peers", type=_peer_table, required=True, help="every node as NAME=HOST:PORT,..."
    )
    args = parser.parse_args(argv)
    secret = os.environ.pop(SECRET_VARIABLE, "")
    if not secret:
        parser.error(f"{SECRET_VARIABLE} is not set")
    Daemon(
        spool=args.spool,
        client=args.client,
        listen=args.listen,
        peers=args.peers,
        secret=secret,
        env_file=args.env_file,
    ).serve_forever()


if __name__ == "__main__":
    main()
