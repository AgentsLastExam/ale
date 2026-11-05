"""The agent's elevation is set at every Linux and macOS sandbox start, not in the image.

The script runs here on a copy of the files it edits; the Docker identity tests run it
against real sudo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError, TaskDefinitionError
from ale.core.taskspec import OperatingSystem
from ale.run import guestd
from ale.run.providers.privileges import set_elevation, verify_identities

pytestmark = pytest.mark.unit

LINUX, MACOS = OperatingSystem.LINUX, OperatingSystem.MACOS
#: An account every Linux host has, so the script can look it up, and that is not root.
USER = "nobody"
MARK = "# ALE: the agent's elevation in this sandbox, set at every start"


class Guest:
    """Runs what the Provider sends on this machine, with ``/etc`` moved under ``root``."""

    def __init__(self, root: Path, system: str = "linux", prelude: str = "") -> None:
        self.root, self.system, self.prelude = root, system, prelude

    async def exec(self, argv, *, run_as=None, timeout_sec=None):  # type: ignore[no-untyped-def]
        python, *rest = argv
        assert python == guestd.PYTHON[self.system] and run_as is None
        rest = [arg.replace('"/etc/', f'"{self.root}/etc/') for arg in rest]
        rest[rest.index("-c") + 1] = self.prelude + rest[rest.index("-c") + 1]
        done = subprocess.run([sys.executable, *rest], capture_output=True, text=True, check=False)
        return done.returncode, done.stdout, done.stderr, False


@pytest.fixture
def etc(tmp_path: Path) -> Path:
    files = tmp_path / "etc"
    files.mkdir()
    (files / "sudoers").write_text(
        "Defaults env_reset\n%sudo ALL=(ALL:ALL) ALL\n@includedir /etc/sudoers.d\n"
    )
    (files / "group").write_text(
        f"root:x:0:\nsudo:x:27:alice,{USER}\nwheel:x:10:{USER}\nadm:x:4:{USER}\n"
    )
    (files / "gshadow").write_text(f"sudo:*:{USER}:alice,{USER}\nadm:*::{USER}\n")
    return files


@pytest.mark.asyncio
async def test_revoking_leaves_the_agent_no_way_up_and_granting_replaces_it(etc: Path) -> None:
    guest = Guest(etc.parent)
    await set_elevation(guest, USER, sudo=False, operating_system=LINUX)  # type: ignore[arg-type]
    sudoers = (etc / "sudoers").read_text().splitlines()
    # Sudo applies the last rule that matches, so the image's own rules no longer count.
    assert sudoers[-2:] == [MARK, f"{USER} ALL=(ALL:ALL) !ALL"]
    assert sudoers[:3] == [
        "Defaults env_reset",
        "%sudo ALL=(ALL:ALL) ALL",
        "@includedir /etc/sudoers.d",
    ]
    assert (etc / "group").read_text() == (
        f"root:x:0:\nsudo:x:27:alice\nwheel:x:10:\nadm:x:4:{USER}\n"
    )
    assert (etc / "gshadow").read_text() == f"sudo:*::alice\nadm:*::{USER}\n"

    # A Sandbox continued from that one, by a request that asks for elevation.
    await set_elevation(guest, USER, sudo=True, operating_system=LINUX)  # type: ignore[arg-type]
    sudoers = (etc / "sudoers").read_text().splitlines()
    assert sudoers.count(MARK) == 1
    assert sudoers[-2:] == [MARK, f"{USER} ALL=(ALL:ALL) NOPASSWD: ALL"]


@pytest.mark.asyncio
async def test_revoking_in_an_image_without_sudo_writes_no_policy(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    await set_elevation(Guest(tmp_path), USER, sudo=False, operating_system=LINUX)  # type: ignore[arg-type]
    assert not (tmp_path / "etc" / "sudoers").exists()


@pytest.mark.asyncio
async def test_a_root_agent_can_only_be_an_elevated_one(etc: Path) -> None:
    before = (etc / "sudoers").read_text()
    with pytest.raises(TaskDefinitionError, match=r"root.*\[agent\]\.sudo = true"):
        await set_elevation(Guest(etc.parent), "root", sudo=False, operating_system=LINUX)  # type: ignore[arg-type]
    assert (etc / "sudoers").read_text() == before
    await set_elevation(Guest(etc.parent), "root", sudo=True, operating_system=LINUX)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_requested_grant_that_cannot_work_says_why() -> None:
    class Probed:
        async def exec(self, argv, *, run_as=None, timeout_sec=None):  # type: ignore[no-untyped-def]
            return 0, "0 yes" if run_as is None else "1000 none", "", False

    with pytest.raises(ProviderCapabilityError, match="has no sudo"):
        await verify_identities(Probed(), "user", sudo=True)  # type: ignore[arg-type]
    await verify_identities(Probed(), "user", sudo=False)  # type: ignore[arg-type]


#: Run first: the script sees macOS, and Directory Services answers from a log.
_MACOS = """
import subprocess, sys
sys.platform = "darwin"
def _run(argv, **kwargs):
    with open(LOG, "a") as log:
        log.write(" ".join(argv) + "\\n")
    member = argv[1:4] == ["-o", "checkmember", "-m"] and argv[-1] == "admin"
    return subprocess.CompletedProcess(argv, 0 if member or "checkmember" not in argv else 67)
subprocess.run = _run
"""


@pytest.mark.asyncio
async def test_macos_revokes_through_sudoers_and_the_admin_group(etc: Path, tmp_path: Path) -> None:
    log = tmp_path / "directory.log"
    group = (etc / "group").read_text()
    guest = Guest(etc.parent, "macos", f"LOG = {str(log)!r}\n" + _MACOS)
    await set_elevation(guest, USER, sudo=False, operating_system=MACOS)  # type: ignore[arg-type]
    assert (etc / "sudoers").read_text().splitlines()[-2:] == [MARK, f"{USER} ALL=(ALL:ALL) !ALL"]
    calls = log.read_text().splitlines()
    # Only a group the account is in is edited; /etc/group means nothing to macOS.
    assert f"/usr/sbin/dseditgroup -o edit -d {USER} -t user admin" in calls
    assert not [call for call in calls if " edit " in call and not call.endswith(" admin")]
    assert calls[-1] == "/usr/bin/dscacheutil -flushcache"
    assert (etc / "group").read_text() == group
