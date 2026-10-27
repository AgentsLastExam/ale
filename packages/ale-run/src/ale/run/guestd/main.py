"""ale-guestd — the in-sandbox execution service.

Every exec, file transfer and screenshot goes through here, so the host side is
identical for containers and virtual machines and providers only have to attach a
transport.

Constraints that shape this file:

* **Standard library only.** It runs on whatever Python the image happens to have.
* **No engine imports.** It is copied into images, not installed from our workspace.
* **One request at a time per connection.** Ordering is a feature: a trace has to be
  reconstructible, and interleaved streams are not worth the confusion.

Two transports, one implementation: ``--stdio`` for containers (a piped exec session)
and ``--tcp`` for virtual machines (reached through a forwarded port).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import queue
import select
import shutil
import socketserver
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import (
    CHUNK_BYTES,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_UNAUTHORIZED,
    ERR_UNSUPPORTED,
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode,
    encode,
    err,
    event,
    ok,
)

GUESTD_VERSION = "0.1.1"

#: Where a claimed TCP token survives a service restart. /run is tmpfs: a reboot or a disk
#: saved as an image carries no token forward. Windows keeps it in memory only, because
#: its image builder saves a running guest's disk.
DEFAULT_TOKEN_FILE = None if os.name == "nt" else Path("/run/ale/guestd-token")


class _Desync(Exception):
    """A request body that cannot be parsed further; the session must end."""


class Handler:
    """Executes operations. Transport-agnostic by construction."""

    def __init__(self, emit: Any, reader: BinaryIO | None = None) -> None:
        self._emit = emit
        self._reader = reader

    def dispatch(self, req_id: int, op: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ProtocolError(ERR_UNSUPPORTED, f"unknown operation: {op}")
        return handler(req_id, params)

    # --- introspection ------------------------------------------------------

    def op_health(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        from gui import has_desktop

        operating_system = (
            "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
        )
        agent_user = os.environ.get("ALE_AGENT_USER") or _current_user()
        agent_uid = None
        if os.name != "nt":
            import pwd

            with contextlib.suppress(KeyError):
                agent_uid = pwd.getpwnam(agent_user).pw_uid
        return ok(
            req_id,
            proto=PROTOCOL_VERSION,
            guestd_version=GUESTD_VERSION,
            os=operating_system,
            python=sys.version.split()[0],
            user=_current_user(),
            agent_user=agent_user,
            agent_home=os.environ.get("ALE_AGENT_HOME") or _home_of(agent_user),
            framework_uid=os.geteuid() if os.name != "nt" else None,
            agent_uid=agent_uid,
            gui=has_desktop(),
        )

    # --- execution ----------------------------------------------------------

    def op_exec(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        argv = params.get("argv")
        shell_cmd = params.get("shell")
        if not argv and not shell_cmd:
            raise ProtocolError(ERR_BAD_REQUEST, "exec needs argv or shell")

        env = dict(os.environ)
        env.update(params.get("env") or {})
        cwd = params.get("cwd")
        timeout = params.get("timeout_sec")
        acknowledged_output = bool(params.get("output_ack"))
        if acknowledged_output and (os.name == "nt" or self._reader is None):
            raise ProtocolError(ERR_UNSUPPORTED, "acknowledged output requires a POSIX session")

        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        popen_kwargs.update(_drop_to(params.get("run_as"), env))
        try:
            if os.name == "nt" and params.get("run_as") and os.environ.get("ALE_AGENT_SUDO") != "1":
                # The elevated launcher must never inherit agent-controlled cwd or Python
                # startup settings. Pass those only to its limited-token child.
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-S", str(Path(__file__).with_name("windows.py"))],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                assert proc.stdin is not None
                with proc.stdin:
                    proc.stdin.write(
                        json.dumps(
                            {
                                "argv": argv,
                                "shell": shell_cmd,
                                "cwd": cwd,
                                "env": env,
                            }
                        ).encode("utf-8")
                    )
            elif shell_cmd:
                proc = subprocess.Popen(shell_cmd, shell=True, **popen_kwargs)
            else:
                proc = subprocess.Popen(list(argv), **popen_kwargs)
        except FileNotFoundError as exc:
            raise ProtocolError(ERR_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc

        timed_out = (
            self._stream_acknowledged_process(req_id, proc, timeout)
            if acknowledged_output
            else self._stream_process(req_id, proc, timeout)
        )
        return ok(
            req_id,
            exit_code=None if timed_out else proc.returncode,
            timed_out=timed_out,
        )

    def _stream_process(
        self, req_id: int, proc: subprocess.Popen[bytes], timeout: float | None
    ) -> bool:
        assert proc.stdout is not None and proc.stderr is not None
        chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue()

        def read(stream: BinaryIO, name: str) -> None:
            read_chunk = getattr(stream, "read1", stream.read)
            while block := read_chunk(CHUNK_BYTES):
                chunks.put((name, block))
            chunks.put((name, None))

        readers = [
            threading.Thread(target=read, args=(proc.stdout, "stdout_chunk"), daemon=True),
            threading.Thread(target=read, args=(proc.stderr, "stderr_chunk"), daemon=True),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + float(timeout) if timeout else None
        timed_out = False
        open_streams = len(readers)
        while open_streams:
            if deadline is not None and time.monotonic() >= deadline and not timed_out:
                timed_out = True
                # Descendants can hold the pipes open after the launcher exits.
                _terminate(proc)
            try:
                name, block = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            if block is None:
                open_streams -= 1
                continue
            self._emit(event(req_id, name, b64=base64.b64encode(block).decode("ascii")))
        proc.wait()
        return timed_out

    def _stream_acknowledged_process(
        self, req_id: int, proc: subprocess.Popen[bytes], timeout: float | None
    ) -> bool:
        """Keep one bounded output event in flight across relays without backpressure."""
        assert self._reader is not None and proc.stdout is not None and proc.stderr is not None
        input_fd = self._reader.fileno()
        streams = {proc.stdout.fileno(): "stdout_chunk", proc.stderr.fileno(): "stderr_chunk"}
        deadline = time.monotonic() + float(timeout) if timeout else None
        sequence = 0
        awaiting_ack = False
        ack = bytearray()
        finished = False
        try:
            while streams or awaiting_ack or proc.poll() is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return True
                ready, _, _ = select.select(
                    [input_fd, *([] if awaiting_ack else streams)],
                    [],
                    [],
                    min(0.1, remaining) if remaining is not None else 0.1,
                )
                if input_fd in ready:
                    block = os.read(input_fd, 1024)
                    if not block:
                        raise _Desync("exec output acknowledgement channel closed")
                    ack.extend(block)
                    if len(ack) > 1024:
                        raise _Desync("exec output acknowledgement exceeds its limit")
                    if b"\n" in ack:
                        try:
                            message = json.loads(ack)
                        except (ValueError, UnicodeError) as exc:
                            raise _Desync("invalid exec output acknowledgement") from exc
                        if (
                            not awaiting_ack
                            or not isinstance(message, dict)
                            or message.get("id") != req_id
                            or message.get("op") != "exec_output_ack"
                            or type(message.get("seq")) is not int
                            or message["seq"] != sequence
                        ):
                            raise _Desync("unexpected exec output acknowledgement")
                        ack.clear()
                        awaiting_ack = False
                    continue
                for descriptor in ready:
                    block = os.read(descriptor, 64 * 1024)
                    if not block:
                        del streams[descriptor]
                        continue
                    name = streams.pop(descriptor)
                    streams[descriptor] = name
                    sequence += 1
                    self._emit(
                        event(
                            req_id,
                            name,
                            b64=base64.b64encode(block).decode("ascii"),
                            seq=sequence,
                        )
                    )
                    awaiting_ack = True
                    break
            proc.wait()
            finished = True
            return False
        finally:
            if not finished:
                _terminate(proc)
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    # --- files --------------------------------------------------------------

    def op_write_file(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        data = base64.b64decode(_require(params, "b64"))
        append = bool(params.get("append"))
        owner = _owner_of(params.get("run_as"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with os.fdopen(_open_for_write(path, append=append), "wb") as handle:
                handle.write(data)
                if owner is not None:
                    # Written by the service, which is root, so ownership is set
                    # explicitly. A file the agent is meant to change but cannot is the
                    # failure the identity model exists to prevent, and it surfaces far
                    # from its cause.
                    os.fchown(handle.fileno(), *owner)
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc
        return ok(req_id, bytes=len(data))

    def op_put_tree(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """Write a directory tree streamed in the request body.

        Every entry gets the upload modes (directories 0755, files 0644) and the owner
        of ``run_as`` (root when absent) as it is written, so a task's files behave the
        same whichever Provider carried them. A failure part-way still drains the body,
        keeping the session usable, and reports the first error.
        """
        if self._reader is None:
            raise ProtocolError(ERR_UNSUPPORTED, "put_tree needs a streamed session")
        reader = self._reader
        root = _require(params, "path")
        failure: ProtocolError | None = None
        tree: _PosixTree | _PathTree | None = None
        try:
            owner = _owner_of(params.get("run_as") or "root")
            tree = _PosixTree(root, owner) if os.open in os.supports_dir_fd else _PathTree(root)
        except ProtocolError as exc:
            failure = exc
        except OSError as exc:
            failure = _os_failure(exc)
        files = total = 0
        splice = _splices(reader)
        try:
            while True:
                frame = _frame(reader, req_id)
                if frame.get("end"):
                    break
                if "abort" in frame:
                    failure = failure or ProtocolError(ERR_BAD_REQUEST, str(frame["abort"]))
                    break
                kind = frame.get("kind")
                size = _size(frame) if kind == "file" else 0
                drained = False
                entry = frame.get("entry")
                try:
                    if failure is None and tree is not None:
                        parts = _entry_parts(entry)
                        if kind == "dir":
                            tree.directory(parts)
                        elif kind == "file":
                            sink = tree.file(parts)
                            try:
                                drained = True
                                _copy(reader, size, sink, splice=splice)
                            finally:
                                os.close(sink)
                            files += 1
                            total += size
                        elif kind == "symlink":
                            tree.symlink(parts, str(frame.get("target") or ""))
                        else:
                            raise ProtocolError(ERR_BAD_REQUEST, f"unknown entry kind: {kind!r}")
                except ProtocolError as exc:
                    failure = failure or exc
                except OSError as exc:
                    failure = failure or _os_failure(exc, str(entry))
                if not drained:
                    _copy(reader, size, None)
        finally:
            if tree is not None:
                tree.close()
        if failure is not None:
            raise failure
        return ok(req_id, files=files, bytes=total)

    def op_read_file(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        offset = int(params.get("offset") or 0)
        length = params.get("length")
        if offset < 0 or (length is not None and int(length) < 0):
            raise ProtocolError(ERR_BAD_REQUEST, "file offset and length must be nonnegative")
        remaining = None if length is None else int(length)
        copied = 0
        with _open_regular(Path(_require(params, "path"))) as handle:
            handle.seek(offset)
            while remaining is None or remaining > 0:
                block = handle.read(
                    CHUNK_BYTES if remaining is None else min(CHUNK_BYTES, remaining)
                )
                if not block:
                    break
                self._emit(event(req_id, "chunk", b64=base64.b64encode(block).decode("ascii")))
                copied += len(block)
                if remaining is not None:
                    remaining -= len(block)
        return ok(req_id, bytes=copied, eof=length is None or copied < int(length))

    def op_file_digest(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        remaining = int(_require(params, "length"))
        if remaining < 0:
            raise ProtocolError(ERR_BAD_REQUEST, "digest length must be nonnegative")
        digest = hashlib.sha256()
        with _open_regular(Path(_require(params, "path"))) as handle:
            while remaining > 0 and (block := handle.read(min(CHUNK_BYTES, remaining))):
                digest.update(block)
                remaining -= len(block)
        return ok(req_id, digest=f"sha256:{digest.hexdigest()}")

    def op_stat(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        if not path.exists():
            return ok(req_id, exists=False)
        info = path.stat()
        return ok(
            req_id,
            exists=True,
            is_dir=path.is_dir(),
            size=info.st_size,
            mode=oct(info.st_mode & 0o777),
        )

    def op_mkdirs(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        # A link where the directory belongs is replaced, as put_tree replaces one.
        _clear_link(path)
        path.mkdir(parents=True, exist_ok=True)
        if (owner := _owner_of(params.get("run_as"))) is not None:
            os.chown(path, *owner, follow_symlinks=False)
        return ok(req_id)

    def op_list_files(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        root = Path(_require(params, "path"))
        if not root.is_absolute() or ".." in root.parts:
            raise ProtocolError(ERR_BAD_REQUEST, "file listings require an absolute path")
        _refuse_links(root)
        files: list[dict[str, Any]] = []
        pending = [root]
        while pending:
            path = pending.pop()
            try:
                info = os.lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if _is_link(info):
                continue
            if stat.S_ISREG(info.st_mode):
                files.append({"path": str(path), "size": info.st_size})
            elif stat.S_ISDIR(info.st_mode):
                try:
                    with os.scandir(path) as children:
                        pending.extend(Path(child.path) for child in children)
                except (FileNotFoundError, NotADirectoryError):
                    continue
        return ok(req_id, files=sorted(files, key=lambda item: item["path"]))

    def op_disk_usage(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        usage = shutil.disk_usage(Path(_require(params, "path")))
        return ok(req_id, total=usage.total, used=usage.used, free=usage.free)

    def op_remove(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return ok(req_id)
        if _is_link(info):
            _clear_link(path)
        elif stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
        return ok(req_id)

    # --- desktop ------------------------------------------------------------

    def op_screenshot(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        from gui import capture_screen  # local import: headless images need no GUI stack

        png = capture_screen()
        if "path" in params:
            path = Path(params["path"])
            if not path.is_absolute() or ".." in path.parts:
                raise ProtocolError(ERR_BAD_REQUEST, "screenshot path must be absolute")
            _refuse_links(path.parent)
            # The Host chooses a unique framework-owned path before capture, so it
            # can remove the file even when the capture reply is lost or cancelled.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(png)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return ok(req_id, bytes=len(png), sha256=hashlib.sha256(png).hexdigest())
        return ok(req_id, png_b64=base64.b64encode(png).decode("ascii"))

    def op_input(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        from gui import dispatch_actions

        applied = dispatch_actions(params.get("actions") or [])
        return ok(req_id, applied=applied)

    def op_screen_size(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        from gui import screen_size

        width, height = screen_size()
        return ok(req_id, width=width, height=height)


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise ProtocolError(ERR_BAD_REQUEST, f"missing parameter: {key}")
    return params[key]


def _is_link(info: os.stat_result) -> bool:
    # A Windows junction is a reparse point rather than a symbolic link, but leads
    # elsewhere just the same.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _refuse_links(path: Path) -> None:
    """Refuse a read whose path passes through a link anywhere.

    A read hands the Host bytes as root, and every directory below the agent's home is the
    agent's to replace with a link, so no part of the path is followed. A missing part is
    left for the read itself to report.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return
        if _is_link(info):
            raise ProtocolError(ERR_BAD_REQUEST, f"path passes through a link: {current}")


