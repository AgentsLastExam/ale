"""Host bootstrap safety; no software installation or services in the unit suite."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = importlib.util.spec_from_file_location(
    "setup_macos", Path(__file__).parents[2] / "scripts/setup_macos.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def test_check_missing_does_not_create_state(tmp_path):
    root = tmp_path / "absent"
    assert setup.main(["--state-dir", str(root), "--check"]) == 1
    assert not root.exists()


def test_rejects_foreign_directory(tmp_path):
    (tmp_path / "important.txt").write_text("keep")
    with pytest.raises(setup.SetupError, match="owned"):
        setup.load_state(tmp_path)
    assert (tmp_path / "important.txt").read_text() == "keep"


def test_config_contains_names_not_credentials(tmp_path):
    state = {"port": 10443, "worker": "ale-local-test"}
    setup.write_config(tmp_path, state, "secret-test-value")
    config = (tmp_path / "run.toml").read_text()
    assert "secret-test-value" not in config
    assert "gateway_host" not in config
    assert (tmp_path / "credentials.env").stat().st_mode & 0o777 == 0o600


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
    state = setup.initialize(root, None, None)
    args = commands["worker"]
    assert args[args.index("--name") + 1] == state["worker"]
    assert "--labels" not in args
    assert state["worker"] not in (root / "run.toml").read_text()


def test_setup_starts_with_no_arguments_using_default_state_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(setup, "initialize", lambda *args: calls.append(args))
    assert setup.main([]) == 0
    assert calls == [(tmp_path / ".local/share/ale/macos", None, None)]


def test_setup_help_has_no_gateway_option(capsys):
    with pytest.raises(SystemExit) as result:
        setup.main(["--help"])
    assert result.value.code == 0
    assert "gateway-host" not in capsys.readouterr().out
