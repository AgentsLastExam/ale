from __future__ import annotations

import json
from pathlib import Path

import pytest

from ale.run import framework, guestd
from ale.run.framework import LABEL, framework_identity, with_framework
from ale.run.guestd import gui

pytestmark = pytest.mark.unit


def test_the_layer_identity_follows_everything_it_is_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = framework_identity(user="user", sudo=False, vm=False)
    assert framework_identity(user="user", sudo=False, vm=False) == base
    assert framework_identity(user="app", sudo=False, vm=False) != base
    assert framework_identity(user="user", sudo=True, vm=False) != base
    assert framework_identity(user="user", sudo=False, vm=True) != base
    monkeypatch.setattr(framework, "_SETUP", "true\n")
    assert framework_identity(user="user", sudo=False, vm=False) != base


@pytest.mark.asyncio
async def test_the_layer_is_built_on_the_task_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALE_CACHE_DIR", str(tmp_path))
    driver = tmp_path / "cua-driver-binary"
    driver.write_bytes(b"driver")
    monkeypatch.setattr(framework, "_cua_driver", lambda: driver)
    built: dict[str, str] = {}
    images = {"task": {"Id": "sha256:" + "1" * 64, "User": "app", "Labels": {}}}

    async def inspect(reference: str, *, missing_ok: bool = False) -> dict:
        if reference in images:
            return images[reference]
        assert missing_ok
        return {}

    async def run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
        if argv[:3] == ("docker", "buildx", "build"):
            context = Path(argv[-1])
            built["dockerfile"] = (context / "Dockerfile").read_text()
            built["files"] = sorted(path.name for path in context.rglob("*"))
            built["unit"] = (context / "ale-guestd.service").read_text()
            tag = argv[argv.index("--tag") + 1]
            identity = framework_identity(user="app", sudo=True, vm=True)
            images[tag] = {"Id": "sha256:" + "2" * 64, "Labels": {LABEL: identity}}
        return 0, "", ""

    monkeypatch.setattr(framework, "_inspect", inspect)
    monkeypatch.setattr(framework, "_run", run)
    layered = await with_framework("task", user="app", sudo=True, vm=True)
    assert layered == "sha256:" + "2" * 64
    dockerfile = built["dockerfile"]
    assert dockerfile.startswith("FROM ale-framework-base:" + "1" * 64)
    assert "COPY guestd/ /opt/ale/guestd/" in dockerfile
    assert "COPY --chmod=0755 cua-driver /usr/local/bin/cua-driver" in dockerfile
    assert json.dumps(["/bin/sh", "/opt/ale/setup.sh", "app", "true"]) in dockerfile
    assert dockerfile.rstrip().splitlines()[-2] == "USER app"
    assert set(guestd.FILES) <= set(built["files"])
    assert "Environment=ALE_AGENT_USER=app" in built["unit"]
    assert f"--tcp 0.0.0.0:{guestd.PORT}" in built["unit"]
    # The same layer again is a no-op.
    images["task"] = {**images["task"], "Labels": {}}
    assert await with_framework("task", user="app", sudo=True, vm=True) == layered


def test_a_desktop_is_recognized_by_its_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui, "_PROGRAM_DIRS", (str(tmp_path),))
    assert not gui.has_desktop()
    (tmp_path / "Xvnc").write_text("")
    assert gui.has_desktop()


def test_a_failed_call_starts_the_driver_once_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    started: list[bool] = []

    def invoke(tool: str, arguments: object) -> dict:
        calls.append(tool)
        if not started:
            raise gui.GuiUnavailable("not serving")
        return {"width": 10, "height": 20}

    monkeypatch.setattr(gui, "_invoke", invoke)
    monkeypatch.setattr(gui, "_start_server", lambda: started.append(True) or True)
    assert gui.screen_size() == (10, 20)
    assert calls == ["get_screen_size", "get_screen_size"]
    assert started == [True]
