"""A Windows guestd serves the desktop of its agent, an account that need not be its own.

The Windows calls (the token's account, the system's ``os.name``) are injected, so each rule
is checked on any Host; the VM suites check them against a real Windows.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

from ale.run.guestd import gui, main, windows

pytestmark = pytest.mark.unit

_REPLY = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {}}}) + "\n"
LANE_A, LANE_B = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"


@pytest.fixture
def system_serving_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """guestd runs as the system in the session of the account ``laneA``."""
    monkeypatch.setenv("ALE_AGENT_USER", "laneA")
    monkeypatch.setenv("ALE_AGENT_HOME", r"C:\Users\laneA")
    monkeypatch.setenv("ALE_AGENT_SUDO", "0")
    monkeypatch.setenv("USERNAME", "laneA")  # the session's environment, not guestd's account
    monkeypatch.setitem(sys.modules, "gui", gui)
    monkeypatch.setattr(gui, "token_account", lambda token=None: "SYSTEM")


def test_the_driver_pipe_is_named_after_the_desktops_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALE_CUA_DRIVER_SOCKET", raising=False)
    monkeypatch.setattr(gui, "token_account", lambda token=None: "user")
    monkeypatch.setenv("ALE_AGENT_USER", "laneA")
    with monkeypatch.context() as windows_host:
        windows_host.setattr(gui.os, "name", "nt")
        named = gui._socket()
    assert named == r"\\.\pipe\ale-cua-driver-laneA"
    # The agent's own MCP server may not be told the account; it runs as that account.
    monkeypatch.delenv("ALE_AGENT_USER")
    with monkeypatch.context() as windows_host:
        windows_host.setattr(gui.os, "name", "nt")
        named = gui._socket()
    assert named == r"\\.\pipe\ale-cua-driver-user"


@pytest.mark.usefixtures("system_serving_lane")
def test_guestd_calls_another_accounts_driver_with_that_accounts_desktop_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = r"C:\ProgramData\ALE\cua-driver\cua-driver.exe"
    runs: list[tuple[list[str], str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        runs.append((command, str(options["input"])))
        return subprocess.CompletedProcess(command, 0, _REPLY, "")

    monkeypatch.setattr(gui, "_binary", lambda: driver)
    monkeypatch.setattr(gui.subprocess, "run", run)
    monkeypatch.setattr(gui, "pipe_server", lambda pipe, token=None: LANE_A)
    monkeypatch.setattr(gui, "token_sid", lambda token=None: LANE_A)
    pipe = r"\\.\pipe\ale-cua-driver-laneA-0123456789abcdef"
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", pipe)  # as a Station tells guestd
    with monkeypatch.context() as windows_host:
        windows_host.setattr(gui.os, "name", "nt")
        gui._call("get_screen_size")
        windows_host.setattr(gui, "token_account", lambda token=None: "LANEA")
        gui._call("get_screen_size")  # guestd is the desktop's account: no launcher
    (launched, given), (direct, _) = runs
    launcher = os.path.join(os.path.dirname(os.path.abspath(gui.__file__)), "windows.py")
    assert launched == [sys.executable, "-I", "-S", launcher]
    request, *messages = given.splitlines()
    assert json.loads(request)["argv"] == direct == [driver, "mcp", "--socket", pipe]
    # The launcher checks the pipe as the desktop's account, and gives the client that
    # account's environment, never guestd's.
    assert (json.loads(request)["pipe"], json.loads(request)["env"]) == (pipe, {})
    assert [json.loads(message).get("method") for message in messages] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


@pytest.mark.usefixtures("system_serving_lane")
def test_guestd_knows_its_account_by_its_token_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as windows_host:
        windows_host.setattr(main.os, "name", "nt")
        health = main.Handler(lambda event: None).dispatch(1, "health", {})["data"]
    assert (health["os"], health["user"], health["agent_user"]) == ("windows", "SYSTEM", "laneA")
    assert health["agent_home"] == r"C:\Users\laneA"


@pytest.mark.usefixtures("system_serving_lane")
def test_guestd_runs_commands_for_the_desktops_agent_and_no_other_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = []
    with monkeypatch.context() as windows_host:
        windows_host.setattr(main.os, "name", "nt")
        assert main._drop_to(None, {}) == {}  # framework work, as guestd itself
        assert main._drop_to("LaneA", {}) == {}  # the agent, through windows.py
        for other in ("laneB", "SYSTEM"):
            with pytest.raises(main.ProtocolError) as refusal:
                main._drop_to(other, {})
            refused.append(refusal.value.code)
    assert refused == [main.ERR_UNSUPPORTED] * 2


@pytest.mark.usefixtures("system_serving_lane")
def test_only_a_guestd_that_runs_as_the_agent_elevates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An elevated agent command runs with guestd's own token, which must be the agent's."""
    monkeypatch.setenv("ALE_AGENT_SUDO", "1")
    with monkeypatch.context() as windows_host:
        windows_host.setattr(main.os, "name", "nt")
        with pytest.raises(main.ProtocolError, match="elevated agent"):
            main._drop_to("laneA", {})
        windows_host.setattr(gui, "token_account", lambda token=None: "laneA")
        assert main._drop_to("laneA", {}) == {}


def test_the_launcher_borrows_only_the_agents_desktop_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = {"laneA's shell": "LaneA", "laneB's shell": "laneB"}
    monkeypatch.setattr(windows, "token_account", owners.__getitem__)
    monkeypatch.setenv("ALE_AGENT_USER", "laneA")
    windows._require_agent("laneA's shell")
    with pytest.raises(PermissionError, match="belongs to 'laneB'"):
        windows._require_agent("laneB's shell")


