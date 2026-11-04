"""macOS episode wiring with an in-memory Orchard peer, never a real VM.

The guestd stdio check launches only an unprivileged local Python subprocess and
writes inside pytest's temporary directory. No model, GUI, controller or VM is used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from tests.support import edit_task_config, write_task

from ale.core.config import RunConfig, RunTimeouts
from ale.core.errors import GuestUnreachableError
from ale.core.verdict import Status
from ale.run import guestd
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import NopHarness, OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers import ProviderRegistry, orchard
from ale.run.providers.orchard import OrchardProvider
from ale.run.providers.orchard_framing import FramedImage
from ale.run.tasksets.manifest import load_tasks
from ale.run.transport import GuestClient, StdioTransport

pytestmark = pytest.mark.unit

IMAGE = "registry.invalid/macos@sha256:" + "a" * 64
OUTPUT = "/Users/user/output/result.txt"


class MemoryGuest:
    """Scripted protocol peer: commands are recognized, never executed on the host."""

    def __init__(
        self,
        *,
        fail_setup: bool = False,
        wait_oracle: bool = False,
        timeout_oracle: bool = False,
        verify_failure: str | None = None,
        shared: MemoryGuest | None = None,
        user: str = "user",
    ) -> None:
        self.user = shared.user if shared else user
        self.files: dict[str, bytes] = shared.files if shared else {}
        self.modes: dict[str, int] = shared.modes if shared else {}
        self.operations: list[tuple[str, dict[str, object]]] = shared.operations if shared else []
        self.responses: asyncio.Queue[str] = asyncio.Queue()
        self.stages: list[str] = shared.stages if shared else []
        self.privileges: list[str] = shared.privileges if shared else []
        self.closed = False
        self.fail_setup = fail_setup
        self.wait_oracle = wait_oracle
        self.timeout_oracle = timeout_oracle
        self.verify_failure = verify_failure
        self.oracle_started = asyncio.Event()
        self._upload: tuple[int, str] | None = None
        self._buffer = bytearray()
        self._pending_file: tuple[str, int] | None = None
        self._pending_ack: tuple[int, dict[str, object]] | None = None

    async def send(self, raw: bytes) -> None:
        if self.closed:
            raise GuestUnreachableError("in-memory guest closed")
        assert isinstance(raw, bytes)
        if self._upload is not None:
            self._upload_body(raw)
            return
        request = json.loads(raw)
        if request["op"] == "exec_output_ack":
            assert self._pending_ack is not None
            req_id, data = self._pending_ack
            assert request["id"] == req_id and request["seq"] == 1
            self.responses.put_nowait(json.dumps({"id": req_id, "ok": True, "data": data}))
            self._pending_ack = None
            return
        req_id, op, params = request["id"], request["op"], request["params"]
        self.operations.append((op, params))
        data: dict[str, object] = {}
        if op == "health":
            data = {
                "proto": 2,
                "os": "macos",
                "framework_uid": 0,
                "agent_uid": 501,
                "agent_user": self.user,
                "agent_home": f"/Users/{self.user}",
                "gui": False,
            }
        elif op == "disk_usage":
            data = {"total": 64 * 1024**3, "used": 1024**3, "free": 63 * 1024**3}
        elif op == "list_files":
            root = str(params["path"]) + "/"
            data = {
                "files": [
                    {"path": path, "size": len(content)}
                    for path, content in self.files.items()
                    if path.startswith(root)
                ]
            }
        elif op == "write_file":
            previous = self.files.get(params["path"], b"") if params.get("append") else b""
            self.files[params["path"]] = previous + base64.b64decode(params["b64"])
        elif op == "read_file":
            if params["path"] not in self.files:
                self.responses.put_nowait(
                    json.dumps(
                        {
                            "id": req_id,
                            "ok": False,
                            "error": {"code": "not_found", "message": "fixture file missing"},
                        }
                    )
                )
                return
            content = self.files[params["path"]][params.get("offset", 0) :]
            if "length" in params:
                content = content[: params["length"]]
            self._chunk(req_id, "chunk", content)
        elif op == "put_tree":
            self._upload = req_id, params["path"]
            if params["path"] == "/Users/user/.ale-solution":
                assert params["run_as"] == "user"
            elif params["path"].startswith("/opt/ale/"):
                assert "run_as" not in params
            return
        elif op == "remove":
            self.files.pop(params["path"], None)
        elif op == "exec":
            result = self._exec(params)
            if result is None:
                if not self.timeout_oracle:
                    return  # Simulate a still-running guest command until cancellation.
                # The guest deadline fires before ALE's host-side stop grace.
                await asyncio.sleep(params["timeout_sec"])
                data = {"exit_code": None, "timed_out": True}
            else:
                code, stdout = result
                data = {"exit_code": code, "timed_out": False}
                if stdout:
                    acknowledged = bool(params.get("output_ack"))
                    self._chunk(
                        req_id, "stdout_chunk", stdout, sequence=1 if acknowledged else None
                    )
                    if acknowledged:
                        self._pending_ack = req_id, data
                        return
        else:
            raise AssertionError(f"Unexpected guest operation: {op}")
        self.responses.put_nowait(json.dumps({"id": req_id, "ok": True, "data": data}))

    def _upload_body(self, raw: bytes) -> None:
        """Decode v2 tree headers and exact binary bodies across arbitrary chunks."""
        assert self._upload is not None
        req_id, root = self._upload
        self._buffer.extend(raw)
        while True:
            if self._pending_file is not None:
                name, length = self._pending_file
                if len(self._buffer) < length:
                    return
                self.files[str(PurePosixPath(root) / name)] = bytes(self._buffer[:length])
                del self._buffer[:length]
                self._pending_file = None
            newline = self._buffer.find(b"\n")
            if newline < 0:
                return
            entry = json.loads(bytes(self._buffer[:newline]))
            del self._buffer[: newline + 1]
            assert entry["id"] == req_id
            if entry.get("end"):
                assert not self._buffer
                self._upload = None
                self.responses.put_nowait(json.dumps({"id": req_id, "ok": True, "data": {}}))
                return
            assert "abort" not in entry
            if entry["kind"] == "file":
                self._pending_file = entry["entry"], entry["size"]
            else:
                assert entry["kind"] == "dir"

    def _chunk(self, req_id: int, event: str, data: bytes, *, sequence: int | None = None) -> None:
        payload: dict[str, object] = {"b64": base64.b64encode(data).decode()}
        if sequence is not None:
            payload["seq"] = sequence
        self.responses.put_nowait(json.dumps({"id": req_id, "event": event, "data": payload}))

    def _exec(self, params: dict[str, object]) -> tuple[int, bytes] | None:
        argv = params["argv"]
        assert isinstance(argv, list)
        cwd, env = params.get("cwd"), params.get("env", {})
        if argv[:3] == ["/usr/bin/sudo", "-l", "-U"]:
            return 0, f"User {self.user} is not allowed to run sudo on mac.\n".encode()
        if argv[:2] == ["/bin/sh", "-c"] and argv[2] in (orchard._GRANT_SUDO, orchard._REVOKE_SUDO):
            self.privileges.append("grant" if argv[2] == orchard._GRANT_SUDO else "revoke")
            return 0, b""
        if argv[:2] == ["sh", "-c"] and "$(id -u)" in argv[2]:
            return 0, b"501 no" if params.get("run_as") == self.user else b"0 yes"
        if argv == ["bash", "setup.sh"] and cwd == "/opt/ale/setup":
            assert not any(path.startswith("/opt/ale/tests/") for path in self.files)
            assert not any(".ale-solution" in path for path in self.files)
            assert env["ALE_HOME"] == "/Users/user"
            self.stages.append("setup")
            return (17 if self.fail_setup else 0), b""
        if argv == ["bash", "/Users/user/.ale-solution/solve.sh"]:
            assert params["run_as"] == "user"
            assert argv[1] in self.files
            assert not any(path.startswith("/opt/ale/tests/") for path in self.files)
            self.stages.append("oracle")
            self.oracle_started.set()
            self.files[OUTPUT] = b"hello macOS"
            self.modes[OUTPUT] = 0o640
            if self.wait_oracle:
                return None
            return 0, b"oracle finished\n"
        if argv == ["bash", "test.sh"] and cwd == "/opt/ale/tests":
            assert "run_as" not in params
            self.stages.append("verify")
            assert not any(path.startswith("/opt/ale/logs/verifier/") for path in self.files)
            score = float(self.files.get(OUTPUT) == b"hello macOS")
            self.files["/opt/ale/logs/verifier/verification.json"] = json.dumps(
                {"reward": score, "result": score}
            ).encode()
            self.files["/opt/ale/logs/verifier/reward.json"] = json.dumps(
                {"reward": score}
            ).encode()
            # The immutable host snapshot must remain the solver's original output.
            self.files[OUTPUT] = b"modified by verifier"
            if self.verify_failure == "timeout":
                return None
            return (17 if self.verify_failure == "exit" else 0), b""
        if argv[:2] == ["sh", "-c"] and argv[-1] == "/logs/verifier":
            return 1, b"mkdir: /logs: Read-only file system"
        if argv[:2] == ["sh", "-c"] and argv[-1] == "/opt/ale/logs/verifier":
            for path in tuple(self.files):
                if path.startswith("/opt/ale/logs/verifier/"):
                    del self.files[path]
            return 0, b""
        if argv[:3] == ["rm", "-rf", "--"]:
            for root in argv[3:]:
                for path in tuple(self.files):
                    if path == root or path.startswith(root + "/"):
                        del self.files[path]
            return 0, b""
        if len(argv) == 5 and argv[3] == "inspect":
            self.stages.append("capture")
            entries = (
                [{"source": OUTPUT, "kind": "file", "mode": self.modes[OUTPUT]}]
                if OUTPUT in self.files
                else []
            )
            return 0, json.dumps({"entries": entries, "skipped": [], "truncated": False}).encode()
        if len(argv) == 5 and argv[3] == "read":
            return 0, self.files[argv[4]]
        if argv[:2] == ["python3", "-c"] and "p.parent).mkdir" in argv[2]:
            assert "run_as" not in params
            self.files.pop(argv[3], None)
            return 0, b""
        if argv[:2] == ["chmod", "--"]:
            assert "run_as" not in params
            self.modes[argv[3]] = int(argv[2], 8)
            return 0, b""
        if argv[:2] == ["sh", "-c"] and "verifier" in argv[2]:
            assert argv[-2:] == ["/opt/ale/logs", "/opt/ale/logs/verifier"]
            return 0, b""
        if argv[:2] == ["sh", "-c"] and "sysconfig.get_paths()" in argv[-1]:
            return 0, b"/opt/homebrew/lib/python3.12/site-packages\n"
        if argv == ["python3", "-c", "import ale_verify"]:
            return 0, b""
        raise AssertionError(f"Unexpected guest command: {argv[0:2]}, cwd={cwd}")

    async def recv(self) -> str:
        return await self.responses.get()

    async def close(self) -> None:
        self.closed = True


class MemoryOrchard:
    def __init__(self, guest: MemoryGuest) -> None:
        self.guest = guest
        self.vm: dict[str, object] | None = None
        self.created: list[str] = []
        self.stop_requested: list[str] = []
        self.stop_observed: list[str] = []
        self.deleted: list[str] = []
        self.closed = False
        self.connections: list[MemoryGuest] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def list_workers(self) -> list[dict[str, object]]:
        return [
            {
                "name": "memory-worker",
                "arch": "arm64",
                "runtime": "tart",
                "last_seen": datetime.now(UTC).isoformat(),
            }
        ]

    async def get_vm(self, name: str) -> dict[str, object] | None:
        if self.vm and name in self.stop_requested and name not in self.stop_observed:
            self.vm.update(
                observedGeneration=2,
                conditions=[{"type": "running", "state": "false"}],
            )
            self.stop_observed.append(name)
        return dict(self.vm) if self.vm else None

    async def create_vm(self, spec: Mapping[str, object]) -> dict[str, object]:
        assert self.vm is None
        self.vm = dict(spec) | {
            "uid": "fixture-uid",
            "status": "running",
            "image_fqn": spec["image"],
            "assignedCPU": spec["cpu"],
            "assignedMemory": spec["memory"],
            "generation": 1,
            "observedGeneration": 1,
            "conditions": [{"type": "running", "state": "true"}],
        }
        self.created.append(str(spec["name"]))
        return self.vm

    async def update_vm(self, name: str, spec: Mapping[str, object]) -> dict[str, object]:
        assert self.vm and self.vm["name"] == name
        assert spec == {
            "os": "darwin",
            "arch": "arm64",
            "runtime": "tart",
            "powerState": "stopped",
        }
        self.vm.update(spec)
        self.vm["generation"] = 2
        self.stop_requested.append(name)
        # The request response does not acknowledge a worker-side stop. A later
        # GET must observe this generation before the provider can delete it.
        return dict(self.vm)

    async def delete_vm(self, name: str) -> None:
        assert self.vm and self.vm["name"] == name
        assert name in self.stop_observed
        assert self.vm["observedGeneration"] == self.vm["generation"]
        assert self.vm["conditions"] == [{"type": "running", "state": "false"}]
        self.deleted.append(name)
        self.vm = None

    async def connect_guest(
        self, name: str, argv: Sequence[str], *, deadline: float | None = None
    ) -> MemoryGuest:
        assert self.vm and self.vm["name"] == name
        connection = MemoryGuest(shared=self.guest) if self.connections else self.guest
        self.connections.append(connection)
        return connection


def episode_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guest: MemoryGuest
) -> tuple[Path, ProviderRegistry, MemoryOrchard]:
    task = write_task(
        tmp_path / "task",
        name="agentslastexam/mac-fixture",
        config={
            "artifacts": [OUTPUT],
            "environment": {"os": "macos", "vm_image": IMAGE},
            "agent": {"network_mode": "public"},
        },
        dockerfile=None,
        instruction="Write hello macOS to the output file.\n",
    )
    setup = task / "environment" / "setup" / "setup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_text("#!/bin/bash\n# In-memory phase fixture.\n")
    spec = load_tasks(task)[0].spec
    registry = ProviderRegistry(RunConfig())
    provider = registry.get(spec.image.kind, spec.os)
    assert isinstance(provider, OrchardProvider)
    controller = MemoryOrchard(guest)
    monkeypatch.setattr(provider, "_local_orchard", SimpleNamespace(client=lambda: controller))
    monkeypatch.setattr(orchard, "frame_image", _framed)
    monkeypatch.setattr(orchard, "check_framer", lambda: None)
    monkeypatch.setattr(orchard, "await_desktop", _desktop)
    return task, registry, controller


async def _framed(local, image, *, user) -> FramedImage:
    return FramedImage(
        name="ale-framed-fixture",
        identity="sha256:" + "e" * 64,
        agent_home=f"/Users/{user}",
        agent_uid=501,
    )


async def _desktop(client, *, agent_user, agent_uid, timeout_sec) -> None:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("oracle", [True, False])
async def test_orchard_episode_scores_and_captures_before_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oracle: bool,
) -> None:
    guest = MemoryGuest()
    guest.files["/opt/ale/logs/verifier/reward.json"] = b'{"reward": 1}'
    guest.files["/opt/ale/logs/verifier/verification.json"] = b'{"reward": 1}'
    task, providers, controller = episode_fixture(tmp_path, monkeypatch, guest)
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness() if oracle else NopHarness()),
        providers,
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": float(oracle)}
    resets = [
        params["argv"]
        for op, params in guest.operations
        if op == "exec" and params["argv"][-1] == "/opt/ale/logs/verifier"
    ]
    assert resets, "verification must prepare its macOS result directory"
    assert all(argv[-2] == "/opt/ale/logs" for argv in resets)
    assert guest.stages == (
        ["setup", "oracle", "capture", "verify"] if oracle else ["setup", "capture", "verify"]
    )
    # The Task declares no sudo, so the agent's sandbox takes it away at boot.
    assert guest.privileges == ["revoke"]
    artifact = result.run_dir / "artifacts/0000/result.txt"
    if oracle:
        assert artifact.read_bytes() == b"hello macOS"
    else:
        assert not artifact.exists()
    assert (result.run_dir / "verification.json").is_file()
    assert json.loads((result.run_dir / "verification.json").read_text()) == {
        "reward": float(oracle),
        "result": float(oracle),
    }
    assert (result.run_dir / "trajectory.json").is_file()
    assert controller.deleted == controller.created
    assert controller.vm is None and controller.closed and guest.closed
    assert len(controller.connections) == 2
    assert all(connection.closed for connection in controller.connections)
    assert result.record.sandboxes[0].provider == "orchard"
    assert result.record.sandboxes[0].outcome == "destroyed"


@pytest.mark.asyncio
async def test_orchard_separate_verifier_restores_solver_artifacts_and_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    solver = MemoryGuest()
    task, providers, solver_controller = episode_fixture(tmp_path, monkeypatch, solver)
    edit_task_config(
        task,
        {
            "verifier": {
                "environment": {"vm_image": IMAGE, "cpus": 2, "memory_mb": 4096},
            }
        },
    )
    verifier = MemoryGuest()
    verifier_controller = MemoryOrchard(verifier)
    spec = load_tasks(task)[0].spec
    provider = providers.get(spec.image.kind, spec.os)
    monkeypatch.setattr(
        provider,
        "_local_orchard",
        SimpleNamespace(
            client=lambda: verifier_controller if solver_controller.created else solver_controller
        ),
    )

    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
        collect_artifacts=True,
    )

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert solver_controller.created != verifier_controller.created
    assert solver.files is not verifier.files
    assert solver.stages == ["setup", "oracle", "capture"]
    assert verifier.stages == ["verify"]
    assert verifier.modes[OUTPUT] == 0o640
    assert solver.files[OUTPUT] == b"hello macOS"
    assert verifier.files[OUTPUT] == b"modified by verifier"
    assert (result.run_dir / "artifacts/0000/result.txt").read_bytes() == b"hello macOS"
    assert not any(".ale-solution" in path for path in verifier.files)
    assert not any(path.startswith("/opt/ale/setup/") for path in verifier.files)
    restored = [
        args for op, args in verifier.operations if op == "write_file" and args["path"] == OUTPUT
    ]
    assert len(restored) == 1 and "run_as" not in restored[0]
    verification = [
        args
        for op, args in verifier.operations
        if op == "exec" and args.get("cwd") == "/opt/ale/tests"
    ]
    assert len(verification) == 1 and "run_as" not in verification[0]
    for controller in (solver_controller, verifier_controller):
        assert controller.deleted == controller.created
        assert controller.vm is None and controller.closed
        assert all(connection.closed for connection in controller.connections)
    assert len(result.record.sandboxes) == 2
    assert all(sandbox.outcome == "destroyed" for sandbox in result.record.sandboxes)


@pytest.mark.asyncio
async def test_orchard_setup_failure_cleans_up_without_running_solver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = MemoryGuest(fail_setup=True)
    task, providers, controller = episode_fixture(tmp_path, monkeypatch, guest)
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness()),
        providers,
        run_dir=tmp_path / "runs",
    )
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert guest.stages == ["setup"]
    assert controller.deleted == controller.created
    assert controller.vm is None and controller.closed and guest.closed


@pytest.mark.asyncio
async def test_orchard_cancelled_episode_stops_its_owned_vm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = MemoryGuest(wait_oracle=True)
    task, providers, controller = episode_fixture(tmp_path, monkeypatch, guest)
    running = asyncio.create_task(
        run_episode(
            load_tasks(task)[0],
            EpisodeProtocol(OracleHarness()),
            providers,
            run_dir=tmp_path / "runs",
        )
    )
    try:
        await asyncio.wait_for(guest.oracle_started.wait(), timeout=5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert controller.deleted == controller.created
    assert len(controller.deleted) == 1
    assert controller.vm is None and controller.closed and guest.closed
    assert "verify" not in guest.stages


@pytest.mark.asyncio
async def test_orchard_agent_timeout_keeps_its_status_and_scores_what_it_left(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = MemoryGuest(wait_oracle=True, timeout_oracle=True)
    task, providers, controller = episode_fixture(tmp_path, monkeypatch, guest)
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness(), timeouts=RunTimeouts(agent=0.05)),
        providers,
        run_dir=tmp_path / "runs",
    )
    assert guest.oracle_started.is_set()
    assert result.verdict.status is Status.TIMEOUT
    assert result.verdict.rewards == {"reward": 1.0}
    assert controller.deleted == controller.created
    assert len(controller.deleted) == 1
    assert controller.vm is None and controller.closed and guest.closed
    assert guest.stages[-1] == "verify"


@pytest.mark.asyncio
async def test_guestd_stdio_round_trip_is_local_and_unprivileged(tmp_path: Path) -> None:
    """Exercise real guest protocol bytes without bootstrapping a host environment."""
    service = Path(guestd.__file__).with_name("main.py")
    transport = StdioTransport([sys.executable, "-I", str(service), "--stdio"])
    await transport.start()
    client = GuestClient(transport)
    try:
        health = await client.health()
        expected_os = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
        assert health["os"] == expected_os
        assert health["proto"] == 2
        payload = b"a local guest protocol check\x00\xff\n"
        target = tmp_path / "guest-file.bin"
        await client.write_file(str(target), payload)
        assert await client.read_file(str(target)) == payload
        source_tree = tmp_path / "source-tree"
        source_tree.mkdir()
        (source_tree / "binary.bin").write_bytes(payload)
        (source_tree / "empty").mkdir()
        target_tree = tmp_path / "uploaded-tree"
        await client.upload_dir(str(source_tree), str(target_tree))
        assert await client.read_file(str(target_tree / "binary.bin")) == payload
        assert (target_tree / "empty").is_dir()
        code, stdout, stderr, timed_out = await client.exec(
            [sys.executable, "-I", "-c", "import os; print(os.getcwd())"],
            cwd=str(tmp_path),
            timeout_sec=5,
        )
        assert (code, stderr, timed_out) == (0, "", False)
        assert Path(stdout.strip()).resolve() == tmp_path.resolve()
    finally:
        await client.close()


@pytest.mark.parametrize("failure", ["exit", "timeout"])
async def test_macos_verifier_failure_does_not_accept_a_written_reward(
    tmp_path, monkeypatch, failure
):
    guest = MemoryGuest(verify_failure=failure)
    task, providers, controller = episode_fixture(tmp_path, monkeypatch, guest)
    result = await run_episode(
        load_tasks(task)[0],
        EpisodeProtocol(OracleHarness(), timeouts=RunTimeouts(verify=0.05)),
        providers,
        run_dir=tmp_path / "runs",
    )
    assert "/opt/ale/logs/verifier/reward.json" in guest.files
    expected = Status.TASK_ERROR if failure == "exit" else Status.TIMEOUT
    assert result.verdict.status is expected
    assert result.verdict.rewards is None
    assert not (result.run_dir / "verification.json").exists()
    assert controller.deleted == controller.created
