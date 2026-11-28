"""Worker stdio between the episode Pod and a peer Pod in its indexed Kubernetes Job.

A session uses a bounded window of atomic files in each direction. The reader removes
frames after delivery. No scheduler credentials, network listener or SSH access are needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# A long-running guest session has constant disk use, even when its peer stops reading.
CHUNK = 256 * 1024
WINDOW = 8
POLL = 0.04
LEASE = 90
SHUTDOWN = threading.Event()
MODES = {"block", "open", "exec", "rmi", "export", "load", "drop", "copy"}


def publish(path: Path, data: bytes) -> None:
    scratch = path.with_suffix(".part")
    scratch.write_bytes(data)
    scratch.replace(path)


class Closed(Exception):
    pass


class Stream:
    def __init__(self, path: Path, side: str):
        self.path = path
        self.side = side
        self.other = "node" if side == "host" else "host"
        self.sent = self.received = 0
        self.closed = threading.Event()
        self.beat = b""
        self.seen = time.monotonic()

    def watch(self) -> None:
        """Notice peer death even if neither half of the stream has any traffic."""
        heartbeat = self.path / self.side
        peer = self.path / self.other
        try:
            while not self.closed.is_set() and not SHUTDOWN.is_set():
                publish(heartbeat, str(time.monotonic_ns()).encode())
                # Reopen the directory when crossing an NFS client cache.
                os.listdir(self.path)
                if (self.path / f"{self.other}-closed").exists():
                    return
                if peer.exists() and (now := peer.read_bytes()) != self.beat:
                    self.beat, self.seen = now, time.monotonic()
                if time.monotonic() - self.seen >= LEASE:
                    return
                self.closed.wait(1)
        except OSError:
            pass
        finally:
            self.closed.set()

    def wait(self) -> None:
        if self.closed.wait(POLL):
            raise Closed
        os.listdir(self.path)

    def send(self, kind: bytes, data: bytes = b"") -> None:
        """One writer owns each direction and publishes frames in order."""
        if self.sent >= WINDOW:
            oldest = self.path / f"{self.side}-{self.sent - WINDOW}"
            while oldest.exists():
                self.wait()
        publish(self.path / f"{self.side}-{self.sent}", kind + data)
        self.sent += 1

    def get(self) -> tuple[bytes, bytes]:
        frame = self.path / f"{self.other}-{self.received}"
        while True:
            try:
                data = frame.read_bytes()
            except FileNotFoundError:
                self.wait()
            else:
                frame.unlink()
                self.received += 1
                return data[:1], data[1:]

    def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(FileNotFoundError):
            (self.path / (self.side + "-closed")).touch()


def host_input(channel: Stream) -> None:
    try:
        while data := os.read(0, CHUNK):
            channel.send(b"i", data)
        channel.send(b"e")
    except (Closed, OSError):
        pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def connect(directory: Path, args: list[str]) -> int:
    root = directory / "sessions"
    session = Path(tempfile.mkdtemp(dir=root))
    channel = Stream(session, "host")
    try:
        publish(session / "request", json.dumps(args).encode())
        threading.Thread(target=channel.watch, daemon=True).start()
        threading.Thread(target=host_input, args=(channel,), daemon=True).start()
        while True:
            kind, data = channel.get()
            if kind == b"x":
                return int(data)
            write_all({b"o": 1, b"r": 2}[kind], data)
    finally:
        channel.close()
        # The node removes the files once it observes this acknowledgement. If no job
        # remains, its directory and every session are already being removed by the Host.


def node_input(channel: Stream, worker: subprocess.Popen) -> None:
    assert worker.stdin is not None
    try:
        while True:
            kind, data = channel.get()
            if kind == b"e":
                return
            if kind != b"i":
                raise ValueError("invalid stdin frame")
            write_all(worker.stdin.fileno(), data)
    except (BrokenPipeError, Closed, FileNotFoundError):
        pass
    finally:
        worker.stdin.close()


def stop(worker: subprocess.Popen) -> None:
    # A short-lived shell or Podman client can exit before its pipe-holding child.
    # Reap the leader and terminate the control process group in either case.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        worker.poll()
        try:
            os.killpg(worker.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
    worker.wait()


def serve(session: Path, worker_script: Path) -> None:
    channel = Stream(session, "node")
    threading.Thread(target=channel.watch, daemon=True).start()
    try:
        args = json.loads((session / "request").read_bytes())
        if (
            not isinstance(args, list)
            or not args
            or not all(isinstance(a, str) for a in args)
            or args[0] not in MODES
        ):
            raise ValueError("invalid allocation worker command")
        worker = subprocess.Popen(
            [sys.executable, "-I", str(worker_script), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        complete = False
        try:
            assert worker.stdout is not None and worker.stderr is not None
            pipes = {worker.stdout: b"o", worker.stderr: b"r"}
            threading.Thread(target=node_input, args=(channel, worker), daemon=True).start()
            while pipes and not channel.closed.is_set():
                for pipe in select.select(list(pipes), [], [], 0.1)[0]:
                    data = os.read(pipe.fileno(), CHUNK)
                    if data:
                        channel.send(pipes[pipe], data)
                    else:
                        del pipes[pipe]
                        pipe.close()
            if channel.closed.is_set():
                raise Closed
            while worker.poll() is None:
                if channel.closed.wait(0.1):
                    raise Closed
            complete = True
            channel.send(
                b"x",
                str(
                    worker.returncode if worker.returncode >= 0 else 128 - worker.returncode
                ).encode(),
            )
            channel.closed.wait(LEASE + 2)  # Keep the last files until the Host reads the exit.
        finally:
            if not complete:
                stop(worker)
    except (Closed, FileNotFoundError):
        pass
    except Exception as exc:
        if not channel.closed.is_set():
            with contextlib.suppress(Closed, OSError):
                channel.send(b"r", f"allocation worker: {exc}\n".encode())
                channel.send(b"x", b"1")
                channel.closed.wait(LEASE + 2)
    finally:
        channel.close()
        shutil.rmtree(session, ignore_errors=True)


def listen(directory: Path, worker_script: Path, endpoint: Path) -> None:
    root = endpoint / "sessions"
    root.mkdir(mode=0o700, exist_ok=True)
    sessions: dict[Path, threading.Thread] = {}
    signal.signal(signal.SIGTERM, lambda _signum, _frame: SHUTDOWN.set())
    while not SHUTDOWN.is_set() and not (directory / "stop").read_text():
        for session in root.iterdir():
            if session in sessions or not (session / "request").is_file():
                continue
            thread = threading.Thread(target=serve, args=(session, worker_script), daemon=True)
            sessions[session] = thread
            thread.start()
        sessions = {s: t for s, t in sessions.items() if t.is_alive()}
        time.sleep(POLL)
    SHUTDOWN.set()
    for thread in sessions.values():
        thread.join()


if __name__ == "__main__":
    if sys.argv[1] == "connect":
        # A cancelled Host transport closes the remote command immediately, even
        # when neither its input nor its output has carried so much as one byte.
        signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
        try:
            sys.exit(connect(Path(sys.argv[2]), sys.argv[3:]))
        except (Closed, OSError) as error:
            sys.exit(f"allocation control channel closed: {error}")
    else:
        endpoint = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
        listen(Path.cwd(), Path(__file__).with_name("worker.py"), endpoint)