def _clear_link(path: Path) -> None:
    """Remove a link at ``path`` itself, leaving what it points at alone."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not _is_link(info):
        return
    # A Windows link to a directory is itself a directory entry.
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0):
        os.rmdir(path)
    else:
        os.unlink(path)


def _open_for_write(path: Path, *, append: bool) -> int:
    """Open ``path`` for writing without writing through a link left there.

    The service is root and may hand the file to the agent afterwards, so a symbolic or
    hard link at ``path`` is replaced, as put_tree replaces one, rather than letting the
    write and chown land on what it points at. The path's parents resolve normally.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if append:
        flags |= os.O_APPEND
    _clear_link(path)
    descriptor = os.open(path, flags, 0o666)
    info = os.fstat(descriptor)
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        if not append:
            os.ftruncate(descriptor, 0)
        return descriptor
    os.close(descriptor)
    if append:
        raise ProtocolError(ERR_BAD_REQUEST, f"not a regular file: {path}")
    os.unlink(path)
    return os.open(path, flags | os.O_EXCL, 0o666)


@contextlib.contextmanager
def _open_regular(path: Path) -> Iterator[BinaryIO]:
    """Open a regular file for reading, refusing a link anywhere on ``path``."""
    try:
        _refuse_links(path)
        # O_NOFOLLOW and O_NONBLOCK keep a link or FIFO swapped in after the check from
        # being followed or from blocking the service.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ProtocolError(ERR_BAD_REQUEST, f"not a regular file: {path}")
            yield handle
    except FileNotFoundError as exc:
        raise ProtocolError(ERR_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise ProtocolError(ERR_PERMISSION, str(exc)) from exc


# --- streamed uploads ---------------------------------------------------------


def _frame(reader: BinaryIO, req_id: int) -> dict[str, Any]:
    raw = reader.readline(MAX_LINE_BYTES + 1)
    if not raw:
        raise _Desync("the request body ended early")
    try:
        frame = decode(raw.decode("utf-8"))
    except (ProtocolError, UnicodeDecodeError) as exc:
        raise _Desync(f"unreadable body frame: {exc}") from exc
    if frame.get("id") != req_id:
        raise _Desync("a body frame names another request")
    return frame


def _size(frame: dict[str, Any]) -> int:
    size = frame.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _Desync("a file entry needs a non-negative size")
    return size


def _entry_parts(entry: object) -> tuple[str, ...]:
    """Split a relative POSIX entry name, refusing anything that could leave the root."""
    if not isinstance(entry, str) or not entry:
        raise ProtocolError(ERR_BAD_REQUEST, f"unsafe entry name: {entry!r}")
    parts = tuple(entry.split("/"))
    unsafe = "\\\0:" if os.name == "nt" else "\0"
    if any(part in {"", ".", ".."} or set(part) & set(unsafe) for part in parts):
        raise ProtocolError(ERR_BAD_REQUEST, f"unsafe entry name: {entry!r}")
    return parts


def _splices(reader: BinaryIO) -> bool:
    """Whether file bodies can move from ``reader`` to disk inside the kernel.

    A container session's stdin is a pipe, and ``os.splice`` (Python 3.10+) moves a pipe's
    bytes into a file without copying them through this process: the difference between
    a quarter and a half of a second per 256 MiB.
    """
    if not hasattr(os, "splice"):
        return False
    try:
        return stat.S_ISFIFO(os.fstat(reader.fileno()).st_mode)
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return False


def _copy(reader: BinaryIO, size: int, sink: int | None, *, splice: bool = False) -> None:
    """Move exactly ``size`` body bytes into the descriptor ``sink``, or discard them.

    A failed write still consumes the rest, so the next frame is where the sender put it.
    """
    remaining, failure = size, None

    def write(view: memoryview) -> None:
        nonlocal failure
        if sink is None or failure is not None:
            return
        try:
            while view:
                view = view[os.write(sink, view) :]
        except OSError as exc:
            failure = exc

    if remaining and sink is not None and splice:
        # Take what the buffered reader already holds, then splice from its descriptor:
        # past this point its buffer is empty, so nothing is read twice or skipped.
        ahead = reader.read(min(len(reader.peek(1)), remaining))  # type: ignore[attr-defined]
        if not ahead:
            raise _Desync("the request body ended inside a file")
        remaining -= len(ahead)
        write(memoryview(ahead))
        while remaining and failure is None:
            try:
                moved = os.splice(reader.fileno(), sink, min(remaining, 1 << 24))
            except OSError:
                break  # a file system without splice: the loop below copies the rest
            if not moved:
                raise _Desync("the request body ended inside a file")
            remaining -= moved
    while remaining:
        block = reader.read(min(remaining, CHUNK_BYTES))
        if not block:
            raise _Desync("the request body ended inside a file")
        remaining -= len(block)
        write(memoryview(block))
    if failure is not None:
        raise failure


def _os_failure(exc: OSError, entry: str = "") -> ProtocolError:
    code = ERR_PERMISSION if isinstance(exc, PermissionError) else ERR_INTERNAL
    return ProtocolError(code, f"{entry + ': ' if entry else ''}{exc.strerror or exc}")


class _PosixTree:
    """An upload root written only through directory descriptors.

    The service writing it is root, so nothing already below the root is followed: a
    symbolic or hard link left where an entry lands is replaced, never written through.
    The root's own parents are framework-chosen paths and resolve normally.
    """

    _DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

    def __init__(self, root: str, owner: tuple[int, int] | None) -> None:
        self._owner = owner
        parent, name = os.path.split(os.path.abspath(root))
        if not name:
            raise ProtocolError(ERR_BAD_REQUEST, "cannot upload onto the filesystem root")
        os.makedirs(parent, exist_ok=True)
        base = os.open(parent, os.O_RDONLY)
        try:
            self._root = self._enter(base, name)
        finally:
            os.close(base)
        self._chain: list[tuple[str, int]] = []

    def directory(self, parts: tuple[str, ...]) -> None:
        self._at(parts)

    def file(self, parts: tuple[str, ...]) -> int:
        parent, name = self._at(parts[:-1]), parts[-1]
        self._clear(parent, name)
        descriptor = os.open(name, self._NEW_FILE, 0o644, dir_fd=parent)
        try:
            self._settle(descriptor, 0o644)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def symlink(self, parts: tuple[str, ...], target: str) -> None:
        parent, name = self._at(parts[:-1]), parts[-1]
        self._clear(parent, name)
        os.symlink(target, name, dir_fd=parent)
        if self._owner is not None:
            os.chown(name, *self._owner, dir_fd=parent, follow_symlinks=False)

    def close(self) -> None:
        for _, descriptor in self._chain:
            os.close(descriptor)
        self._chain.clear()
        os.close(self._root)

    def _at(self, names: tuple[str, ...]) -> int:
        """The descriptor for ``names``, reusing the open chain as far as it matches."""
        keep = 0
        while keep < min(len(names), len(self._chain)) and self._chain[keep][0] == names[keep]:
            keep += 1
        for _, descriptor in self._chain[keep:]:
            os.close(descriptor)
        del self._chain[keep:]
        for name in names[keep:]:
            parent = self._chain[-1][1] if self._chain else self._root
            self._chain.append((name, self._enter(parent, name)))
        return self._chain[-1][1] if self._chain else self._root

    def _enter(self, parent: int, name: str) -> int:
        try:
            descriptor = os.open(name, self._DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            os.mkdir(name, 0o755, dir_fd=parent)
            descriptor = os.open(name, self._DIRECTORY, dir_fd=parent)
        except OSError:
            if not stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                raise
            os.unlink(name, dir_fd=parent)
            os.mkdir(name, 0o755, dir_fd=parent)
            descriptor = os.open(name, self._DIRECTORY, dir_fd=parent)
        try:
            self._settle(descriptor, 0o755)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _settle(self, descriptor: int, mode: int) -> None:
        if self._owner is not None:
            os.fchown(descriptor, *self._owner)
        os.fchmod(descriptor, mode)

    @staticmethod
    def _clear(parent: int, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)


class _PathTree:
    """The same upload where directory descriptors do not exist (Windows).

    Windows access comes from ACLs, which the Provider resets once the tree is written.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def directory(self, parts: tuple[str, ...]) -> None:
        self._root.joinpath(*parts).mkdir(parents=True, exist_ok=True)

    def file(self, parts: tuple[str, ...]) -> int:
        path = self._root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        return os.open(path, flags, 0o644)

    def symlink(self, parts: tuple[str, ...], target: str) -> None:
        path = self._root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.is_file():
            path.unlink()
        os.symlink(target, path)

    def close(self) -> None:
        return None


def _drop_to(name: str | None, env: dict[str, str]) -> dict[str, Any]:
    """Run a child process as ``name``, adjusting the environment to match.

    Setting the uid alone is not enough: a process whose ``HOME`` still points at root's
    home writes dotfiles it cannot read back, and a graphical program looks for its
    session under the wrong runtime directory. So the identity and the environment that
    describes it move together.

    Silently ignored when we are not root — a guest service that is already unprivileged
    cannot drop further, and refusing would break images that run as a normal user.
    """
    if os.name == "nt":
        if name and name.casefold() not in {
            _current_user().casefold(),
            _current_user().rsplit("\\", 1)[-1].casefold(),
        }:
            raise ProtocolError(
                ERR_UNSUPPORTED,
                f"Windows guestd is running as {_current_user()!r}, not requested user {name!r}",
            )
        return {}
    if not name or os.geteuid() != 0:
        return {}

    import pwd

    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise ProtocolError(ERR_NOT_FOUND, f"no such user in this image: {name}") from None

    env["HOME"] = account.pw_dir
    env["USER"] = env["LOGNAME"] = account.pw_name
    if sys.platform == "darwin":
        # Framework PATH excludes agent-writable tools. Add them only to the child
        # whose uid and supplementary groups are changed below.
        env["PATH"] = f"{account.pw_dir}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + env.get(
            "PATH", "/usr/bin:/bin:/usr/sbin:/sbin"
        )
    else:
        env.setdefault("XDG_RUNTIME_DIR", f"/tmp/runtime-{account.pw_name}")

    # A desktop image publishes its session bus address for exactly this. Without it a
    # graphical program starts, creates a window nobody maps, and exits successfully —
    # so a task author debugging a GUI setup sees a script that "worked" and a screen
    # that did not change.
    if sys.platform != "darwin" and "DBUS_SESSION_BUS_ADDRESS" not in env:
        try:
            with open("/tmp/dbus-session-bus-address") as handle:
                if address := handle.read().strip():
                    env["DBUS_SESSION_BUS_ADDRESS"] = address
        except OSError:
            pass

    def preexec() -> None:
        os.setgid(account.pw_gid)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setuid(account.pw_uid)

    return {"preexec_fn": preexec}


def _home_of(name: str) -> str:
    """The account's home directory, from the image's own account database."""
    if os.name == "nt":
        return str(Path.home())
    import pwd

    try:
        return pwd.getpwnam(name).pw_dir
    except KeyError:
        return ""


def _owner_of(name: str | None) -> tuple[int, int] | None:
    """The uid/gid pair for a user, or ``None`` when there is nothing to change."""
    if os.name == "nt" or not name or os.geteuid() != 0:
        return None

    import pwd

    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise ProtocolError(ERR_NOT_FOUND, f"no such user in this image: {name}") from None
    return account.pw_uid, account.pw_gid


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - unusual images without a passwd entry
        return str(os.getuid()) if hasattr(os, "getuid") else "unknown"


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)


class Claim:
    """The token of the framework's first TCP connection; later ones must present it.

    No Task or agent code runs in a freshly booted guest before the framework connects,
    so the first token offered is the framework's. Every later connection, whether from
    the agent over loopback or from anything else that reaches the forwarded port, must
    prove it holds the same token before a single operation runs.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._token = self._load()

    def admit(self, token: object) -> bool:
        if not isinstance(token, str) or len(token) < 32:
            return False
        with self._lock:
            if self._token is None:
                try:
                    self._store(token)
                except OSError:
                    return False  # an unpersisted claim could be retaken after a restart
                self._token = token
                return True
            return hmac.compare_digest(self._token.encode("utf-8"), token.encode("utf-8"))

    def _load(self) -> str | None:
        if self._path is None:
            return None
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _store(self, token: str) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)


def _authenticate(line: str, claim: Claim, emit: Any) -> bool:
    req_id = 0
    try:
        message = decode(line)
        req_id = int(message.get("id", 0))
        params = message.get("params") or {}
        admitted = (
            message.get("op") == "auth"
            and isinstance(params, dict)
            and claim.admit(params.get("token"))
        )
    except (ProtocolError, TypeError, ValueError):
        admitted = False
    if not admitted:
        emit(err(req_id, ERR_UNAUTHORIZED, "authenticate with the framework token first"))
        return False
    emit(ok(req_id, protocol=PROTOCOL_VERSION))
    return True


def serve_stream(reader: BinaryIO, writer: TextIO, claim: Claim | None = None) -> None:
    """Serve requests from a byte stream until it closes.

    Requests are lines; an operation with a body reads its frames from the same stream.
    With a ``claim``, the first request must authenticate; anything else closes the
    connection before an operation can run.
    """

    def emit(payload: dict[str, Any]) -> None:
        writer.write(encode(payload) + "\n")
        writer.flush()

    handler = Handler(emit, reader)
    authenticated = claim is None
    last_acknowledged_exec: int | None = None
    while raw := reader.readline():
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if not authenticated:
            assert claim is not None
            if not _authenticate(line, claim, emit):
                return
            authenticated = True
            continue
        req_id = 0
        try:
            message = decode(line)
            req_id = int(message.get("id", 0))
            op = message.get("op")
            if not isinstance(op, str):
                raise ProtocolError(ERR_BAD_REQUEST, "missing op")
            if op == "exec_output_ack" and req_id == last_acknowledged_exec:
                # A command deadline may finish before its last output is consumed.
                continue
            params = message.get("params") or {}
            if op == "exec" and params.get("output_ack"):
                last_acknowledged_exec = req_id
            emit(handler.dispatch(req_id, op, params))
        except _Desync as exc:
            # The rest of the stream cannot be framed, so no later reply could be trusted.
            emit(err(req_id, ERR_BAD_REQUEST, str(exc)))
            return
        except ProtocolError as exc:
            emit(err(req_id, exc.code, exc.message))
        except Exception as exc:  # keep the session alive; one bad request is not fatal
            emit(err(req_id, ERR_INTERNAL, f"{type(exc).__name__}: {exc}"))


class _TCPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        writer = self.wfile

        class _TextWriter:
            @staticmethod
            def write(text: str) -> None:
                writer.write(text.encode("utf-8"))

            @staticmethod
            def flush() -> None:
                writer.flush()

        serve_stream(self.rfile, _TextWriter(), self.server.claim)  # type: ignore[arg-type, attr-defined]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[socketserver.BaseRequestHandler],
        claim: Claim | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.claim = claim if claim is not None else Claim(None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ale-guestd", description="ALE guest service")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true", help="serve one session over stdin/stdout")
    mode.add_argument("--tcp", metavar="HOST:PORT", help="serve over TCP")
    parser.add_argument("--ready-file", help="touch this path once serving")
    parser.add_argument("--token-file", help="where a claimed TCP token is kept")
    args = parser.parse_args(argv)

    if args.ready_file:
        Path(args.ready_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.ready_file).write_text(f"{GUESTD_VERSION}\n", encoding="utf-8")

    if args.stdio:
        serve_stream(sys.stdin.buffer, sys.stdout)
        return 0

    host, _, port = args.tcp.rpartition(":")
    claim = Claim(Path(args.token_file) if args.token_file else DEFAULT_TOKEN_FILE)
    with _Server((host or "0.0.0.0", int(port)), _TCPHandler, claim) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
