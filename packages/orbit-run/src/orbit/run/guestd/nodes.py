"""ssh between the nodes of a Sandbox that spans several machines.

Such a Sandbox runs one container on each of its machines, named ``node0`` to ``nodeN-1``
and joined by a network that reaches only one another. The agent works on node 0 and starts
processes on the others as people do on a cluster: ``ssh node1 COMMAND``, which ``torchrun``
on each node, ``mpirun``, ``pdsh``, DeepSpeed, ``scp`` and ``rsync`` build on. Images run
no sshd, so the framework layer of a Task one of whose Sandboxes spans several nodes adds
Orbit's own OpenSSH at ``/opt/orbit-ssh``, static so that it runs on any image, and every
node runs it. The engine runs this file as root on every node, through guestd, with the
framework's Python:

- ``keys`` prints the Sandbox's keys, new for each Sandbox: the nodes' host key, root's and
  the agent account's, as JSON.
- ``join`` reads them from ``ORBIT_NODES_JOIN``, with the node's number, the nodes' names
  and addresses and the variables the node's sessions get, and, again each time it is run:
  writes sshd's configuration and keys below ``/etc/ssh/orbit``, root's and the account's
  key at ``~/.ssh/id_ed25519``, the nodes' host key to ``/etc/ssh/ssh_known_hosts``, where
  any OpenSSH client looks, links ``ssh``, ``scp`` and ``sftp`` into ``/usr/local/bin`` where
  the image has none, and starts sshd, or has it reread its configuration.

A session gets from sshd the account's ``HOME``, ``USER``, ``LOGNAME``, ``SHELL`` and
directory; then the container's own variables, the image's and the Task's as its first
process got them, as a command through guestd does; then the node's variables from the
engine: the nodes' and, while the Sandbox is sealed, its egress proxy's. sshd takes them in
one ``SetEnv``, so ``join`` runs again as they change.

Standard library only, and no engine imports: this file is copied into guests.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SSH = Path("/opt/orbit-ssh")
ETC = Path("/etc/ssh/orbit")
KNOWN_HOSTS = Path("/etc/ssh/ssh_known_hosts")
#: Where sshd's privilege-separated part, which runs as nobody, is chrooted, and where the
#: clients are linked when the image has none.
EMPTY, BIN = Path("/var/empty"), Path("/usr/local/bin")
#: Where ``join`` takes its input from: a root process's environment, which no other
#: account reads, as the private keys must not be on a command line.
VARIABLE = "ORBIT_NODES_JOIN"
#: The comment that marks the nodes' line in the known hosts, which ``join`` replaces.
MARK = "orbit-sandbox-nodes"
#: Variables that describe a node, an account or a session, which sshd or the session sets.
_LOCAL = frozenset(
    {"HOME", "USER", "LOGNAME", "SHELL", "MAIL", "HOSTNAME", "PWD", "OLDPWD", "SHLVL", "_",
     "TERM"}
)  # fmt: skip


def keys() -> dict[str, list[str]]:
    """New ed25519 keys, private and public: the nodes' host key, root's and the account's."""
    found: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as directory:
        for name in ("host", "root", "account"):
            path = Path(directory) / name
            subprocess.run(
                [str(SSH / "bin" / "ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C",
                 f"orbit-{name}", "-f", str(path)],
                check=True,
            )  # fmt: skip
            found[name] = [path.read_text(), Path(f"{path}.pub").read_text().strip()]
    return found


def _write(path: Path, text: str, mode: int) -> None:
    """Replace ``path``, in a directory of root's, whole, created with ``mode``."""
    temporary = path.with_name(path.name + ".orbit")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)
        os.fchmod(file.fileno(), mode)
    os.replace(temporary, path)


def _directory(path: Path, mode: int) -> None:
    """A directory of root's."""
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    os.chmod(path, mode)


def _own_key(owner: pwd.struct_passwd, pair: list[str]) -> None:
    """Give ``owner`` its key at ``~/.ssh/id_ed25519``, through descriptors that follow no
    link: the account may have made its ``.ssh``, or a file in it, a link to a file of root's,
    which root's writes would otherwise change."""
    home = os.open(owner.pw_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            if not stat.S_ISDIR(os.stat(".ssh", dir_fd=home, follow_symlinks=False).st_mode):
                os.unlink(".ssh", dir_fd=home)
                os.mkdir(".ssh", 0o700, dir_fd=home)
        except FileNotFoundError:
            os.mkdir(".ssh", 0o700, dir_fd=home)
        own = os.open(".ssh", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
    finally:
        os.close(home)
    try:
        os.fchown(own, owner.pw_uid, owner.pw_gid)
        os.fchmod(own, 0o700)
        for name, text, mode in (
            ("id_ed25519", pair[0], 0o600),
            ("id_ed25519.pub", pair[1] + "\n", 0o644),
        ):
            temporary = name + ".orbit"
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=own)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(temporary, flags, mode, dir_fd=own), "w") as file:
                file.write(text)
                os.fchmod(file.fileno(), mode)
                os.fchown(file.fileno(), owner.pw_uid, owner.pw_gid)
            os.replace(temporary, name, src_dir_fd=own, dst_dir_fd=own)
    finally:
        os.close(own)


