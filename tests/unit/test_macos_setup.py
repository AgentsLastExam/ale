"""Host bootstrap safety; no software installation or services in the unit suite."""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = importlib.util.spec_from_file_location(
    "setup_macos", Path(__file__).parents[2] / "scripts/setup_macos.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


@pytest.fixture(autouse=True)
def no_root_changes(monkeypatch):
    """Nothing here may run sudo; the framer tests exercise it with a fake runner."""
    changes = []
    monkeypatch.setattr(setup, "install_framer", lambda: changes.append("install"))
    monkeypatch.setattr(setup, "remove_framer", lambda: changes.append("remove"))
    return changes


def test_check_missing_does_not_create_state(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setattr(setup, "STATE_DIR", root)
    assert setup.main(["--check"]) == 1
    assert not root.exists()


def test_rejects_foreign_directory(tmp_path):
    (tmp_path / "important.txt").write_text("keep")
    with pytest.raises(setup.SetupError, match="owned"):
        setup.load_state(tmp_path)
    assert (tmp_path / "important.txt").read_text() == "keep"


def test_the_provider_reads_the_state_setup_writes():
    from ale.run.providers.orchard_client import STATE_DIR

    assert setup.STATE_DIR == STATE_DIR


def test_cleanup_refuses_vms_before_stopping_processes(tmp_path, monkeypatch):
    state = {"owner": setup.OWNER, "processes": {"controller": {"pid": 42}}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(setup, "api", lambda *args: [{"name": "task-still-here"}])
    monkeypatch.setattr(setup, "stop_process", lambda record: pytest.fail("must not stop"))
    with pytest.raises(setup.SetupError, match="VM"):
        setup.cleanup(tmp_path, state)


def test_cleanup_does_not_kill_reused_pid(monkeypatch):
    monkeypatch.setattr(setup, "process_matches", lambda record: False)
    monkeypatch.setattr(setup.os, "kill", lambda *args: pytest.fail("must not signal"))
    setup.stop_process({"pid": 42})


def test_download_checksum_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "download", lambda url, path: path.write_bytes(b"wrong"))
    with pytest.raises(setup.SetupError, match="checksum"):
        setup.install_binary(tmp_path, "orchard")
    assert not (tmp_path / "bin/orchard").exists()


@pytest.mark.parametrize("running", [False, True])
def test_cleanup_preserves_local_vm_disks_even_without_controller(tmp_path, monkeypatch, running):
    disk = tmp_path / "tart-home/vms/task/disk.img"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"retained task data")
    state = {"processes": {"controller": {"pid": 42}}}
    monkeypatch.setattr(setup, "process_matches", lambda record: running)
    monkeypatch.setattr(setup, "api", lambda *args: [])
    monkeypatch.setattr(setup, "stop_process", lambda record: pytest.fail("must not stop"))
    with pytest.raises(setup.SetupError, match="contains VMs"):
        setup.cleanup(tmp_path, state)
    assert disk.read_bytes() == b"retained task data"


def test_cleanup_removes_the_images_ale_cached(tmp_path, monkeypatch):
    root = tmp_path / "state"
    for name in ("ale-framed-0123", "ale-built-4567"):
        (root / "tart-home/vms" / name).mkdir(parents=True)
    state = {"processes": {"controller": {"pid": 42}, "worker": {"pid": 43}}}
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(setup, "api", lambda *args: [])
    stopped = []
    monkeypatch.setattr(setup, "stop_process", lambda record: stopped.append(record["pid"]))
    setup.cleanup(root, state)
    assert stopped == [43, 42]
    assert not root.exists()


@pytest.mark.parametrize("stamp", ["2000-01-01T00:00:00Z", None, "bad", "2026-01-01T00:00:00"])
def test_ready_rejects_stale_or_invalid_heartbeat(tmp_path, monkeypatch, stamp):
    state = {"worker": "local", "processes": {"controller": {"pid": 1}, "worker": {"pid": 2}}}
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(
        setup,
        "api",
        lambda *args: [
            {
                "name": "local",
                "runtime": "tart",
                "arch": "arm64",
                "last_seen": stamp,
            }
        ],
    )
    assert not setup.ready(tmp_path, state)


@pytest.mark.parametrize("roles", [{}, {"controller": {"pid": 1}}])
def test_ready_requires_both_owned_services(tmp_path, monkeypatch, roles):
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(setup, "api", lambda *args: pytest.fail("missing process"))
    assert not setup.ready(tmp_path, {"processes": roles})


def test_ready_accepts_fresh_worker_with_live_services(tmp_path, monkeypatch):
    state = {"worker": "local", "processes": {"controller": {"pid": 1}, "worker": {"pid": 2}}}
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(
        setup,
        "api",
        lambda *args: [
            {
                "name": "local",
                "runtime": "tart",
                "arch": "arm64",
                "last_seen": datetime.now(UTC).isoformat(),
            }
        ],
    )
    assert setup.ready(tmp_path, state)
    monkeypatch.setattr(setup, "process_matches", lambda record: record["pid"] != 2)
    assert not setup.ready(tmp_path, state)


@pytest.mark.parametrize("version", ["0.54.0", "0.56.0"])
def test_explicit_orchard_version_must_match_pin(tmp_path, monkeypatch, version):
    program = tmp_path / "orchard"
    program.touch()
    monkeypatch.setattr(setup, "run", lambda *args: "orchard version " + version)
    monkeypatch.setattr(setup, "install_binary", lambda *args: pytest.fail("explicit must fail"))
    with pytest.raises(setup.SetupError, match="must be version"):
        setup.binary(tmp_path, "orchard", str(program))


def test_pinned_orchard_is_reused(tmp_path, monkeypatch):
    program = tmp_path / "orchard"
    program.touch()
    monkeypatch.setattr(setup, "run", lambda *args: "orchard version 0.55.0-3083b54")
    monkeypatch.setattr(setup, "install_binary", lambda *args: pytest.fail("already installed"))
    assert setup.binary(tmp_path, "orchard", str(program)) == program


def test_initialize_leaves_task_placement_to_orchard(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "binary", lambda root, name, explicit: tmp_path / name)

    def command(argv, **kwargs):
        if argv[0] == "/usr/bin/openssl":
            (root / "controller.key").touch()
        return "fake-bootstrap-token"

    monkeypatch.setattr(setup, "run", command)
    commands = {}
    monkeypatch.setattr(
        setup, "start_process", lambda root, state, role, argv, *args: commands.update({role: argv})
    )
    monkeypatch.setattr(setup, "wait_for", lambda *args: None)
    setup.initialize(root, None, None)
    state = setup.load_state(root)
    args = commands["worker"]
    assert args[args.index("--name") + 1] == state["worker"]
    assert "--labels" not in args
    assert not (root / "run.toml").exists() and not (root / "credentials.env").exists()


def test_setup_starts_with_no_arguments_in_its_state_directory(
    tmp_path, monkeypatch, no_root_changes
):
    monkeypatch.setattr(setup, "STATE_DIR", tmp_path / "state")
    calls = []
    monkeypatch.setattr(setup, "initialize", lambda *args: calls.append(args))
    assert setup.main([]) == 0
    assert calls == [(tmp_path / "state", None, None)]
    assert no_root_changes == ["install"]


def test_setup_stays_the_parent_of_the_worker_it_started(tmp_path, monkeypatch):
    class Worker:
        waited = False

        def wait(self):
            self.waited = True

    worker = Worker()
    monkeypatch.setattr(setup, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(setup, "initialize", lambda *args: worker)
    assert setup.main([]) == 0
    assert worker.waited


def test_only_the_controller_outlives_the_setup_command(tmp_path, monkeypatch):
    sessions = {}
    real = subprocess.Popen

    def popen(argv, **kwargs):
        sessions[argv[0]] = kwargs["start_new_session"]
        return real(["true"], stdin=subprocess.DEVNULL)

    monkeypatch.setattr(setup.subprocess, "Popen", popen)
    monkeypatch.setattr(setup, "process_info", lambda pid: ("", ""))
    state = {"owner": setup.OWNER, "processes": {}}
    setup.start_process(tmp_path, state, "controller", ["controller"], {})
    setup.start_process(tmp_path, state, "worker", ["worker"], {})
    assert sessions == {"controller": True, "worker": False}


def test_setup_help_has_no_gateway_or_location_option(capsys):
    with pytest.raises(SystemExit) as result:
        setup.main(["--help"])
    assert result.value.code == 0
    usage = capsys.readouterr().out
    assert "gateway-host" not in usage and "state-dir" not in usage


def framer_install(monkeypatch, tmp_path, installed: bytes | None):
    framer = tmp_path / "libexec" / "framework-macos"
    if installed is not None:
        framer.parent.mkdir()
        framer.write_bytes(installed)
        (tmp_path / "rule").write_text("rule\n")
    monkeypatch.setattr(setup, "FRAMER", framer)
    monkeypatch.setattr(setup, "FRAMER_RULE", tmp_path / "rule")
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "run", lambda argv: "tester\n")
    commands = []

    def sudo(argv, check):
        assert argv[0] == "/usr/bin/sudo"
        if argv[1] == "/usr/sbin/visudo":
            commands.append(("rule", Path(argv[-1]).read_text()))
        commands.append(tuple(argv[1:2]) + tuple(argv[-2:]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup.subprocess, "run", sudo)
    return framer, commands


@pytest.mark.parametrize("installed", [None, b"#!/bin/sh\nold\n"])
def test_the_framer_and_its_sudoers_rule_are_installed_when_missing_or_stale(
    tmp_path, monkeypatch, installed
):
    monkeypatch.undo()
    framer, commands = framer_install(monkeypatch, tmp_path, installed)
    setup.install_framer()
    assert commands[0] == ("rule", f"tester ALL=(root) NOPASSWD: {framer}\n")
    assert [command[-1] for command in commands[2:]] == [
        str(framer.parent),
        str(framer),
        str(tmp_path / "rule"),
    ]
    assert commands[3][-2] == str(setup.FRAMER_SOURCE)


def test_a_current_framer_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.undo()
    _, commands = framer_install(monkeypatch, tmp_path, setup.FRAMER_SOURCE.read_bytes())
    setup.install_framer()
    assert commands == []


def test_the_framer_is_installed_only_on_an_apple_silicon_mac(tmp_path, monkeypatch):
    monkeypatch.undo()
    framer_install(monkeypatch, tmp_path, None)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    with pytest.raises(setup.SetupError, match="Apple Silicon"):
        setup.install_framer()
