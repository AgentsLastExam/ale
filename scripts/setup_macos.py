#!/usr/bin/env python3
"""Prepare a private local Orchard controller and Tart worker for Orbit.

Run with uv; this script uses only Python's standard library. It never changes
Homebrew, launchd, system trust, desktop permissions, or the shared Tart home. With sudo,
which asks for this account's password, it installs Orbit's macOS framer: a root-owned copy of
``framework_macos.sh`` and a sudoers rule that lets this account run that program alone as
root, which framing an image offline needs. On macOS 26 or later it also installs Softnet
root-owned and set-user-ID as the worker's ``softnet``, which a VM whose network Orbit
restricts runs on.

It stays in the foreground as the worker's parent. macOS's Local Network privacy lets a
program started from a Terminal or SSH login reach the VMs only while it descends from
that login, so a worker left behind as an orphan fails with "no route to host".
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
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

OWNER = "orbit-local-orchard-v1"
#: The Orchard Provider reads the controller, its credentials and Tart from here.
STATE_DIR = Path.home() / ".local/share/orbit/macos"
#: Local Tart images Orbit keeps as a cache, which cleanup removes with the rest.
CACHED_IMAGES = ("orbit-framed-", "orbit-built-")
#: The framer the Orchard Provider runs through sudo, and the rule that allows only it.
FRAMER = Path("/usr/local/libexec/orbit/framework-macos")
FRAMER_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages/orbit-run/src/orbit/run/framework_macos.sh"
)
FRAMER_RULE = Path("/etc/sudoers.d/orbit-framework-macos")
#: Softnet, which Tart runs for a VM whose network Orbit restricts, and which needs root to set
#: the VM's network up; the Orchard Provider looks for it here.
SOFTNET = Path("/usr/local/libexec/orbit/softnet")
#: Its release archive and the SHA-256 of the archive and of the program in it.
SOFTNET_RELEASE = (
    "0.25.0",
    "https://github.com/openai/softnet/releases/download/0.25.0/softnet.tar.gz",
    "5a2e8ef779f0a2169b88d273ee806c0d64321ec7714c0dc99bf217523ae9b008",
    "5c838134bb91af720d3575a9918e0bebe48a40bf37deaf0a5ec2219ba94b8534",
)
RELEASES = {
    "orchard": (
        "0.58.0",
        "https://github.com/openai/orchard/releases/download/0.58.0/orchard-darwin-arm64",
        "8875ad3b34959ca1585860cbbdee447f069846d3aa05ec6d92038640a3b9800e",
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
            "No owned Orbit setup state here; initialize an empty directory first"
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
        # Staged, then renamed into place, so a service still running the old copy keeps it.
        if name == "orchard":
            target = directory / "orchard"
            staged = Path(temporary) / "orchard"
            shutil.copyfile(archive, staged)
            staged.chmod(0o755)
            os.replace(staged, target)
        else:
            unpacked = Path(temporary) / "unpacked"
            unpacked.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(unpacked, filter="data")
            apps = list(unpacked.rglob("tart.app"))
            if len(apps) != 1:
                raise SetupError("Tart release does not contain exactly one application")
            destination = directory / "tart.app"
            staged = directory / f"tart.app.{secrets.token_hex(4)}"
            shutil.copytree(apps[0], staged, symlinks=True)
            if destination.exists():
                destination.rename(Path(temporary) / "replaced.app")
            staged.rename(destination)
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
) -> subprocess.Popen | None:
    """Start ``role`` unless it runs already; the new process, or ``None`` if it did."""
    old = state["processes"].get(role)
    if old and process_matches(old):
        return None
    log = root / f"{role}.log"
    private_write(log, "")
    with log.open("a") as stream:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.PIPE if token else subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=role == "controller",
        )
    if token:
        try:
            process.stdin.write((token + "\n").encode())
            process.stdin.close()
        except BrokenPipeError:
            pass
    started, command = process_info(process.pid)
    state["processes"][role] = {
        "pid": process.pid,
        "started": started,
        "command": command,
        "orchard": RELEASES["orchard"][0],
    }
    save_state(root, state)
    if process.poll() is not None:
        raise SetupError(f"{role} exited; inspect its private log")
    return process


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


def stop_other_orchard(root: Path, state: dict) -> None:
    """Stop the services a run of another Orchard pin started, so this run starts its own; not
    while the controller holds VMs, which the worker would stop."""
    pin = RELEASES["orchard"][0]
    processes = state["processes"]
    outdated = [
        role
        for role in ("worker", "controller")
        if role in processes
        and processes[role].get("orchard") != pin
        and process_matches(processes[role])
    ]
    if not outdated:
        return
    controller = processes.get("controller")
    if not (controller and process_matches(controller)):
        # Without its controller, nothing says which VMs the worker still runs.
        raise SetupError(
            f"Setup now runs Orchard {pin}, but an earlier worker runs without its "
            "controller; stop it once its VMs are collected, then run setup again"
        )
    if api(root, state, "vms"):
        raise SetupError(
            f"Setup now runs Orchard {pin}, but the running controller still holds VMs; "
            "collect and delete them, then run setup again"
        )
    for role in outdated:
        stop_process(processes[role])


def initialize(
    root: Path, orchard_path: str | None, tart_path: str | None
) -> subprocess.Popen | None:
    """Start what is missing; the worker this run started, which it must stay the parent of."""
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
            "worker": "orbit-local-" + secrets.token_hex(4),
            "processes": {},
        }
        save_state(root, state)
        private_write(root / "password", secrets.token_urlsafe(32))
    selected = state.get("binaries", {})
    given = state.get("given", {})
    for name, explicit in (("orchard", orchard_path), ("tart", tart_path)):
        if (
            explicit
            and name in selected
            and Path(explicit).expanduser().resolve() != Path(selected[name]).resolve()
        ):
            raise SetupError(
                "Existing setup uses different executables; clean up before changing tools"
            )

    def chosen(name: str) -> str | None:
        """The executable an earlier run was given. One it found on PATH or installed is
        found again, and replaced by the pinned release when it is another version."""
        return selected.get(name) if given.get(name) else None

    explicit = {"orchard": orchard_path or chosen("orchard"), "tart": tart_path or chosen("tart")}
    orchard = binary(root, "orchard", explicit["orchard"])
    tart = binary(root, "tart", explicit["tart"])
    # Only once both are in hand: a failed download leaves the earlier services running.
    stop_other_orchard(root, state)
    state["binaries"] = {"orchard": str(orchard), "tart": str(tart)}
    state["given"] = {name: bool(path) for name, path in explicit.items()}
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
    # Tart finds Softnet on the worker's PATH when it starts a VM that has it.
    softnet = root / "worker-bin/softnet"
    if SOFTNET.exists() and not softnet.is_symlink():
        softnet.symlink_to(SOFTNET)
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
    worker = start_process(
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
    return worker


def install_framer() -> None:
    """Install this checkout's framer root-owned, and the rule to run it, unless both are in place.

    The rule file is readable by root alone, so only its presence is checked.
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupError("Local Tart setup requires an Apple Silicon Mac")
    account = run(["/usr/bin/id", "-un"]).strip()
    rule = f"{account} ALL=(root) NOPASSWD: {FRAMER}\n"
    source = FRAMER_SOURCE.read_bytes()
    try:
        current = FRAMER.read_bytes() == source and FRAMER_RULE.is_file()
    except OSError:
        current = False
    if current:
        return
    print("Installing the macOS framer as root; sudo may ask for this account's password.")
    with tempfile.TemporaryDirectory() as staging:
        rule_file = Path(staging) / "rule"
        rule_file.write_text(rule)
        for argv in (
            ["/usr/sbin/visudo", "-cqf", str(rule_file)],
            [
                "/usr/bin/install",
                "-d",
                "-o",
                "root",
                "-g",
                "wheel",
                "-m",
                "0755",
                str(FRAMER.parent),
            ],
            [
                "/usr/bin/install",
                "-o",
                "root",
                "-g",
                "wheel",
                "-m",
                "0755",
                str(FRAMER_SOURCE),
                str(FRAMER),
            ],
            [
                "/usr/bin/install",
                "-o",
                "root",
                "-g",
                "wheel",
                "-m",
                "0440",
                str(rule_file),
                str(FRAMER_RULE),
            ],
        ):
            if subprocess.run(["/usr/bin/sudo", *argv], check=False).returncode:
                raise SetupError(f"could not install the macOS framer ({Path(argv[0]).name})")


