from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ale.core.errors import ProviderStartError
from ale.run import guestd
from ale.run.providers.qemu import guest_service_identity, install_guest_service

pytestmark = pytest.mark.unit


def _rootfs(tmp_path: Path, *stale: tuple[str, bytes]) -> Path:
    archive = tmp_path / "rootfs.tar"
    with tarfile.open(archive, "w") as rootfs:
        for name, content in stale:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            rootfs.addfile(member, io.BytesIO(content))
    return archive


def test_the_engine_guest_service_replaces_any_the_image_carries(tmp_path: Path) -> None:
    archive = _rootfs(tmp_path, ("opt/ale/guestd/main.py", b"old guestd"))
    install_guest_service(archive, user="user", gui=True)
    root = tmp_path / "root"
    with tarfile.open(archive) as rootfs:
        rootfs.extractall(root, filter="tar")
        members = {member.name: member for member in rootfs.getmembers()}
    for name in guestd.FILES:
        installed = root / "opt/ale/guestd" / name
        assert installed.read_bytes() == (guestd.SOURCE / name).read_bytes()
        assert members[f"opt/ale/guestd/{name}"].mode == 0o600
    assert members["opt/ale"].mode == 0o700
    unit = (root / "etc/systemd/system/ale-guestd.service").read_text()
    assert "Environment=ALE_AGENT_USER=user" in unit
    assert "Environment=ALE_GUI=true" in unit
    assert "After=network.target" in unit
    assert "network-online.target" not in unit
    link = members["etc/systemd/system/multi-user.target.wants/ale-guestd.service"]
    assert link.issym() and link.linkname == "/etc/systemd/system/ale-guestd.service"


def test_an_invalid_agent_account_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProviderStartError, match="agent account"):
        install_guest_service(_rootfs(tmp_path), user="user\nExecStart=/bin/sh", gui=False)


def test_the_guest_service_identity_follows_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    before = guest_service_identity()
    monkeypatch.setattr("ale.run.providers.qemu._GUEST_UNIT", "[Unit]\n")
    assert guest_service_identity() != before
