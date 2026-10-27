#!/usr/bin/env python3
"""Prepare a private local Orchard controller and Tart worker for ALE.

Run with uv; this script uses only Python's standard library. It never changes
Homebrew, launchd, system trust, desktop permissions, or the shared Tart home.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OWNER = "ale-local-orchard-v1"
RELEASES = {
    "orchard": (
        "0.55.0",
        "https://github.com/openai/orchard/releases/download/0.55.0/orchard-darwin-arm64",
        "35a6ca9f1770a6da9ccc508cee46df21f962d4c5fb437161e4fe8daa34bb14fe",
    ),
    "tart": (
        "2.41.1",
        "https://github.com/cirruslabs/tart/releases/download/2.41.1/tart.tar.gz",
        "e680a734febccfce7d4e910c1f7f13fc3b00f19a181c945e2d746de3214c32c2",
    ),
}


class SetupError(Exception):
    """An actionable bootstrap failure with no credential-bearing details."""


def run(argv: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(
            f"Could not run {Path(argv[0]).name}; check installation and permissions"
        ) from exc
    if result.returncode:
        raise SetupError(
            f"{Path(argv[0]).name} failed (exit {result.returncode}). "
            "Check software compatibility and macOS security diagnostics; "
            "this script does not bypass signature or permission checks."
        )
    return result.stdout.strip()


def private_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise SetupError(f"Refusing symlink: {path.name}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def save_state(root: Path, state: dict) -> None:
    private_write(root / "state.new", json.dumps(state, indent=2) + "\n")
    (root / "state.new").replace(root / "state.json")


def load_state(root: Path) -> dict:
    if root.is_symlink() or (root / "state.json").is_symlink():
        raise SetupError("Refusing symlinked state directory")
    try:
        state = json.loads((root / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(
            "No owned ALE setup state here; initialize an empty directory first"
        ) from exc
    if state.get("owner") != OWNER:
        raise SetupError("Directory is not owned by this setup script")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise SetupError("Setup state must be owned by this user with permissions 0700")
    return state


def download(url: str, path: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as target:
        shutil.copyfileobj(response, target)


def install_binary(root: Path, name: str) -> Path:
    version, url, expected = RELEASES[name]
    directory = root / "bin"
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=root) as temporary:
        archive = Path(temporary) / "release"
        download(url, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise SetupError(f"{name} {version} download checksum mismatch")
        if name == "orchard":
            target = directory / "orchard"
            shutil.copyfile(archive, target)
            target.chmod(0o755)
        else:
            unpacked = Path(temporary) / "unpacked"
            unpacked.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(unpacked, filter="data")
            apps = list(unpacked.rglob("tart.app"))
            if len(apps) != 1:
                raise SetupError("Tart release does not contain exactly one application")
            destination = directory / "tart.app"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(apps[0], destination, symlinks=True)
            target = destination / "Contents/MacOS/tart"
    return target


def binary(root: Path, name: str, explicit: str | None) -> Path:
    target = root / "bin" / ("orchard" if name == "orchard" else "tart.app/Contents/MacOS/tart")
    candidate = Path(explicit).expanduser().absolute() if explicit else target
    if not candidate.exists() and not explicit:
        existing = shutil.which(name)
        if existing:
            candidate = Path(existing).resolve()
    if candidate.exists():
        version = run([str(candidate), "--version"])
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        required = tuple(map(int, RELEASES[name][0].split(".")))
        if match and tuple(map(int, match.groups())) == required:
            return candidate
        if explicit:
            raise SetupError(f"Explicit {name} must be version {RELEASES[name][0]}")
    if explicit:
        raise SetupError(f"Explicit {name} executable does not exist")
    candidate = install_binary(root, name)
    version = run([str(candidate), "--version"])
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match or match.group(0) != RELEASES[name][0]:
        raise SetupError(f"Installed {name} does not report pinned version {RELEASES[name][0]}")
    return candidate


def process_info(pid: int) -> tuple[str, str]:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        text=True,
        capture_output=True,
        timeout=5,
    )
    line = result.stdout.strip()
    return (line[:24], line[25:]) if line else ("", "")


def process_matches(record: dict) -> bool:
    started, command = process_info(record["pid"])
    return bool(started and started == record["started"] and command == record["command"])


def stop_process(record: dict) -> None:
    if not process_matches(record):
        return
    os.kill(record["pid"], signal.SIGTERM)
    for _ in range(100):
        if not process_matches(record):
            return
        time.sleep(0.1)
    raise SetupError("Owned service did not stop; state preserved for inspection")


def environment(root: Path, tart: str) -> dict[str, str]:
    # Do not inherit user Orchard contexts, auth overrides, or shared Tart caches.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("ORCHARD_", "TART_"))
    }
    env.update(ORCHARD_HOME=str(root / "orchard-home"), TART_HOME=str(root / "tart-home"))
    env["PATH"] = str(root / "worker-bin") + os.pathsep + env.get("PATH", "")
    return env


def start_process(
    root: Path, state: dict, role: str, argv: list[str], env: dict, token: str | None = None
) -> None:
    old = state["processes"].get(role)
    if old and process_matches(old):
        return
    log = root / f"{role}.log"
    private_write(log, "")
    with log.open("a") as stream:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.PIPE if token else subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    if token:
        try:
            process.stdin.write((token + "\n").encode())
            process.stdin.close()
        except BrokenPipeError:
            pass
    started, command = process_info(process.pid)
    state["processes"][role] = {"pid": process.pid, "started": started, "command": command}
    save_state(root, state)
    if process.poll() is not None:
        raise SetupError(f"{role} exited; inspect its private log")


def api(root: Path, state: dict, resource: str) -> list:
    password = (root / "password").read_text().strip()
    authorization = base64.b64encode(f"bootstrap-admin:{password}".encode()).decode()
    request = urllib.request.Request(
        f"https://localhost:{state['port']}/v1/{resource}",
        headers={"Authorization": "Basic " + authorization},
    )
    context = ssl.create_default_context(cafile=str(root / "controller.crt"))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=3) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SetupError(
            "Local Orchard HTTPS/authentication unavailable; inspect private logs"
        ) from exc
    if not isinstance(data, list):
        raise SetupError("Unexpected Orchard API response")
    return data


def ready(root: Path, state: dict) -> bool:
    processes = state.get("processes", {})
    if any(
        role not in processes or not process_matches(processes[role])
        for role in ("controller", "worker")
    ):
        return False
    for worker in api(root, state, "workers"):
        if not isinstance(worker, dict) or not (
            worker.get("name") == state["worker"]
            and worker.get("runtime") == "tart"
            and worker.get("arch") == "arm64"
            and worker.get("scheduling_paused", False) is False
        ):
            continue
        stamp = worker.get("last_seen")
        if not isinstance(stamp, str):
            continue
        try:
            age = (
                datetime.now(UTC) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            continue
        if -30 <= age <= 180:
            return True
    return False


def wait_for(predicate, description: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except SetupError:
            pass
        time.sleep(0.25)
    raise SetupError(f"Timed out waiting for {description}; inspect the private service logs")


def write_config(root: Path, state: dict, password: str) -> None:
    private_write(
        root / "run.toml",
        "\n".join(
            [
                "[vm]",
                'provider = "orchard"',
                "",
                "[vm.orchard]",
                f'endpoint = "https://localhost:{state["port"]}"',
                'username_env = "ALE_ORCHARD_USERNAME"',
                'password_env = "ALE_ORCHARD_PASSWORD"',
                f"ca_file = {json.dumps(str(root / 'controller.crt'))}",
                'python_path = "/opt/homebrew/bin/python3"',
                "",
                "[artifacts]",
                'collect = "host"',
                "",
            ]
        ),
    )
    private_write(
        root / "credentials.env",
        "\n".join(
            [
                "export ALE_ORCHARD_USERNAME=bootstrap-admin",
                f"export ALE_ORCHARD_PASSWORD={shlex.quote(password)}",
                f"export ALE_RUN_CONFIG={shlex.quote(str(root / 'run.toml'))}",
                "",
            ]
        ),
    )


def initialize(root: Path, orchard_path: str | None, tart_path: str | None) -> dict:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupError("Local Tart setup requires an Apple Silicon Mac")
    if root.exists():
        state = load_state(root)
    else:
        root.mkdir(parents=True, mode=0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        state = {
            "owner": OWNER,
            "port": port,
            "worker": "ale-local-" + secrets.token_hex(4),
            "processes": {},
        }
        save_state(root, state)
        private_write(root / "password", secrets.token_urlsafe(32))
    selected = state.get("binaries", {})
    for name, explicit in (("orchard", orchard_path), ("tart", tart_path)):
        if (
            explicit
            and name in selected
            and Path(explicit).expanduser().resolve() != Path(selected[name]).resolve()
        ):
            raise SetupError(
                "Existing setup uses different executables; clean up before changing tools"
            )
    orchard = binary(root, "orchard", orchard_path or selected.get("orchard"))
    tart = binary(root, "tart", tart_path or selected.get("tart"))
    state["binaries"] = {"orchard": str(orchard), "tart": str(tart)}
    save_state(root, state)
    for name in ("orchard-home", "tart-home", "worker-bin"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    link = root / "worker-bin/tart"
    if link.is_symlink():
        if link.resolve() != tart.resolve():
            raise SetupError(
                "Existing worker Tart executable differs; stop setup before changing tools"
            )
    elif link.exists():
        raise SetupError("Unexpected worker executable")
    else:
        link.symlink_to(tart)
    if not (root / "controller.crt").exists():
        private_write(
            root / "openssl.cnf",
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\n"
            "prompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
            "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,digitalSignature\n",
        )
        run(
            [
                "/usr/bin/openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "365",
                "-config",
                str(root / "openssl.cnf"),
                "-keyout",
                str(root / "controller.key"),
                "-out",
                str(root / "controller.crt"),
            ]
        )
        (root / "controller.key").chmod(0o600)
    env = environment(root, str(tart))
    password = (root / "password").read_text().strip()
    start_process(
        root,
        state,
        "controller",
        [
            str(orchard),
            "controller",
            "run",
            "--listen",
            f"127.0.0.1:{state['port']}",
            "--data-dir",
            str(root / "controller"),
            "--controller-cert",
            str(root / "controller.crt"),
            "--controller-key",
            str(root / "controller.key"),
        ],
        env | {"ORCHARD_BOOTSTRAP_ADMIN_TOKEN": password},
    )
    wait_for(lambda: api(root, state, "workers") is not None, "controller")
    token = run([str(orchard), "get", "bootstrap-token", "bootstrap-admin"], env=env)
    start_process(
        root,
        state,
        "worker",
        [
            str(orchard),
            "worker",
            "run",
            f"https://localhost:{state['port']}",
            "--name",
            state["worker"],
            "--bootstrap-token-stdin",
        ],
        env,
        token,
    )
    wait_for(lambda: ready(root, state), "registered Tart worker")
    write_config(root, state, password)
    return state


def cleanup(root: Path, state: dict) -> None:
    # A stopped controller does not prove that its Tart VM disks were removed.
    local_vms = root / "tart-home" / "vms"
    if local_vms.is_symlink() or (local_vms.exists() and any(local_vms.iterdir())):
        raise SetupError(
            "Private Tart home still contains VMs; collect and delete them before cleanup"
        )
    controller = state["processes"].get("controller")
    worker = state["processes"].get("worker")
    if controller and process_matches(controller):
        if api(root, state, "vms"):
            raise SetupError(
                "Controller still contains VMs; collect and delete them before cleanup"
            )
    elif worker and process_matches(worker):
        raise SetupError("Worker is running without controller; restart setup before safe cleanup")
    for role in ("worker", "controller"):
        if record := state["processes"].get(role):
            stop_process(record)
    shutil.rmtree(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/ale/macos")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--cleanup", action="store_true")
    parser.add_argument("--orchard-path", help="Use an explicitly installed compatible Orchard")
    parser.add_argument("--tart-path", help="Use an explicitly installed compatible Tart")
    args = parser.parse_args(argv)
    root = args.state_dir.expanduser().absolute()
    try:
        if args.cleanup and not root.exists():
            print("No local setup to clean up.")
            return 0
        if args.check:
            state = load_state(root)
            if not ready(root, state):
                raise SetupError("Local services are not ready; run setup again to start them")
            print(
                "Local Orchard controller and Tart worker are ready; guest capabilities untested."
            )
            return 0
        # A sibling lock does not disappear while cleanup removes the state directory.
        root.parent.mkdir(parents=True, exist_ok=True)
        lock_path = root.parent / (root.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.cleanup:
                cleanup(root, load_state(root))
                print("Removed owned local services and setup data.")
            else:
                initialize(root, args.orchard_path, args.tart_path)
                print(f"Ready. Run: source {shlex.quote(str(root / 'credentials.env'))}")
                print(f"Then: uv run ale run TASK --config {shlex.quote(str(root / 'run.toml'))}")
                print("Provide a prepared OCI image with the required guest apps and permissions.")
        return 0
    except (SetupError, OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