def softnet_installed() -> bool:
    """Whether Softnet is in place: the pinned program, root-owned and set-user-ID."""
    try:
        info = SOFTNET.stat()
        current = hashlib.sha256(SOFTNET.read_bytes()).hexdigest() == SOFTNET_RELEASE[3]
    except OSError:
        return False
    return current and info.st_uid == 0 and bool(info.st_mode & stat.S_ISUID)


def macos_major() -> int:
    """This Mac's macOS major version. ``sw_vers`` reports it as it is, where Python built
    for an older SDK may be shown the compatibility version, 16 for macOS 26."""
    result = subprocess.run(
        ["/usr/bin/sw_vers", "-productVersion"], capture_output=True, text=True, check=False
    )
    return int((result.stdout.strip() or "0").split(".")[0])


def install_softnet() -> None:
    """Install the pinned Softnet root-owned and set-user-ID, unless it is in place.

    It needs macOS 26 or later; elsewhere it is left out, and only open macOS Tasks run.
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SetupError("Local Tart setup requires an Apple Silicon Mac")
    if macos_major() < 26:
        print("Softnet needs macOS 26 or later; only open macOS Tasks run on this Mac.")
        return
    if softnet_installed():
        return
    version, url, archive_digest, digest = SOFTNET_RELEASE
    with tempfile.TemporaryDirectory() as staging:
        archive = Path(staging) / "softnet.tar.gz"
        download(url, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_digest:
            raise SetupError(f"softnet {version} download checksum mismatch")
        unpacked = Path(staging) / "unpacked"
        with tarfile.open(archive) as bundle:
            bundle.extract("softnet", unpacked, filter="data")
        program = unpacked / "softnet"
        if hashlib.sha256(program.read_bytes()).hexdigest() != digest:
            raise SetupError(f"softnet {version} program checksum mismatch")
        print("Installing Softnet as root; sudo may ask for this account's password.")
        owned = ["/usr/bin/install", "-o", "root", "-g", "wheel", "-m"]
        for argv in (
            [*owned, "0755", "-d", str(SOFTNET.parent)],
            [*owned, "4755", str(program), str(SOFTNET)],
        ):
            if subprocess.run(["/usr/bin/sudo", *argv], check=False).returncode:
                raise SetupError("could not install Softnet")


def remove_installed() -> None:
    """Remove the framer, its sudoers rule and Softnet."""
    if FRAMER.exists() or FRAMER_RULE.exists() or SOFTNET.exists():
        argv = ["/usr/bin/sudo", "/bin/rm", "-f", str(FRAMER_RULE), str(FRAMER), str(SOFTNET)]
        if subprocess.run(argv, check=False).returncode:
            raise SetupError("could not remove the macOS framer and Softnet")


def cleanup(root: Path, state: dict) -> None:
    # A stopped controller does not prove that its Tart VM disks were removed.
    local_vms = root / "tart-home" / "vms"
    if local_vms.is_symlink() or (
        local_vms.exists()
        and any(not vm.name.startswith(CACHED_IMAGES) for vm in local_vms.iterdir())
    ):
        raise SetupError(
            "Private Tart home still contains VMs or snapshot images; collect and delete them "
            "before cleanup"
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
    remove_installed()


def hold(worker: subprocess.Popen) -> None:
    """Stay the worker's parent until it exits; Ctrl-C reaches it as part of this group."""
    print("Keep this command running while Tasks run. Ctrl-C stops the worker.", flush=True)
    try:
        worker.wait()
    except KeyboardInterrupt:
        worker.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--cleanup", action="store_true")
    parser.add_argument("--orchard-path", help="Use an explicitly installed compatible Orchard")
    parser.add_argument("--tart-path", help="Use an explicitly installed compatible Tart")
    args = parser.parse_args(argv)
    root = STATE_DIR
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
            linked = (root / "worker-bin/softnet").is_symlink()
            if softnet_installed() and linked:
                print(f"Softnet {SOFTNET_RELEASE[0]} is the worker's: restricted networking runs.")
            else:
                print("Softnet is not the worker's: only open macOS Tasks run.")
            return 0
        # A sibling lock does not disappear while cleanup removes the state directory.
        root.parent.mkdir(parents=True, exist_ok=True)
        lock_path = root.parent / (root.name + ".lock")
        worker = None
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.cleanup:
                cleanup(root, load_state(root))
                print("Removed owned local services and setup data.")
            else:
                install_framer()
                install_softnet()
                worker = initialize(root, args.orchard_path, args.tart_path)
                print("Ready. In another shell, run macOS Tasks with: uv run orbit run TASK")
        if worker is not None:
            hold(worker)
        return 0
    except (SetupError, OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
