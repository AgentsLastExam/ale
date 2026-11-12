"""PID 1 of an ALE Firecracker microVM.

The Provider writes this file into every microVM disk it converts, and the kernel starts
it on the framework's Python. It is shipped as source and runs in the guest, so like
guestd it is standard-library only and imports nothing from the engine.

It does for the image what Docker does for a container:

* lays the sandbox's writable disk over the read-only root with overlayfs, which is how
  one converted disk serves every sandbox of an image, as one image's layers serve every
  container;
* mounts ``/proc``, ``/sys``, ``/dev`` and the rest a process expects, and writes
  ``/etc/hostname``, ``/etc/hosts`` and ``/etc/resolv.conf``;
* starts guestd as root and the image's own command (``ENTRYPOINT`` and ``CMD``) as its
  ``USER``, in its ``WORKDIR``, with its ``ENV``;
* stops the machine when that command exits, since a container lives exactly as long as
  its command, and on SIGINT, which is how the Provider asks for a clean shutdown.
"""

from __future__ import annotations

import ctypes
import fcntl
import grp
import json
import os
import pwd
import shutil
import signal
import socket
import struct
import sys
import time

HERE = "/opt/ale/firecracker"
#: The sandbox's writable disk, then the overlay's pieces on it.
RW = f"{HERE}/rw"
UPPER = f"{RW}/upper"
WORK = f"{RW}/work"
MERGED = f"{RW}/merged"
#: Where the boot root goes while the overlay becomes ``/``.
OLD = f"{HERE}/old"
#: A private tmpfs for guestd's claimed token, which must not outlive a boot.
RUN = f"{HERE}/run"
COMMAND_LOG = f"{RW}/ale/command.log"
GUESTD = ("/opt/ale/python/bin/python3", "/opt/ale/guestd/main.py")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_MOVE = 8192
MS_REC = 16384
MS_PRIVATE = 1 << 18
MNT_DETACH = 2
SYS_PIVOT_ROOT = 155  # x86-64
RB_AUTOBOOT = 0x01234567
RB_DISABLE_CAD = 0

_libc = ctypes.CDLL(None, use_errno=True)


class Shutdown(Exception):
    """The machine should stop: its command exited, or the Host asked."""


def _check(result: int, what: str) -> None:
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"{what}: {os.strerror(code)}")


def mount(source: str, target: str, fstype: str | None, flags: int = 0, data: str = "") -> None:
    _check(
        _libc.mount(
            source.encode(),
            target.encode(),
            fstype.encode() if fstype else None,
            ctypes.c_ulong(flags),
            data.encode() if data else None,
        ),
        f"mount {target}",
    )


def _log(message: str) -> None:
    with open("/dev/kmsg", "w") as kmsg:
        kmsg.write(f"ale-init: {message}\n")


def assemble_root() -> dict:
    """Mount the overlay of the writable disk on the read-only layers and make it ``/``."""
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("none", "/", None, MS_REC | MS_PRIVATE)
    mount("/dev/vdb", RW, "ext4", MS_NOSUID | MS_NODEV, "noinit_itable")
    with open(f"{RW}/ale/sandbox.json") as handle:
        config = json.load(handle)
    lowers = []
    for index, device in enumerate(config.get("layers", ())):
        target = f"{RW}/lower/{index}"
        os.makedirs(target, exist_ok=True)
        mount(device, target, "ext4", MS_RDONLY | MS_NOSUID | MS_NODEV, "noload")
        lowers.append(f"{target}/upper")
    for path in (UPPER, WORK, MERGED):
        os.makedirs(path, exist_ok=True)
    options = f"lowerdir={':'.join([*lowers, '/'])},upperdir={UPPER},workdir={WORK}"
    mount("overlay", MERGED, "overlay", 0, options)
    os.chdir(MERGED)
    _check(_libc.syscall(SYS_PIVOT_ROOT, b".", OLD.lstrip("/").encode()), "pivot_root")
    os.chroot(".")
    os.chdir("/")
    # The writable disk stays reachable to root, for the command log and a clean shutdown;
    # the boot root and its mounts go.
    mount(f"{OLD}{RW}", RW, None, MS_MOVE)
    _check(_libc.umount2(OLD.encode(), MNT_DETACH), "umount boot root")
    return config


