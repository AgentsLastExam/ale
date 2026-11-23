"""Probe, as root in a Linux Sandbox, whether it has the container features named.

The engine runs this file's source on the framework's Python (``python3 -I -c``) with
container feature names as arguments, and reads back one JSON object: for each feature, the
outcome of each of its probes, ``ok`` or the errno name the probe failed with. It uses only
the standard library, through raw x86-64 syscalls, and imports no engine code, so it runs in
any Sandbox the framework is in.
"""

import ctypes
import errno
import json
import os
import socket
import struct
import sys
from collections.abc import Callable

#: The probes that show each container feature, all of which must pass. Starting a
#: container is not a syscall, so ``nested_containers`` has none here; Provider conformance
#: starts one instead.
FEATURES = {
    "nested_containers": (),
    "perf": ("perf_event_open",),
    "bpf": ("bpf_map",),
    "io_uring": ("io_uring",),
    "fuse": ("fuse_device",),
    "raw_sockets": ("raw_socket",),
    "user_namespaces": ("user_namespace",),
    "mount": ("mount_tmpfs", "mount_namespace"),
}

#: The C library, loaded only when the probe runs: the engine imports this module for
#: :data:`FEATURES` on Hosts whose syscalls these are not.
_libc: ctypes.CDLL


def _failed(code: int) -> str:
    return errno.errorcode.get(code, str(code))


def _syscall(number: int, *args: object) -> str:
    """Call a syscall that returns a file descriptor, and close it."""
    ctypes.set_errno(0)
    result = _libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        return _failed(ctypes.get_errno())
    os.close(result)
    return "ok"


def _in_child(probe: Callable[[], str]) -> str:
    """Run ``probe`` in a child process, so a namespace it enters does not change this
    one."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        try:
            outcome = probe()
        except OSError as error:
            outcome = _failed(error.errno)
        except Exception as error:  # reported: the child must reach _exit
            outcome = type(error).__name__
        os.write(write_end, outcome.encode())
        os._exit(0)
    os.close(write_end)
    outcome = os.read(read_end, 200).decode() or "no-result"
    os.close(read_end)
    os.waitpid(pid, 0)
    return outcome


def perf_event_open() -> str:
    # PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CPU_CLOCK; disabled, excluding kernel and
    # hypervisor.
    attr = ctypes.create_string_buffer(struct.pack("IIQQQQQ", 1, 128, 0, 0, 0, 0, 0x61), 128)
    return _syscall(298, attr, 0, -1, -1, 0)


def bpf_map() -> str:
    # BPF_MAP_CREATE of a one-entry BPF_MAP_TYPE_ARRAY.
    attr = ctypes.create_string_buffer(struct.pack("IIII", 2, 4, 4, 1), 64)
    return _syscall(321, 0, attr, 64)


def io_uring() -> str:
    # io_uring_setup with four entries.
    return _syscall(425, 4, ctypes.create_string_buffer(120))


def fuse_device() -> str:
    try:
        os.close(os.open("/dev/fuse", os.O_RDWR))
    except OSError as error:
        return _failed(error.errno)
    return "ok"


def raw_socket() -> str:
    try:
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close()
    except OSError as error:
        return _failed(error.errno)
    return "ok"


def _user_namespace() -> str:
    os.unshare(os.CLONE_NEWUSER)
    return "ok"


def user_namespace() -> str:
    return _in_child(_user_namespace)


def _mount_tmpfs() -> str:
    target = b"/tmp/orbit-capprobe"
    os.makedirs(target, exist_ok=True)
    if _libc.mount(b"none", target, b"tmpfs", 0, None) != 0:
        return _failed(ctypes.get_errno())
    _libc.umount2(target, 0)
    return "ok"


def mount_tmpfs() -> str:
    return _in_child(_mount_tmpfs)


def _mount_namespace() -> str:
    os.unshare(os.CLONE_NEWNS)
    return "ok"


def mount_namespace() -> str:
    return _in_child(_mount_namespace)


def _outcome(probe: Callable[[], str]) -> str:
    try:
        return probe()
    except OSError as error:
        return _failed(error.errno)


if __name__ == "__main__":
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.syscall.restype = ctypes.c_long
    print(
        json.dumps(
            {
                feature: {name: _outcome(globals()[name]) for name in FEATURES[feature]}
                for feature in sys.argv[1:]
            }
        )
    )
