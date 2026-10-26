"""A TCP guest service obeys only the framework that claimed it first."""

from __future__ import annotations

import json
import socket
import socketserver
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ale.core.errors import ProviderStartError
from ale.run.guestd.main import Claim, _Server, _TCPHandler, serve_stream
from ale.run.transport import TcpTransport

pytestmark = pytest.mark.unit

TOKEN = "t" * 43
OTHER = "o" * 43


@contextmanager
def guest(
    claim: Claim | None = None,
    handler: type[socketserver.BaseRequestHandler] = _TCPHandler,
) -> Iterator[tuple[str, int]]:
    with _Server(("127.0.0.1", 0), handler, claim) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address  # type: ignore[misc]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def exchange(address: tuple[str, int], *lines: dict[str, object]) -> list[dict[str, object]]:
    with socket.create_connection(address, timeout=2) as client:
        client.settimeout(2)
        stream = client.makefile("rb")
        replies = []
        for line in lines:
            try:
                client.sendall(json.dumps(line).encode() + b"\n")
                raw = stream.readline()
            except (ConnectionResetError, BrokenPipeError):
                # A request sent after refusal can race with the server closing TCP.
                if not replies or replies[-1].get("error", {}).get("code") != "unauthorized":
                    raise
                break
            if not raw:
                break
            replies.append(json.loads(raw))
        return replies


def auth(token: str) -> dict[str, object]:
    return {"id": 1, "op": "auth", "params": {"token": token}}


HEALTH = {"id": 2, "op": "health", "params": {}}


def test_an_unauthenticated_operation_is_refused_and_the_connection_closed() -> None:
    with guest() as address:
        replies = exchange(address, HEALTH, HEALTH)
    assert len(replies) == 1
    assert replies[0]["ok"] is False
    assert replies[0]["error"]["code"] == "unauthorized"  # type: ignore[index]


@pytest.mark.parametrize("refused", [True, False])
@pytest.mark.parametrize("disconnect", [ConnectionResetError, BrokenPipeError])
def test_exchange_accepts_disconnect_only_after_explicit_refusal(
    monkeypatch: pytest.MonkeyPatch, refused: bool, disconnect: type[OSError]
) -> None:
    reply = {"ok": False, "error": {"code": "unauthorized"}} if refused else {"ok": True}
    client = MagicMock()
    reader = client.makefile.return_value
    reader.readline.side_effect = [json.dumps(reply).encode(), disconnect()]
    if disconnect is BrokenPipeError:
        client.sendall.side_effect = [None, disconnect()]
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext(client))
    if refused:
        assert exchange(("127.0.0.1", 1), auth(OTHER), HEALTH) == [reply]
    else:
        with pytest.raises(disconnect):
            exchange(("127.0.0.1", 1), auth(TOKEN), HEALTH)


def test_the_first_token_claims_the_guest_and_only_it_is_accepted_later() -> None:
    with guest() as address:
        claimed = exchange(address, auth(TOKEN), HEALTH)
        intruder = exchange(address, auth(OTHER), HEALTH)
        returning = exchange(address, auth(TOKEN), HEALTH)
    assert [reply["ok"] for reply in claimed] == [True, True]
    assert len(intruder) == 1 and intruder[0]["ok"] is False
    assert intruder[0]["error"]["code"] == "unauthorized"
    assert [reply["ok"] for reply in returning] == [True, True]


def test_a_short_token_never_claims() -> None:
    with guest() as address:
        assert exchange(address, auth("short"))[0]["ok"] is False
        assert exchange(address, auth(TOKEN))[0]["ok"] is True


def test_a_claim_survives_a_service_restart_through_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "run" / "guestd-token"
    assert Claim(path).admit(TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    restarted = Claim(path)
    assert not restarted.admit(OTHER)
    assert restarted.admit(TOKEN)


def test_an_unpersistable_claim_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    assert not Claim(blocker / "guestd-token").admit(TOKEN)


async def test_the_host_transport_authenticates_and_fails_closed_on_another_token() -> None:
    with guest() as (host, port):
        owner = TcpTransport(host, port, token=TOKEN)
        await owner.start(timeout_sec=5, interval_sec=0.05)
        await owner.close()

        started = time.monotonic()
        with pytest.raises(ProviderStartError, match="refused the framework token"):
            await TcpTransport(host, port, token=OTHER).start(timeout_sec=30, interval_sec=0.05)
        assert time.monotonic() - started < 5


async def test_a_guest_without_authentication_is_rejected_as_outdated() -> None:
    class OutdatedHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            writer = self.wfile

            class Text:
                @staticmethod
                def write(text: str) -> None:
                    writer.write(text.encode())

                @staticmethod
                def flush() -> None:
                    writer.flush()

            serve_stream(self.rfile, Text())  # type: ignore[arg-type]

    with (
        guest(handler=OutdatedHandler) as (host, port),
        pytest.raises(ProviderStartError, match="predates authenticated connections"),
    ):
        await TcpTransport(host, port, token=TOKEN).start(timeout_sec=5, interval_sec=0.05)