def container_variables() -> dict[str, str]:
    """The variables the container's first process started with: the image's and the
    Task's."""
    found = {}
    for entry in Path("/proc/1/environ").read_bytes().split(b"\0"):
        name, equals, value = entry.decode("utf-8", "replace").partition("=")
        if equals and name:
            found[name] = value
    return found


def session_variables(given: dict[str, str]) -> dict[str, str]:
    """What a session on this node gets above sshd's own: the container's variables, then
    ``given``, the node's; none that describes a node, an account or a session, and none a
    configuration line cannot hold."""
    merged = {**container_variables(), **given}
    return {
        name: value
        for name, value in merged.items()
        if name not in _LOCAL
        and not name.startswith("SSH_")
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        and "\n" not in value
        and "\r" not in value
    }


def set_env(variables: dict[str, str]) -> str:
    """sshd's ``SetEnv`` line, which takes every variable: sshd keeps only the first."""
    quoted = []
    for name, value in sorted(variables.items()):
        text = f"{name}={value}".replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{text}"')
    return "SetEnv " + " ".join(quoted) if quoted else ""


def sshd_config(address: str, variables: dict[str, str]) -> str:
    return (
        "\n".join(
            line
            for line in (
                f"ListenAddress {address}",
                f"HostKey {ETC}/host",
                f"PidFile {ETC}/sshd.pid",
                f"AuthorizedKeysFile {ETC}/authorized/%u",
                "PermitRootLogin prohibit-password",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PrintMotd no",
                # A launcher reaches every node at once, and each rank may again.
                "MaxStartups 1000:30:1000",
                f"Subsystem sftp {SSH}/libexec/sftp-server",
                set_env(variables),
            )
            if line
        )
        + "\n"
    )


def _running() -> int | None:
    """sshd's process, if it runs."""
    try:
        pid = int((ETC / "sshd.pid").read_text())
        if os.readlink(f"/proc/{pid}/exe") == str(SSH / "sbin" / "sshd"):
            return pid
    except (OSError, ValueError):
        pass
    return None


def join(config: dict[str, object]) -> None:
    rank = int(config["rank"])  # type: ignore[call-overload]
    names = [str(name) for name in config["names"]]  # type: ignore[attr-defined]
    addresses = [str(address) for address in config["addresses"]]  # type: ignore[attr-defined]
    pairs: dict[str, list[str]] = config["keys"]  # type: ignore[assignment]
    account = pwd.getpwnam(str(config["account"]))
    root = pwd.getpwuid(0)
    _directory(ETC, 0o755)
    _directory(ETC / "authorized", 0o755)
    _write(ETC / "host", pairs["host"][0], 0o600)
    for owner, pair in ((root, pairs["root"]), (account, pairs["account"])):
        _write(ETC / "authorized" / owner.pw_name, pair[1] + "\n", 0o644)
        _own_key(owner, pair)
        if account.pw_uid == 0:
            break
    host = pairs["host"][1].split()
    line = f"{','.join(names + addresses)} {host[0]} {host[1]} {MARK}"
    kept = []
    if KNOWN_HOSTS.exists():
        kept = [each for each in KNOWN_HOSTS.read_text().splitlines() if MARK not in each]
    _directory(KNOWN_HOSTS.parent, 0o755)
    _write(KNOWN_HOSTS, "\n".join([*kept, line]) + "\n", 0o644)
    _directory(EMPTY, 0o755)
    variables = session_variables(dict(config["variables"]))  # type: ignore[call-overload]
    path = variables.get("PATH") or os.defpath
    for tool in ("ssh", "scp", "sftp"):
        if shutil.which(tool, path=path) is None:
            _directory(BIN, 0o755)
            link = BIN / tool
            link.unlink(missing_ok=True)
            link.symlink_to(SSH / "bin" / tool)
    _write(ETC / "sshd_config", sshd_config(addresses[rank], variables), 0o600)
    sshd = [str(SSH / "sbin" / "sshd"), "-f", str(ETC / "sshd_config")]
    # sshd and every session it starts would inherit the keys.
    clean = {name: value for name, value in os.environ.items() if name != VARIABLE}
    checked = subprocess.run([*sshd, "-t"], capture_output=True, text=True, check=False, env=clean)
    if checked.returncode:
        raise SystemExit(f"sshd refuses its configuration: {checked.stderr.strip()}")
    if (pid := _running()) is not None:
        # It reexecutes itself with the new configuration; sessions keep theirs.
        os.kill(pid, signal.SIGHUP)
        return
    subprocess.run([*sshd, "-E", str(ETC / "log")], check=True, env=clean)


def main(argv: list[str] | None = None) -> None:
    command = (argv if argv is not None else sys.argv[1:])[0]
    if not (SSH / "sbin" / "sshd").is_file():
        raise SystemExit(
            f"the image has no OpenSSH at {SSH}: its framework layer was added for a Task none "
            "of whose Sandboxes spans several nodes"
        )
    if command == "keys":
        print(json.dumps(keys()))
    elif command == "join":
        join(json.loads(os.environ[VARIABLE]))
    else:
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    main()
