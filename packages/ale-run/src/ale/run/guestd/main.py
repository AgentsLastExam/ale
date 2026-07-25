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
import os
import shutil
import socketserver
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import (
    CHUNK_BYTES,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_TIMEOUT,
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
        }
        try:
            if shell_cmd:
                proc = subprocess.Popen(shell_cmd, shell=True, **popen_kwargs)
            else:
                proc = subprocess.Popen(list(argv), **popen_kwargs)
        except FileNotFoundError as exc:
            raise ProtocolError(ERR_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise ProtocolError(ERR_PERMISSION, str(exc)) from exc

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self._stream(req_id, stdout, stderr)
            raise ProtocolError(ERR_TIMEOUT, f"command exceeded {timeout}s") from None

        self._stream(req_id, stdout, stderr)
        return ok(req_id, exit_code=proc.returncode)

    def _stream(self, req_id: int, stdout: bytes, stderr: bytes) -> None:
        for name, data in (("stdout_chunk", stdout), ("stderr_chunk", stderr)):
            for start in range(0, len(data), CHUNK_BYTES):
                block = data[start : start + CHUNK_BYTES]
                self._emit(event(req_id, name, b64=base64.b64encode(block).decode("ascii")))

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
