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
import os
import selectors
import shutil
import signal
import socketserver
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import (
    CHUNK_BYTES,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_UNSUPPORTED,
    PROTOCOL_VERSION,
    ProtocolError,
    decode,
    encode,
    err,
    event,
    ok,
)

GUESTD_VERSION = "0.1.0"


class Handler:
    """Executes operations. Transport-agnostic by construction."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def dispatch(self, req_id: int, op: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ProtocolError(ERR_UNSUPPORTED, f"unknown operation: {op}")
        return handler(req_id, params)

    # --- introspection ------------------------------------------------------

    def op_health(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        return ok(
            req_id,
            proto=PROTOCOL_VERSION,
            guestd_version=GUESTD_VERSION,
            os=sys.platform,
            python=sys.version.split()[0],
            user=_current_user(),
            gui=_has_display(),
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

        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
        }
        popen_kwargs.update(_drop_to(params.get("run_as"), env))
        try:
            if shell_cmd:
                proc = subprocess.Popen(shell_cmd, shell=True, **popen_kwargs)
            else:
                proc = subprocess.Popen(list(argv), **popen_kwargs)
        except FileNotFoundError as exc:
            raise ProtocolError(ERR_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc

        timed_out = self._stream_process(req_id, proc, timeout)
        return ok(
            req_id,
            exit_code=None if timed_out else proc.returncode,
            timed_out=timed_out,
        )

    def _stream_process(
        self, req_id: int, proc: subprocess.Popen[bytes], timeout: float | None
    ) -> bool:
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None and proc.stderr is not None
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout_chunk")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr_chunk")
        deadline = time.monotonic() + float(timeout) if timeout else None
        timed_out = False

        while selector.get_map():
            if deadline is not None and time.monotonic() >= deadline and proc.poll() is None:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            wait = 0.1 if remaining is None else min(0.1, remaining)
            for key, _ in selector.select(wait):
                block = os.read(key.fileobj.fileno(), CHUNK_BYTES)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                self._emit(
                    event(
                        req_id,
                        key.data,
                        b64=base64.b64encode(block).decode("ascii"),
                    )
                )
        proc.wait()
        return timed_out

    # --- files --------------------------------------------------------------

    def op_write_file(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        data = base64.b64decode(_require(params, "b64"))
        append = bool(params.get("append"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("ab" if append else "wb") as handle:
                handle.write(data)
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc
        if (mode := params.get("mode")) is not None:
            os.chmod(path, int(mode, 8) if isinstance(mode, str) else mode)
        if (owner := _owner_of(params.get("run_as"))) is not None:
            # Written by the service, which is root, so ownership is set explicitly.
            # A file the agent is meant to change but cannot is the failure the identity
            # model exists to prevent, and it surfaces far from its cause.
            os.chown(path, *owner)
        return ok(req_id, bytes=len(data))

    def op_read_file(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        offset = int(params.get("offset") or 0)
        length = params.get("length")
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(int(length)) if length else handle.read()
        except FileNotFoundError as exc:
            raise ProtocolError(ERR_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc
        for start in range(0, len(data), CHUNK_BYTES):
            block = data[start : start + CHUNK_BYTES]
            self._emit(event(req_id, "chunk", b64=base64.b64encode(block).decode("ascii")))
        return ok(req_id, bytes=len(data), eof=not length or len(data) < int(length))

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
        Path(_require(params, "path")).mkdir(parents=True, exist_ok=True)
        return ok(req_id)

    def op_remove(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        return ok(req_id)

    # --- desktop ------------------------------------------------------------

    def op_screenshot(self, req_id: int, params: dict[str, Any]) -> dict[str, Any]:
        from gui import capture_screen  # local import: headless images need no GUI stack

        png = capture_screen()
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


def _drop_to(name: str | None, env: dict[str, str]) -> dict[str, Any]:
    """Run a child process as ``name``, adjusting the environment to match.

    Setting the uid alone is not enough: a process whose ``HOME`` still points at root's
    home writes dotfiles it cannot read back, and a graphical program looks for its
    session under the wrong runtime directory. So the identity and the environment that
    describes it move together.

    Silently ignored when we are not root — a guest service that is already unprivileged
    cannot drop further, and refusing would break images that run as a normal user.
    """
    if not name or os.geteuid() != 0:
        return {}

    import pwd

    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise ProtocolError(ERR_NOT_FOUND, f"no such user in this image: {name}") from None

    env["HOME"] = account.pw_dir
    env["USER"] = env["LOGNAME"] = account.pw_name
    env.setdefault("XDG_RUNTIME_DIR", f"/tmp/runtime-{account.pw_name}")

    # A desktop image publishes its session bus address for exactly this. Without it a
    # graphical program starts, creates a window nobody maps, and exits successfully —
    # so a task author debugging a GUI setup sees a script that "worked" and a screen
    # that did not change.
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
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


def _owner_of(name: str | None) -> tuple[int, int] | None:
    """The uid/gid pair for a user, or ``None`` when there is nothing to change."""
    if not name or os.geteuid() != 0:
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


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def serve_stream(reader: TextIO | BinaryIO, writer: TextIO) -> None:
    """Serve requests from a line-oriented stream until it closes."""

    def emit(payload: dict[str, Any]) -> None:
        writer.write(encode(payload) + "\n")
        writer.flush()

    handler = Handler(emit)
    for raw in reader:  # type: ignore[union-attr]
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        req_id = 0
        try:
            message = decode(line)
            req_id = int(message.get("id", 0))
            op = message.get("op")
            if not isinstance(op, str):
                raise ProtocolError(ERR_BAD_REQUEST, "missing op")
            emit(handler.dispatch(req_id, op, message.get("params") or {}))
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

        serve_stream(self.rfile, _TextWriter())  # type: ignore[arg-type]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ale-guestd", description="ALE guest service")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true", help="serve one session over stdin/stdout")
    mode.add_argument("--tcp", metavar="HOST:PORT", help="serve over TCP")
    parser.add_argument("--ready-file", help="touch this path once serving")
    args = parser.parse_args(argv)

    if args.ready_file:
        Path(args.ready_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.ready_file).write_text(f"{GUESTD_VERSION}\n", encoding="utf-8")

    if args.stdio:
        serve_stream(sys.stdin, sys.stdout)
        return 0

    host, _, port = args.tcp.rpartition(":")
    with _Server((host or "0.0.0.0", int(port)), _TCPHandler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
