"""Install the ALE framework into a Windows VM disk without booting it.

Run by ``framework.frame_disk`` inside the VM builder, never on the Host: libguestfs mounts
the disk from a small appliance VM and edits the registry hives with hivex, so nothing the
disk holds executes. Standard library only, since it runs on the builder's system Python.

What it installs is the Windows counterpart of the Linux layer: guestd and Cua Driver under
``C:\\ProgramData\\ALE``, a boot service that starts them in the agent's session
(``guestd/service.py``), and the identity of the framework in ``C:\\ProgramData\\ALE\\framework``.
It checks first that the disk can run them, and says what it lacks in a line starting
``ALE: ``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = "/Windows/System32/config"
ROOT = "/ProgramData/ALE"
SERVICE = "ALEGuestd"
#: Python the service may run guestd with; older ones lack what guestd uses.
MINIMUM_PYTHON = (3, 12)
#: What libguestfs says when NTFS was not shut down cleanly and so mounts read-only.
UNCLEAN = ("hibernat", "unclean", "unsafe state", "read-only file system")


class Lacks(Exception):
    """The disk cannot run the framework; the message says why."""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("disk", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--runner-address", required=True)
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="ale-hives-") as temporary:
            hives = Path(temporary)
            guestfish(args.disk, f"download {CONFIG}/SYSTEM {hives}/SYSTEM", readonly=True)
            guestfish(args.disk, f"download {CONFIG}/SOFTWARE {hives}/SOFTWARE", readonly=True)
            guestfish(args.disk, f"download {CONFIG}/SAM {hives}/SAM", readonly=True)
            python = check(hives, args.user)
            merge(hives / "SYSTEM", service_key(hives, python, args.user, args.runner_address))
            guestfish(
                args.disk,
                f"""
                mkdir-p {ROOT}
                rm-rf {ROOT}/guestd
                rm-rf {ROOT}/cua-driver
                copy-in {args.source}/guestd {ROOT}
                copy-in {args.source}/cua-driver {ROOT}
                upload {hives}/SYSTEM {CONFIG}/SYSTEM
                write {ROOT}/framework "{args.identity}\\n"
                """,
            )
    except Lacks as reason:
        print(f"ALE: {reason}", file=sys.stderr)
        return 1
    return 0


def guestfish(disk: Path, commands: str, *, readonly: bool = False) -> None:
    """One appliance boot that runs ``commands`` with the disk's Windows volume mounted."""
    argv = ["guestfish", "--format=qcow2", "-a", str(disk), "-i"]
    if readonly:
        argv.insert(1, "--ro")
    done = subprocess.run(argv, input=commands, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        if any(sign in detail.lower() for sign in UNCLEAN):
            raise Lacks(
                "the Windows disk was not shut down completely (left hibernated by Fast Startup, "
                "or after a crash); boot it once and shut it down fully with Fast Startup off"
            )
        raise Lacks(f"could not edit the Windows disk: {detail[-1500:]}")


def export(hives: Path, hive: str, key: str) -> str:
    """The registry text of one key and everything below it, or "" if the key is absent."""
    done = subprocess.run(
        ["hivexregedit", "--export", "--prefix", f"HKEY_LOCAL_MACHINE\\{hive}",
         str(hives / hive), key],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    return done.stdout


def check(hives: Path, user: str) -> str:
    """Raise unless the disk can run the framework for ``user``; return its Python."""
    names = export(hives, "SAM", r"\SAM\Domains\Account\Users\Names")
    if user.casefold() not in {name.casefold() for name in key_names(names)}:
        raise Lacks(
            f"the agent account '{user}' does not exist on this Windows disk; create it "
            f"or name an existing account with image.user in task.yaml"
        )
    winlogon = values(
        export(hives, "SOFTWARE", r"\Microsoft\Windows NT\CurrentVersion\Winlogon"),
        r"\Microsoft\Windows NT\CurrentVersion\Winlogon",
    )
    if winlogon.get("AutoAdminLogon") != "1" or (
        winlogon.get("DefaultUserName", "").casefold() != user.casefold()
    ):
        raise Lacks(
            f"this Windows disk does not sign in '{user}' automatically; the agent's "
            f"session starts at boot, so Winlogon needs AutoAdminLogon=1 and "
            f"DefaultUserName={user}"
        )
    policies = r"\Microsoft\Windows\CurrentVersion\Policies\System"
    if values(export(hives, "SOFTWARE", policies), policies).get("EnableLUA") == "dword:00000000":
        raise Lacks("User Account Control is off on this Windows disk; the framework needs it on")
    python = newest_python(export(hives, "SOFTWARE", r"\Python\PythonCore"))
    if python is None:
        raise Lacks(
            "this Windows disk has no Python "
            f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer installed for all users"
        )
    return python


def key_names(text: str) -> list[str]:
    """The names of the keys below the first exported key."""
    keys = [line[1:-1] for line in text.splitlines() if line.startswith("[")]
    return [key.rsplit("\\", 1)[-1] for key in keys[1:]]


def values(text: str, key: str) -> dict[str, str]:
    """The values set directly on ``key`` in exported registry text.

    Strings are decoded, from the quoted form or from hivex's ``hex(1):``/``hex(2):``
    UTF-16 bytes; every other type is returned as written, e.g. ``dword:00000001``.
    """
    found: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("["):
            inside = line[1:-1].casefold().endswith(key.casefold())
        elif inside and (match := re.fullmatch(r'"([^"]+)"=(.*)', line)):
            name, raw = match.groups()
            if raw.startswith('"'):
                found[name] = unquote(raw[1:-1])
            elif strings := re.fullmatch(r"hex\([12]\):((?:[0-9a-f]{2},?)*)", raw):
                data = bytes.fromhex(strings[1].replace(",", ""))
                found[name] = data.decode("utf-16-le", "replace").rstrip("\0")
            else:
                found[name] = raw
    return found


def unquote(text: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", text)


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def newest_python(text: str) -> str | None:
    """The ``python.exe`` of the newest all-users Python that guestd can run on."""
    best: tuple[tuple[int, ...], str] | None = None
    for key in (line[1:-1] for line in text.splitlines() if line.startswith("[")):
        match = re.search(r"\\PythonCore\\(\d+)\.(\d+)\\InstallPath$", key)
        if match is None:
            continue
        version = (int(match[1]), int(match[2]))
        path = values(text, key).get("ExecutablePath", "")
        if version >= MINIMUM_PYTHON and path and (best is None or version > best[0]):
            best = (version, path)
    return best[1] if best else None


def control_set(hives: Path) -> str:
    """The control set the disk boots from, which is where services are registered."""
    current = values(export(hives, "SYSTEM", r"\Select"), r"\Select").get("Current", "")
    match = re.fullmatch(r"dword:([0-9a-f]{8})", current)
    if match is None or not 1 <= int(match[1], 16) <= 999:
        raise Lacks("this Windows disk records no control set to boot from")
    return f"ControlSet{int(match[1], 16):03d}"


def service_key(hives: Path, python: str, user: str, runner_address: str) -> str:
    """The registry text that makes Windows start the framework's service at boot."""
    script = r"C:\ProgramData\ALE\guestd\service.py"
    image = f'"{python}" -I -S "{script}" "{user}" {runner_address}'
    return "\r\n".join(
        [
            "Windows Registry Editor Version 5.00",
            "",
            f"[HKEY_LOCAL_MACHINE\\SYSTEM\\{control_set(hives)}\\Services\\{SERVICE}]",
            '"Type"=dword:00000010',  # its own process
            '"Start"=dword:00000002',  # automatic, at boot
            '"ErrorControl"=dword:00000001',
            f'"ImagePath"={quote(image)}',
            '"ObjectName"="LocalSystem"',
            '"DisplayName"="ALE guest service"',
            "",
        ]
    )


def merge(hive: Path, registry_text: str) -> None:
    """Apply ``registry_text`` to a downloaded SYSTEM hive."""
    with tempfile.NamedTemporaryFile("w", suffix=".reg", encoding="utf-8", newline="") as file:
        file.write(registry_text)
        file.flush()
        done = subprocess.run(
            ["hivexregedit", "--merge", "--prefix", "HKEY_LOCAL_MACHINE\\SYSTEM",
             str(hive), file.name],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
    if done.returncode != 0:
        raise Lacks(f"could not register the framework service: {done.stderr.strip()}")


if __name__ == "__main__":
    raise SystemExit(main())
