"""Guest sessions and a host-initiated gateway tunnel for remote Linux containers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    SANDBOX_GATEWAY_PORT,
    ExecOutputSink,
    ExecResult,
    Identity,
    Sandbox,
    SandboxFile,
    SandboxRequest,
)
from ale.run import guestd
from ale.run.guestd.tunnel import Mux
from ale.run.providers import relay
from ale.run.transport import GuestClient

PROXY_PORT = SANDBOX_GATEWAY_PORT + 1


class CommandChannel(Protocol):
    async def argv(self, remote_command: str) -> list[str]: ...


class GatewayTunnel:
    """The Host end of the stream tunnel; reconnects if its SSH session drops."""

    def __init__(
        self,
        ssh: CommandChannel,
        *,
        listeners: list[tuple[int, int]],
        targets: list[tuple[str, int]],
    ) -> None:
        self._ssh = ssh
        self._targets = targets
        self._command = "cd / && exec {} -I {} {}".format(
            guestd.PYTHON["linux"],
            "/opt/ale/guestd/tunnel.py",
            " ".join(f"--listen {port}:{target}" for port, target in listeners),
        )
        self._closing = False
        self._first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, timeout_sec: float = 120) -> None:
        self._task = asyncio.ensure_future(self._supervise())
        try:
            await asyncio.wait_for(asyncio.shield(self._first), timeout_sec)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        if self._first.done() and not self._first.cancelled():
            self._first.exception()  # mark retrieved
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _dial(self, index: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        host, port = self._targets[index]
        return await asyncio.open_connection(host, port)

    async def _supervise(self) -> None:
        failures = 0
        detail = ""
        while not self._closing:
            ready = False
            try:
                process = await asyncio.create_subprocess_exec(
                    *await self._ssh.argv(self._command),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._process = process
                assert process.stdin is not None and process.stdout is not None
                mux = Mux(process.stdout, process.stdin, dial=self._dial)
                running = asyncio.ensure_future(mux.run())
                try:
                    waiting = asyncio.ensure_future(mux.ready.wait())
                    await asyncio.wait(
                        {waiting, running}, timeout=60, return_when=asyncio.FIRST_COMPLETED
                    )
                    waiting.cancel()
                    ready = mux.ready.is_set()
                    if ready:
                        failures = 0
                        if not self._first.done():
                            self._first.set_result(None)
                        await running
                finally:
                    running.cancel()
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                    assert process.stderr is not None
                    detail = (await process.stderr.read()).decode("utf-8", "replace").strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            if self._closing:
                return
            if not ready:
                failures += 1
                if failures >= 3:
                    if not self._first.done():
                        self._first.set_exception(
                            ProviderStartError(f"the sandbox tunnel did not start: {detail[-500:]}")
                        )
                    return
            await asyncio.sleep(min(2**failures, 15))


def tunnel_plan(
    request: SandboxRequest,
) -> tuple[str | None, list[tuple[int, int]], list[tuple[str, int]]]:
    """What the tunnel carries onto the sandbox's loopback: the Gateway at its fixed port and,
    under a proxied policy, the Host's egress proxy at ``PROXY_PORT``.

    Returns the Gateway's URL there, the ports to listen on with the index of the Host
    address behind each, and those addresses.
    """
    listeners: list[tuple[int, int]] = []
    targets: list[tuple[str, int]] = []
    local_gateway = None
    if request.gateway_url:
        parts = _tunneled(request.gateway_url)
        local_gateway = parts._replace(netloc=f"127.0.0.1:{SANDBOX_GATEWAY_PORT}").geturl()
        listeners.append((SANDBOX_GATEWAY_PORT, len(targets)))
        targets.append((_host_end(parts), parts.port or 0))
    if request.network.proxied and request.proxy_url:
        parts = _tunneled(request.proxy_url)
        listeners.append((PROXY_PORT, len(targets)))
        targets.append((_host_end(parts), parts.port or 0))
    return local_gateway, listeners, targets


def _tunneled(url: str) -> SplitResult:
    parts = urlsplit(url)
    if parts.port is None:
        raise ProviderCapabilityError(f"{url!r} has no explicit port to tunnel")
    return parts


def _host_end(parts: SplitResult) -> str:
    """Where the Host dials a service that listens on every interface."""
    host = parts.hostname or "127.0.0.1"
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


class TunneledSandbox(Sandbox):
    """Guestd serves commands and files on independent connections."""

    _client: GuestClient
    _file_client: GuestClient
    _local_gateway: str | None
    _sealed: bool
    _relay: str | None

    def _as(self, identity: Identity) -> str | None:
        return self.agent_user if identity is Identity.AGENT else None

    @property
    def gateway_url(self) -> str | None:
        """The Gateway on the sandbox's own loopback, where the tunnel delivers it."""
        return self._local_gateway

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        identity: Identity = Identity.FRAMEWORK,
        output_sink: ExecOutputSink | None = None,
    ) -> ExecResult:
        if self._sealed and self._relay:
            env = relay.variables(self._relay, "127.0.0.1") | (env or {})
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code, stdout, stderr, timed_out = await self._client.exec(
            argv,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            run_as=self._as(identity),
            output_sink=output_sink,
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((loop.time() - started) * 1000),
            timed_out=timed_out,
        )

    async def write_file(
        self, path: PurePosixPath | str, data: bytes, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.write_file(str(path), data, run_as=self._as(identity))

    # File reads have their own guestd session, so they proceed while a command runs.

    async def read_file(
        self, path: PurePosixPath | str, *, offset: int = 0, length: int | None = None
    ) -> bytes:
        return await self._file_client.read_file(str(path), offset=offset, length=length)

    async def list_files(self, path: str) -> tuple[SandboxFile, ...]:
        return await self._file_client.list_files(path)

    async def file_digest(self, path: str, *, length: int) -> str:
        return await self._file_client.file_digest(path, length=length)

    async def upload_dir(
        self, source: str, target: PurePosixPath | str, *, identity: Identity = Identity.FRAMEWORK
    ) -> None:
        await self._client.upload_dir(source, str(target), run_as=self._as(identity))

    async def screenshot(self) -> bytes:
        return await self._client.screenshot()

    async def inject_input(self, actions: Sequence[object]) -> int:
        # Accept either raw dicts or DesktopAction models, so callers need no adapter.
        payload = [
            action if isinstance(action, dict) else action.model_dump(mode="json")  # type: ignore[union-attr]
            for action in actions
        ]
        return await self._client.inject_input(payload)
