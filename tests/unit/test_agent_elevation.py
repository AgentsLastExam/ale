"""The agent's elevation is set at every Linux sandbox start, not built into the image.

The script runs here on a copy of the files it edits; the Docker identity tests run it
against real sudo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ale.core.errors import ProviderCapabilityError, TaskDefinitionError
from ale.run import guestd
from ale.run.providers.privileges import set_elevation, verify_identities

pytestmark = pytest.mark.unit

#: An account every Linux host has, so the script can look it up, and that is not root.
USER = "nobody"
MARK = "# ALE: the agent's elevation in this sandbox, set at every start"


class Guest:
    """Runs what the Provider sends on this machine, with ``/etc`` moved under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def exec(self, argv, *, run_as=None, timeout_sec=None):  # type: ignore[no-untyped-def]
        python, *rest = argv
        assert python == guestd.PYTHON["linux"] and run_as is None
        rest = [arg.replace('"/etc/', f'"{self.root}/etc/') for arg in rest]
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
    await set_elevation(guest, USER, sudo=False)  # type: ignore[arg-type]
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
    await set_elevation(guest, USER, sudo=True)  # type: ignore[arg-type]
    sudoers = (etc / "sudoers").read_text().splitlines()
    assert sudoers.count(MARK) == 1
    assert sudoers[-2:] == [MARK, f"{USER} ALL=(ALL:ALL) NOPASSWD: ALL"]


@pytest.mark.asyncio
async def test_revoking_in_an_image_without_sudo_writes_no_policy(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    await set_elevation(Guest(tmp_path), USER, sudo=False)  # type: ignore[arg-type]
    assert not (tmp_path / "etc" / "sudoers").exists()


@pytest.mark.asyncio
async def test_a_root_agent_can_only_be_an_elevated_one(etc: Path) -> None:
    before = (etc / "sudoers").read_text()
    with pytest.raises(TaskDefinitionError, match=r"root.*\[agent\]\.sudo = true"):
        await set_elevation(Guest(etc.parent), "root", sudo=False)  # type: ignore[arg-type]
    assert (etc / "sudoers").read_text() == before
    await set_elevation(Guest(etc.parent), "root", sudo=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_requested_grant_that_cannot_work_says_why() -> None:
    class Probed:
        async def exec(self, argv, *, run_as=None, timeout_sec=None):  # type: ignore[no-untyped-def]
            return 0, "0 yes" if run_as is None else "1000 none", "", False

    with pytest.raises(ProviderCapabilityError, match="has no sudo"):
        await verify_identities(Probed(), "user", sudo=True)  # type: ignore[arg-type]
    await verify_identities(Probed(), "user", sudo=False)  # type: ignore[arg-type]
