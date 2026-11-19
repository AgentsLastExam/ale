"""Host bootstrap safety; no software installation or services in the unit suite."""

import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

SPEC = importlib.util.spec_from_file_location(
    "setup_macos", Path(__file__).parents[2] / "scripts/setup_macos.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


@pytest.fixture(autouse=True)
def no_root_changes(monkeypatch):
    """Nothing here may run sudo; the framer and Softnet tests exercise it with a fake runner."""
    changes = []
    monkeypatch.setattr(setup, "install_framer", lambda: changes.append("install"))
    monkeypatch.setattr(setup, "install_softnet", lambda: changes.append("softnet"))
    monkeypatch.setattr(setup, "remove_installed", lambda: changes.append("remove"))
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
    from ale.run.providers.orchard.client import STATE_DIR

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
    monkeypatch.setattr(setup, "run", lambda *args: "orchard version 0.58.0-3083b54")
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
    assert no_root_changes == ["install", "softnet"]


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
    started = []
    real = subprocess.Popen

    # Each stand-in runs until the test ends: start_process fails a process that has already
    # exited, so one that exits at once would race its check.
    def popen(argv, **kwargs):
        sessions[argv[0]] = kwargs["start_new_session"]
        started.append(real(["sleep", "60"], stdin=subprocess.DEVNULL))
        return started[-1]

    monkeypatch.setattr(setup.subprocess, "Popen", popen)
    monkeypatch.setattr(setup, "process_info", lambda pid: ("", ""))
    state = {"owner": setup.OWNER, "processes": {}}
    try:
        setup.start_process(tmp_path, state, "controller", ["controller"], {})
        setup.start_process(tmp_path, state, "worker", ["worker"], {})
    finally:
        for process in started:
            process.kill()
            process.wait()
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


def _earlier_setup(tmp_path, monkeypatch, binaries, given):
    root = tmp_path / "infra"
    root.mkdir(mode=0o700)
    state = {
        "owner": setup.OWNER,
        "port": 1,
        "worker": "local",
        "processes": {},
        "binaries": binaries,
        "given": given,
    }
    setup.save_state(root, state)
    setup.private_write(root / "password", "secret")
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "run", lambda argv, **kwargs: (root / "controller.key").touch())
    monkeypatch.setattr(setup, "start_process", lambda *args: None)
    monkeypatch.setattr(setup, "wait_for", lambda *args: None)
    monkeypatch.setattr(setup, "SOFTNET", tmp_path / "softnet")
    return root


def test_an_earlier_setups_executables_are_found_again_unless_they_were_given(
    tmp_path, monkeypatch
):
    """An executable given on the command line is kept to; its own copy, or one found on
    PATH, is found again, so another pin replaces it."""
    root = _earlier_setup(
        tmp_path,
        monkeypatch,
        {"orchard": "/opt/homebrew/bin/orchard", "tart": "/opt/tart"},
        {"orchard": False, "tart": True},
    )
    events = []

    def binary(root, name, explicit):
        events.append((name, explicit))
        return tmp_path / name

    monkeypatch.setattr(setup, "binary", binary)
    monkeypatch.setattr(setup, "stop_other_orchard", lambda *args: events.append("stop"))
    setup.initialize(root, None, None)
    assert events == [("orchard", None), ("tart", "/opt/tart"), "stop"]
    assert setup.load_state(root)["given"] == {"orchard": False, "tart": True}
    assert not (root / "worker-bin/softnet").is_symlink()
    (tmp_path / "softnet").touch()
    setup.initialize(root, None, None)
    assert (root / "worker-bin/softnet").readlink() == tmp_path / "softnet"


def test_a_failed_download_leaves_the_earlier_services_running(tmp_path, monkeypatch):
    root = _earlier_setup(tmp_path, monkeypatch, {}, {})

    def binary(root, name, explicit):
        raise setup.SetupError("orchard 0.58.0 download checksum mismatch")

    stopped = []
    monkeypatch.setattr(setup, "binary", binary)
    monkeypatch.setattr(setup, "stop_other_orchard", lambda *args: stopped.append(True))
    with pytest.raises(setup.SetupError, match="checksum"):
        setup.initialize(root, None, None)
    assert stopped == []


