"""The Windows framework installer: what it reads from a disk's registry and what it writes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ale.run import framework_windows as installer

pytestmark = pytest.mark.unit

WINLOGON = r"\Microsoft\Windows NT\CurrentVersion\Winlogon"
POLICIES = r"\Microsoft\Windows\CurrentVersion\Policies\System"


def _sz(text: str) -> str:
    """A string as hivex exports it: UTF-16 bytes."""
    return "hex(1):" + ",".join(f"{byte:02x}" for byte in (text + "\0").encode("utf-16-le"))


def _key(hive: str, path: str, **entries: str) -> str:
    lines = ["Windows Registry Editor Version 5.00", "", f"[HKEY_LOCAL_MACHINE\\{hive}{path}]"]
    lines += [f'"{name}"={value}' for name, value in entries.items()]
    return "\n".join(lines) + "\n"


def _disk(
    *,
    users: tuple[str, ...] = ("Administrator", "User"),
    logon: str = "User",
    auto: str = "1",
    uac: str = "dword:00000001",
    current: str = "dword:00000001",
) -> dict[tuple[str, str], str]:
    names = "\n".join(
        f"[HKEY_LOCAL_MACHINE\\SAM\\SAM\\Domains\\Account\\Users\\Names\\{user}]\n@=hex(3e9):\n"
        for user in users
    )
    return {
        ("SAM", r"\SAM\Domains\Account\Users\Names"): (
            "[HKEY_LOCAL_MACHINE\\SAM\\SAM\\Domains\\Account\\Users\\Names]\n@=hex(0):\n\n" + names
        ),
        ("SOFTWARE", WINLOGON): _key(
            "SOFTWARE", WINLOGON, AutoAdminLogon=_sz(auto), DefaultUserName=_sz(logon)
        ),
        ("SOFTWARE", POLICIES): _key("SOFTWARE", POLICIES, EnableLUA=uac),
        ("SYSTEM", r"\Select"): _key("SYSTEM", r"\Select", Current=current),
    }


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Serve exported registry text for the keys a test sets."""

    def serve(keys: dict[tuple[str, str], str]) -> None:
        monkeypatch.setattr(installer, "export", lambda hives, hive, key: keys.get((hive, key), ""))

    return serve


def test_registry_text_is_read_as_hivex_exports_it() -> None:
    text = _key(
        "SOFTWARE",
        WINLOGON,
        Plain=_sz("User"),
        Quoted=r'"C:\\Python312\\"',
        Number="dword:00000001",
    ) + _key("SOFTWARE", WINLOGON + r"\Notify", Plain=_sz("elsewhere"))
    assert installer.values(text, WINLOGON) == {
        "Plain": "User",
        "Quoted": "C:\\Python312\\",
        "Number": "dword:00000001",
    }


def test_a_disk_with_the_account_and_its_autologon_passes(registry) -> None:  # type: ignore[no-untyped-def]
    registry(_disk(logon="USER"))  # no Python: the framework brings its own
    installer.check(Path("/hives"), "user")


@pytest.mark.parametrize(
    ("fault", "complaint"),
    [
        ({"users": ("Administrator",)}, "agent account 'user' does not exist"),
        ({"logon": "Administrator"}, "does not sign in 'user' automatically"),
        ({"auto": "0"}, "does not sign in 'user' automatically"),
        ({"uac": "dword:00000000"}, "User Account Control is off"),
    ],
)
def test_a_disk_that_cannot_run_the_framework_says_why(
    registry,  # type: ignore[no-untyped-def]
    fault: dict[str, object],
    complaint: str,
) -> None:
    registry(_disk(**fault))  # type: ignore[arg-type]
    with pytest.raises(installer.Lacks, match=complaint):
        installer.check(Path("/hives"), "user")


def test_the_service_is_registered_in_the_control_set_the_disk_boots_from(registry) -> None:  # type: ignore[no-untyped-def]
    registry(_disk(current="dword:00000002"))
    text = installer.service_key(Path("/hives"), "user", "172.30.0.1")
    assert "[HKEY_LOCAL_MACHINE\\SYSTEM\\ControlSet002\\Services\\ALEGuestd]" in text
    assert '"Start"=dword:00000002' in text and '"ObjectName"="LocalSystem"' in text
    assert (
        '"ImagePath"="\\"C:\\\\ProgramData\\\\ALE\\\\python\\\\python.exe\\" -I -S '
        '\\"C:\\\\ProgramData\\\\ALE\\\\guestd\\\\service.py\\" \\"user\\" 172.30.0.1"'
    ) in text
    registry({})
    with pytest.raises(installer.Lacks, match="no control set"):
        installer.service_key(Path("/hives"), "user", "172.30.0.1")


def test_the_framework_is_installed_and_its_marker_written_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry,  # type: ignore[no-untyped-def]
) -> None:
    registry(_disk())
    edits: list[tuple[bool, str]] = []
    merged: list[str] = []
    monkeypatch.setattr(
        installer,
        "guestfish",
        lambda disk, commands, *, readonly=False: edits.append((readonly, commands)),
    )
    monkeypatch.setattr(installer, "merge", lambda hive, text: merged.append(text))
    identity = "sha256:" + "a" * 64
    monkeypatch.setattr(
        sys,
        "argv",
        ["install.py", str(tmp_path / "d.qcow2"), "/framework", "/python", "--user", "user",
         "--identity", identity, "--runner-address", "172.30.0.1"],
    )  # fmt: skip
    assert installer.main() == 0
    *reads, install = edits
    assert [readonly for readonly, _ in reads] == [True, True, True]  # hives, read without writing
    assert len(merged) == 1 and "ControlSet001" in merged[0]
    assert not install[0]
    steps = [line.strip() for line in install[1].strip().splitlines()]
    assert steps[:3] == [
        "mkdir-p /ProgramData/ALE",
        "rm-rf /ProgramData/ALE/guestd",
        "rm-rf /ProgramData/ALE/cua-driver",
    ]
    assert "copy-in /framework/guestd /ProgramData/ALE" in steps
    assert "copy-in /framework/cua-driver /ProgramData/ALE" in steps
    assert {"rm-rf /ProgramData/ALE/python", "copy-in /python /ProgramData/ALE"} <= set(steps)
    assert steps[-1] == f'write /ProgramData/ALE/framework "{identity}\\n"'
    assert steps[-2].startswith("upload ") and steps[-2].endswith("/Windows/System32/config/SYSTEM")


def test_a_disk_that_lacks_something_prints_one_line_and_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registry,  # type: ignore[no-untyped-def]
) -> None:
    registry(_disk(users=("Administrator",)))
    edits: list[str] = []
    monkeypatch.setattr(
        installer, "guestfish", lambda disk, commands, *, readonly=False: edits.append(commands)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["install.py", str(tmp_path / "d.qcow2"), "/framework", "/python", "--user", "user",
         "--identity", "sha256:" + "a" * 64, "--runner-address", "172.30.0.1"],
    )  # fmt: skip
    assert installer.main() == 1
    assert capsys.readouterr().err.startswith("ALE: the agent account 'user' does not exist")
    assert all("upload" not in commands for commands in edits)  # only the reads ran


def test_a_hibernated_disk_is_not_written_to(monkeypatch: pytest.MonkeyPatch) -> None:
    def hibernated(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 1, "", "libguestfs: error: mount: Windows is hibernated, refused to mount"
        )

    monkeypatch.setattr(installer.subprocess, "run", hibernated)
    with pytest.raises(installer.Lacks, match="not shut down completely"):
        installer.guestfish(Path("/d.qcow2"), "ls /")