def mount_basics() -> None:
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755")
    mount("tmpfs", RUN, "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "mode=0700,size=1m")
    for name, target in (
        ("fd", "/proc/self/fd"),
        ("stdin", "/proc/self/fd/0"),
        ("stdout", "/proc/self/fd/1"),
        ("stderr", "/proc/self/fd/2"),
    ):
        if not os.path.lexists(f"/dev/{name}"):
            os.symlink(target, f"/dev/{name}")
    for path in ("/dev/pts", "/dev/shm", "/dev/mqueue"):
        os.makedirs(path, exist_ok=True)
    mount("devpts", "/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC, "gid=5,mode=620,ptmxmode=666")
    # A VM's default size, half the guest's memory, where Docker gives 64 MB.
    mount("shm", "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")
    mount("mqueue", "/dev/mqueue", "mqueue", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount("cgroup2", "/sys/fs/cgroup", "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC)


def _replace(path: str, text: str) -> None:
    """Write a file the way Docker's bind mount would shadow it: never through a link."""
    if os.path.islink(path):
        os.unlink(path)
    with open(path, "w") as handle:
        handle.write(text)


def configure_network(config: dict) -> None:
    """The kernel configured eth0 from its command line; name the machine and its resolvers.

    The resolvers are the runner's, which Docker gave it from the Host's.
    """
    hostname = config["hostname"]
    _check(_libc.sethostname(hostname.encode(), len(hostname)), "sethostname")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sh22x", b"lo", 0)
        flags = struct.unpack("16sh22x", fcntl.ioctl(sock, 0x8913, request))[1]
        fcntl.ioctl(sock, 0x8914, struct.pack("16sh22x", b"lo", flags | 1))
    _replace("/etc/hostname", f"{hostname}\n")
    _replace(
        "/etc/hosts",
        "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n"
        f"{config['address']}\t{hostname}\n",
    )
    with open(f"{RW}/ale/resolv.conf") as handle:
        _replace("/etc/resolv.conf", handle.read())


def _account(user: str) -> tuple[int, int, str, list[int]]:
    """uid, gid, home and supplementary groups for an OCI ``USER`` (``name``, ``uid``, ``x:y``)."""
    name, _, group = (user or "root").partition(":")
    try:
        entry = pwd.getpwnam(name) if not name.isdigit() else pwd.getpwuid(int(name))
        uid, gid, home, login = entry.pw_uid, entry.pw_gid, entry.pw_dir, entry.pw_name
    except KeyError:
        if not name.isdigit():
            raise
        uid, gid, home, login = int(name), int(name), "/", None
    if group:
        gid = int(group) if group.isdigit() else grp.getgrnam(group).gr_gid
    groups = os.getgrouplist(login, gid) if login else [gid]
    return uid, gid, home or "/", groups


def _environment(image_env: list[str], extra: dict[str, str], hostname: str, home: str) -> dict:
    env = dict(item.partition("=")[::2] for item in image_env)
    env.update(extra)
    env.setdefault("PATH", DEFAULT_PATH)
    env.setdefault("HOME", home)
    env["HOSTNAME"] = hostname
    return env


def _spawn(argv: list[str], env: dict, *, cwd: str, account=None, log: int | None = None) -> int:
    # Found while still root: once the child is the image's user, the framework's Python
    # can no longer read its own standard library, which a lookup on PATH would import.
    program = argv[0] if "/" in argv[0] else shutil.which(argv[0], path=env.get("PATH"))
    pid = os.fork()
    if pid:
        return pid
    try:
        if program is None:
            raise FileNotFoundError(f"{argv[0]} is not on PATH")
        os.setsid()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        null = os.open("/dev/null", os.O_RDONLY)
        os.dup2(null, 0)
        if log is not None:
            os.dup2(log, 1)
            os.dup2(log, 2)
        if account is not None:
            uid, gid, _, groups = account
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
        os.chdir(cwd)
        os.execve(program, argv, env)
    except BaseException as exc:  # the child must never return into the init loop
        os.write(2, f"ale-init: cannot start {argv[0]}: {exc}\n".encode())
    os._exit(127)


def supervise(config: dict, image: dict) -> None:
    """Run guestd and the image's command until the command exits or the Host asks to stop."""
    hostname = config["hostname"]
    extra = config.get("env", {})
    image_env = image.get("Env") or []
    root_env = _environment(image_env, extra, hostname, "/root")
    root_env["ALE_AGENT_USER"] = config["agent_user"]
    guestd_argv = [
        *GUESTD,
        "--tcp",
        f"0.0.0.0:{config['guest_port']}",
        "--token-file",
        f"{RUN}/guestd-token",
    ]
    account = _account(image.get("User") or "root")
    command = [*(image.get("Entrypoint") or []), *(image.get("Cmd") or [])]
    os.makedirs(os.path.dirname(COMMAND_LOG), exist_ok=True)
    log = os.open(COMMAND_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    guestd = _spawn(guestd_argv, root_env, cwd="/")
    main = None
    if command:
        env = _environment(image_env, extra, hostname, account[2])
        main = _spawn(command, env, cwd=image.get("WorkingDir") or "/", account=account, log=log)
    while True:
        pid, status = os.wait()
        if pid == guestd:
            _log(f"guestd exited ({status}); restarting")
            time.sleep(1)
            guestd = _spawn(guestd_argv, root_env, cwd="/")
        elif pid == main:
            _log(f"the image's command exited ({status}); stopping")
            raise Shutdown


def _on_signal(signum: int, frame: object) -> None:
    raise Shutdown


def _reap_all(seconds: float) -> bool:
    """Reap until no process is left or the time is up. Every process is init's descendant."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return True
        time.sleep(0.05)
    return False


def power_off() -> None:
    """Stop every process, unmount the writable disk cleanly and reset, ending Firecracker.

    Clean matters: a snapshot keeps the writable disk, which a later machine mounts
    read-only, and a read-only mount cannot replay a journal.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.kill(-1, signal.SIGTERM)
    if not _reap_all(10):
        os.kill(-1, signal.SIGKILL)
        _reap_all(5)
    os.closerange(3, 65536)  # the command's log, open for writing on the writable disk
    os.sync()
    for target in ("/", RW):
        try:
            mount("none", target, None, MS_REMOUNT | MS_RDONLY)
        except OSError as exc:
            print(f"ale-init: {exc}", file=sys.stderr, flush=True)
    os.sync()
    _libc.reboot(RB_AUTOBOOT)


def main() -> None:
    _check(_libc.reboot(RB_DISABLE_CAD), "disable Ctrl+Alt+Del")
    config = assemble_root()
    with open(f"{HERE}/image.json") as handle:
        image = json.load(handle)
    mount_basics()
    configure_network(config)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        supervise(config, image)
    except Shutdown:
        pass
    except BaseException as exc:
        _log(f"failed: {exc!r}")
    power_off()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(f"ale-init: {exc!r}", file=sys.stderr, flush=True)
        os.sync()
        _libc.reboot(RB_AUTOBOOT)