@pytest.mark.parametrize("vms", [[], [{"name": "kept"}]])
def test_services_another_orchard_started_are_stopped_unless_they_hold_vms(
    tmp_path, monkeypatch, vms
):
    processes = {
        "controller": {"pid": 1},
        "worker": {"pid": 2, "orchard": "0.55.0"},
        "other": {"pid": 3},
    }
    state = {"processes": processes}
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    monkeypatch.setattr(setup, "api", lambda root, state, resource: vms)
    stopped = []
    monkeypatch.setattr(setup, "stop_process", lambda record: stopped.append(record["pid"]))
    if vms:
        with pytest.raises(setup.SetupError, match="holds VMs"):
            setup.stop_other_orchard(tmp_path, state)
        assert stopped == []
    else:
        setup.stop_other_orchard(tmp_path, state)
        assert stopped == [2, 1]
    # A worker whose controller is gone may still run VMs nobody can list.
    stopped.clear()
    monkeypatch.setattr(setup, "process_matches", lambda record: record["pid"] != 1)
    with pytest.raises(setup.SetupError, match="without its controller"):
        setup.stop_other_orchard(tmp_path, state)
    assert stopped == []
    monkeypatch.setattr(setup, "process_matches", lambda record: True)
    for record in processes.values():
        record["orchard"] = setup.RELEASES["orchard"][0]
    stopped.clear()
    setup.stop_other_orchard(tmp_path, state)
    assert stopped == []


def test_a_started_service_records_the_orchard_it_runs(tmp_path, monkeypatch):
    real = subprocess.Popen
    monkeypatch.setattr(setup.subprocess, "Popen", lambda argv, **kwargs: real(["true"]))
    monkeypatch.setattr(setup, "process_info", lambda pid: ("", ""))
    state = {"owner": setup.OWNER, "processes": {}}
    setup.start_process(tmp_path, state, "worker", ["worker"], {})
    assert state["processes"]["worker"]["orchard"] == setup.RELEASES["orchard"][0]


def softnet_release(monkeypatch, tmp_path, program: bytes = b"softnet program"):
    """A Softnet release whose archive holds ``program``, pinned by this script."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in (("LICENSE", b"license"), ("softnet", program)):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o755
            bundle.addfile(member, io.BytesIO(data))
    archive = buffer.getvalue()
    monkeypatch.setattr(
        setup,
        "SOFTNET_RELEASE",
        (
            "0.25.0",
            "https://example.invalid/softnet.tar.gz",
            hashlib.sha256(archive).hexdigest(),
            hashlib.sha256(program).hexdigest(),
        ),
    )
    monkeypatch.setattr(setup, "download", lambda url, path: path.write_bytes(archive))
    monkeypatch.setattr(setup, "SOFTNET", tmp_path / "libexec/softnet")
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "macos_major", lambda: 26)
    installed = []

    def sudo(argv, check):
        assert argv[0] == "/usr/bin/sudo"
        if argv[-2:-1] != ["-d"]:
            installed.append(Path(argv[-2]).read_bytes())
        installed.append(tuple(argv[1:]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup.subprocess, "run", sudo)
    return installed


def test_softnet_is_installed_root_owned_and_set_uid(tmp_path, monkeypatch):
    monkeypatch.undo()
    installed = softnet_release(monkeypatch, tmp_path)
    setup.install_softnet()
    owned = ("/usr/bin/install", "-o", "root", "-g", "wheel", "-m")
    assert installed[0] == (*owned, "0755", "-d", str(setup.SOFTNET.parent))
    assert installed[1] == b"softnet program"
    assert installed[2][:-2] == (*owned, "4755") and installed[2][-1] == str(setup.SOFTNET)


@pytest.mark.parametrize(("broken", "message"), [("archive", "download"), ("program", "program")])
def test_softnet_that_is_not_the_pinned_release_is_not_installed(
    tmp_path, monkeypatch, broken, message
):
    monkeypatch.undo()
    installed = softnet_release(monkeypatch, tmp_path)
    version, url, archive, program = setup.SOFTNET_RELEASE
    pins = (
        (version, url, "0" * 64, program)
        if broken == "archive"
        else (version, url, archive, "0" * 64)
    )
    monkeypatch.setattr(setup, "SOFTNET_RELEASE", pins)
    with pytest.raises(setup.SetupError, match=f"{message} checksum"):
        setup.install_softnet()
    assert installed == []


def test_softnet_in_place_or_before_macos_26_is_not_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.undo()
    installed = softnet_release(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "softnet_installed", lambda: True)
    setup.install_softnet()
    monkeypatch.setattr(setup, "softnet_installed", lambda: False)
    monkeypatch.setattr(setup, "macos_major", lambda: 15)
    setup.install_softnet()
    assert installed == []
    assert "macOS 26" in capsys.readouterr().out


def test_softnet_is_in_place_only_as_the_pinned_root_owned_set_uid_program(tmp_path, monkeypatch):
    softnet_release(monkeypatch, tmp_path)
    program = setup.SOFTNET
    assert not setup.softnet_installed()
    program.parent.mkdir()
    program.write_bytes(b"softnet program")
    program.chmod(0o4755)
    assert not setup.softnet_installed()  # this account's

    class RootOwned(type(program)):
        def stat(self, **kwargs):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | stat.S_ISUID | 0o755)

    monkeypatch.setattr(setup, "SOFTNET", RootOwned(program))
    assert setup.softnet_installed()
    program.write_bytes(b"another softnet")
    assert not setup.softnet_installed()
