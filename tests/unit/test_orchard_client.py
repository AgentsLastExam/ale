"""Orchard boundary tests use only in-process fakes."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import aiohttp
import pytest

from ale.core.errors import GuestUnreachableError, ProviderStartError
from ale.run.providers.orchard_client import OrchardClient, OrchardTransport

pytestmark = pytest.mark.unit


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send_str(self, value):
        self.sent.append(json.loads(value))

    async def receive(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True

    def frame(self, value):
        self.messages.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(value), None))

    def output(self, value):
        self.frame({"type": "stdout", "data": base64.b64encode(value).decode()})


async def test_fragmented_lines_and_chunked_writes():
    socket = Socket()
    transport = OrchardTransport(socket)
    socket.output(b'{"hello":')
    socket.output(b'"world"}\nnext\n')
    assert await transport.recv() == '{"hello":"world"}'
    assert await transport.recv() == "next"
    await transport.send(b"x" * 40000)
    chunks = [base64.b64decode(item["data"]) for item in socket.sent]
    assert max(map(len, chunks)) <= 16384
    assert b"".join(chunks) == b"x" * 40000
    await transport.close()
    assert socket.closed


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "stdout", "data": "invalid base64!"},
        {"type": "error", "message": "secret remote content"},
        {"type": "exit", "exit": {"code": 0}},
    ],
)
async def test_protocol_or_exit_failure_is_safe(frame):
    socket = Socket()
    transport = OrchardTransport(socket)
    socket.frame(frame)
    with pytest.raises(GuestUnreachableError) as error:
        await transport.recv()
    assert "secret" not in str(error.value)
    with pytest.raises(GuestUnreachableError) as later:
        await transport.send(b"next request\n")
    assert str(later.value) == str(error.value)
    await transport.close()


async def test_line_limit_and_queue_limit_fail_explicitly():
    socket = Socket()
    transport = OrchardTransport(socket, max_line_bytes=8, max_buffer_bytes=16)
    socket.output(b"123456789")
    with pytest.raises(GuestUnreachableError, match="limit"):
        await transport.recv()
    await transport.close()

    socket = Socket()
    transport = OrchardTransport(socket, max_line_bytes=8, max_buffer_bytes=8)
    socket.output(b"a\nb\nc\nd\ne\n")
    with pytest.raises(GuestUnreachableError, match="limit"):
        await transport.recv()
    await transport.close()


async def test_close_unblocks_receive():
    socket = Socket()
    transport = OrchardTransport(socket)
    waiting = asyncio.create_task(transport.recv())
    await asyncio.sleep(0)
    await transport.close()
    with pytest.raises(GuestUnreachableError):
        await waiting


class Response:
    def __init__(self, status, data):
        self.status, self.data = status, data
        encoded = b"" if data is None else json.dumps(data).encode()
        self.content = self
        self._body = encoded

    async def read(self, size):
        result, self._body = self._body[:size], self._body[size:]
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.close = AsyncMock()

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return next(self.responses)


async def test_rest_lifecycle_missing_and_no_mutation_retry():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    session = Session(
        [
            Response(404, {}),
            Response(200, {"name": "vm", "uid": "uid"}),
            Response(204, None),
            Response(503, {"secret": "body"}),
        ]
    )
    client._session = session
    assert await client.get_vm("vm") is None
    assert (await client.create_vm({"name": "vm"}))["uid"] == "uid"
    await client.delete_vm("vm")
    with pytest.raises(ProviderStartError) as error:
        await client.create_vm({"name": "vm"})
    assert "secret" not in str(error.value)
    assert len(session.requests) == 4
    assert session.requests[0][1] == "https://orchard.invalid/v1/vms/vm"
    assert session.requests[0][2]["timeout"].total == 30
    await client.close()


@pytest.mark.parametrize(
    "url", ["http://orchard.invalid", "https://user:password@host", "https://host?token=secret"]
)
def test_credentials_require_verified_https(url):
    with pytest.raises(ValueError):
        OrchardClient(url, username="fake", password="secret")


async def test_bootstrap_exec_drains_output_and_preserves_binary_stdin():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    socket = Socket()
    client._socket = AsyncMock(return_value=socket)
    socket.output(b"hello")
    socket.frame({"type": "stderr", "data": base64.b64encode(b"warning").decode()})
    socket.frame({"type": "exit", "exit": {"code": 7}})
    result = await client.exec("vm", ["python3", "-"], stdin=b"x" * 70000)
    assert result.exit_code == 7
    assert result.stdout == b"hello"
    assert result.stderr == b"warning"
    assert all(len(base64.b64decode(frame["data"])) <= 16384 for frame in socket.sent)
    assert socket.sent[-1] == {"type": "stdin", "data": ""}
    assert socket.closed


async def test_exec_cancellation_and_timeout_close_without_retry():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    socket = Socket()
    client._socket = AsyncMock(return_value=socket)
    execution = asyncio.create_task(client.exec("vm", ["sleep", "20"]))
    await asyncio.sleep(0)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert socket.closed
    assert client._socket.await_count == 1

    socket = Socket()
    client._socket = AsyncMock(return_value=socket)
    with pytest.raises(GuestUnreachableError, match="unknown"):
        await client.exec("vm", ["sleep", "20"], timeout_sec=0.001)
    assert socket.closed
    assert client._socket.await_count == 1


async def test_exec_refuses_oversized_output():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    socket = Socket()
    client._socket = AsyncMock(return_value=socket)
    socket.output(b"too large")
    with pytest.raises(GuestUnreachableError, match="limit"):
        await client.exec("vm", ["read"], max_output_bytes=2)
    assert socket.closed


async def test_websocket_command_and_tls_options_are_explicit():
    client = OrchardClient(
        "https://orchard.invalid", username="fake", password="secret", server_hostname="controller"
    )
    session = Session([])
    session.ws_connect = AsyncMock(return_value=Socket())
    client._session = session
    transport = await client.connect_guest(
        "vm/name", ["python3", "/path with spaces/guestd", "--stdio"]
    )
    args, kwargs = session.ws_connect.call_args
    assert args == ("wss://orchard.invalid/v1/vms/vm%2Fname/exec",)
    assert kwargs["params"]["command"] == "python3 '/path with spaces/guestd' --stdio"
    assert kwargs["server_hostname"] == "controller"
    assert kwargs["ssl"].check_hostname
    assert kwargs["params"]["wait"] == "30"
    await transport.close()
    await client.close()


async def test_startup_connection_uses_remaining_budget_instead_of_default_30_seconds():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    async with client:
        # The session's HTTP handshake timeout must not cut short the startup budget.
        assert client._session.timeout.total is None
        with pytest.MonkeyPatch.context() as patch:
            connect = AsyncMock(return_value=Socket())
            patch.setattr(client._session, "ws_connect", connect)
            deadline = asyncio.get_running_loop().time() + 180
            transport = await client.connect_guest("vm", ["guestd"], deadline=deadline)
            assert connect.call_args.kwargs["params"]["wait"] == "180"
            await transport.close()


async def test_startup_connection_timeout_does_not_retry_command():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    session = Session([])

    async def stalled_handshake(*args, **kwargs):
        await asyncio.Event().wait()

    session.ws_connect = AsyncMock(side_effect=stalled_handshake)
    client._session = session
    with pytest.raises(GuestUnreachableError, match="connection failed"):
        await client.connect_guest(
            "vm", ["guestd"], deadline=asyncio.get_running_loop().time() + 0.01
        )
    assert session.ws_connect.await_count == 1
    await client.close()


async def test_empty_successful_delete_and_bounded_response():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    client._session = Session([Response(200, None), Response(200, {"large": "x" * 1048576})])
    await client.delete_vm("vm")
    with pytest.raises(ProviderStartError, match="size limit"):
        await client.get_vm("vm")
    await client.close()


async def test_real_session_initialization_and_redirect_refusal():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    await client.start()
    session = client._session
    assert session is not None
    assert not session.trust_env
    assert session.headers["Authorization"] == "Basic " + base64.b64encode(b"fake:secret").decode()
    assert len(session.trace_configs) == 1
    with pytest.raises(GuestUnreachableError, match="redirects"):
        await session.trace_configs[0].on_request_redirect.send(session, None, None)
    await client.start()
    assert client._session is session
    await client.close()
    assert session.closed
    await client.close()


async def test_update_vm_uses_existing_spec_endpoint_without_retry():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    session = Session(
        [
            Response(200, {"uid": "uid", "generation": 2, "powerState": "stopped"}),
            Response(503, None),
        ]
    )
    client._session = session
    spec = {"os": "darwin", "arch": "arm64", "runtime": "tart", "powerState": "stopped"}
    result = await client.update_vm("vm/name", spec)
    assert result["generation"] == 2
    assert session.requests[0][:2] == ("PUT", "https://orchard.invalid/v1/vms/vm%2Fname")
    assert session.requests[0][2]["json"] == spec
    with pytest.raises(ProviderStartError):
        await client.update_vm("vm/name", spec)
    assert len(session.requests) == 2
    await client.close()


async def test_transport_preserves_binary_upload_body_without_line_framing():
    socket = Socket()
    transport = OrchardTransport(socket, max_line_bytes=8)
    payload = b"\x00\xff\r\n" * 65536
    await transport.send(payload)
    assert b"".join(base64.b64decode(frame["data"]) for frame in socket.sent) == payload
    assert all(len(base64.b64decode(frame["data"])) <= 16384 for frame in socket.sent)
    await transport.close()


async def test_list_workers_is_authenticated_read_only_request():
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    workers = [{"name": "mac-1", "arch": "arm64", "runtime": "tart"}]
    session = Session([Response(200, workers)])
    client._session = session
    assert await client.list_workers() == workers
    assert session.requests[0][:2] == ("GET", "https://orchard.invalid/v1/workers")
    await client.close()


@pytest.mark.parametrize("response", [{"workers": []}, [None], ["worker"], None])
async def test_list_workers_rejects_invalid_response_shape(response):
    client = OrchardClient("https://orchard.invalid", username="fake", password="secret")
    client._session = Session([Response(200, response)])
    with pytest.raises(ProviderStartError, match="workers response"):
        await client.list_workers()
    await client.close()