def test_the_launcher_leaves_the_rest_of_stdin_to_the_command() -> None:
    incoming, sending = os.pipe()
    try:
        os.write(sending, json.dumps({"argv": ["cua-driver.exe", "mcp"]}).encode() + b"\n")
        os.write(sending, b'{"jsonrpc": "2.0"}\n')
        os.close(sending)
        assert windows._request(incoming) == {"argv": ["cua-driver.exe", "mcp"]}
        assert os.read(incoming, 100) == b'{"jsonrpc": "2.0"}\n'
    finally:
        os.close(incoming)


def test_a_driver_pipe_another_account_owns_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a machine several agents share, a pipe name that went free when a Driver exited
    may be another account's by the time it is called: it would take the desktop's input
    and give it screenshots of its own."""
    monkeypatch.setattr(gui, "token_sid", lambda token=None: LANE_A)
    servers = {"own": LANE_A, "elevated": "S-1-5-32-544", "squatted": LANE_B, "none": None,
               "untold": ""}  # fmt: skip
    monkeypatch.setattr(gui, "pipe_server", lambda pipe, token=None: servers[pipe])
    for pipe in ("own", "elevated", "none"):
        gui.require_own_pipe(pipe)
    with pytest.raises(gui.GuiUnavailable, match=f"served by {LANE_B}"):
        gui.require_own_pipe("squatted")
    with pytest.raises(gui.GuiUnavailable, match="an account it cannot tell"):
        gui.require_own_pipe("untold")


def test_the_agents_own_client_finds_the_pipe_its_account_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI may pass its MCP servers none of the variables the agent's commands have."""
    monkeypatch.delenv("ALE_CUA_DRIVER_SOCKET", raising=False)
    monkeypatch.delenv("ALE_AGENT_USER", raising=False)
    monkeypatch.setattr(gui, "token_account", lambda token=None: "laneA")
    monkeypatch.setattr(gui, "token_sid", lambda token=None: LANE_A)
    names = ["ale-cua-driver-laneA-0000", "ale-cua-driver-laneAA-1111", "ale-cua-driver-laneA-ffff"]
    owners = {names[0]: LANE_B, names[1]: "S-1-5-21-1-2-3-1027", names[2]: LANE_A}
    monkeypatch.setattr(gui.os, "listdir", lambda path: ["other", *names])
    servers = {f"\\\\.\\pipe\\{name}": owner for name, owner in owners.items()}
    monkeypatch.setattr(gui, "pipe_server", lambda pipe, token=None: servers[pipe])
    with monkeypatch.context() as windows_host:
        windows_host.setattr(gui.os, "name", "nt")
        assert gui._socket() == r"\\.\pipe\ale-cua-driver-laneA-ffff"
        servers[rf"\\.\pipe\{names[2]}"] = LANE_B  # the name went free and B took it
        assert gui._socket() == r"\\.\pipe\ale-cua-driver-laneA"


def test_the_agents_environment_takes_the_requests_variables_in_any_case() -> None:
    own = {"Path": r"C:\Users\laneA\bin;C:\Windows", "TEMP": r"C:\Users\laneA\Temp"}
    merged = windows.merged(own, {"PATH": r"C:\tools", "ALE_GATEWAY_URL": "http://x"})
    assert merged == {
        "TEMP": r"C:\Users\laneA\Temp",
        "PATH": r"C:\tools",
        "ALE_GATEWAY_URL": "http://x",
    }


@pytest.mark.usefixtures("system_serving_lane")
def test_a_limited_agent_command_gets_none_of_guestds_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guestd may run as the system; the agent's command gets its own account's
    environment, which ``windows.py`` asks Windows for, and the request's variables."""
    monkeypatch.setenv("TEMP", r"C:\ProgramData\ALE\temp\laneA")
    monkeypatch.setenv("ALE_CUA_DRIVER_SOCKET", r"\\.\pipe\ale-cua-driver-laneA-0123")
    sent: list[dict[str, object]] = []

    class Launched:
        def __init__(self, argv: list[str], **options: object) -> None:
            self.argv, self.pid, self.returncode = argv, 1, 0
            self.stdin = self
            self.stdout, self.stderr = io.BytesIO(), io.BytesIO()

        def write(self, data: bytes) -> None:
            sent.append(json.loads(data))

        def __enter__(self) -> Launched:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(main.subprocess, "Popen", Launched)
    monkeypatch.setattr(main.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(main, "Path", pathlib.PurePosixPath)  # what os.name says otherwise
    with monkeypatch.context() as windows_host:
        windows_host.setattr(main.os, "name", "nt")
        main.Handler(lambda event: None).dispatch(
            1, "exec", {"argv": ["python.exe", "x.py"], "run_as": "laneA", "env": {"A": "1"}}
        )
    (request,) = sent
    assert request["argv"] == ["python.exe", "x.py"]  # found on the agent's own PATH
    assert request["env"] == {
        "ALE_CUA_DRIVER_SOCKET": r"\\.\pipe\ale-cua-driver-laneA-0123",
        "A": "1",
    }


@pytest.mark.usefixtures("system_serving_lane")
def test_a_read_whose_path_led_through_a_link_once_opened_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Windows has no O_NOFOLLOW: a directory swapped for a junction after the check is
    caught by the opened handle's own path."""
    (tmp_path / "log.txt").write_bytes(b"another Lane's")
    monkeypatch.setattr(main, "_opened_at", lambda descriptor, path: False)
    monkeypatch.setattr(main, "_refuse_links", lambda path: None)  # the swap came after it
    with monkeypatch.context() as windows_host:
        windows_host.setattr(main.os, "name", "nt")
        with (
            pytest.raises(main.ProtocolError, match="passes through a link"),
            main._open_regular(tmp_path / "log.txt"),
        ):
            pass
