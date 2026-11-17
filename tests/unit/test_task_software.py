"""A Task that names licensed software instead of an image."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.support import CONTAINER_BASE, edit_task_config, write_task

from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError, TaskDefinitionError
from ale.core.taskspec import ImageKind, ImageSpec, OperatingSystem
from ale.run.lint import lint_repository
from ale.run.providers import ProviderRegistry
from ale.run.tasksets.manifest import load_task_folder

pytestmark = pytest.mark.unit

#: A Windows Task on a machine that has its software, verified in a Linux container.
SOFTWARE_TASK: dict[str, Any] = {
    "artifacts": [r"output\model.3dm"],
    "environment": {"os": "windows", "software": ["rhino-7", "materials-studio"]},
    "agent": {"user": None, "workdir": "Documents"},
    "verifier": {"environment": {"os": "linux", "cpus": 1}},
}


def _software_task(path: Path) -> Path:
    task = write_task(path, config=SOFTWARE_TASK, dockerfile=None, solve=None)
    (task / "solution").mkdir()
    (task / "solution/solve.ps1").write_text("Set-Content output\\model.3dm '3dm'\n")
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\nRUN pip install rhino3dm\n")
    return task


def test_a_task_names_software_instead_of_an_image(tmp_path: Path) -> None:
    task = _software_task(tmp_path / "task")
    loaded = load_task_folder(task)
    # Sorted, so the order a Task lists them in changes no identity.
    assert loaded.spec.image == ImageSpec(
        kind=ImageKind.STATION, software=("materials-studio", "rhino-7")
    )
    assert loaded.spec.os is OperatingSystem.WINDOWS
    assert loaded.image_source_digest is None
    assert lint_repository(task) == []


@pytest.mark.parametrize(
    ("layer", "message"),
    [
        ({"environment": {"image": "/images/windows.qcow2"}}, "software replaces an image"),
        ({"agent": {"user": "user"}}, r"decides the agent's account; remove \[agent\]\.user"),
        ({"environment": {"software": []}}, "at least 1 item"),
        ({"environment": {"software": ["rhino-7", "rhino-7"]}}, "rhino-7 listed more than once"),
        ({"environment": {"software": ["rhino,7"]}}, "should match pattern"),
        # What one lease of a Station can name.
        ({"environment": {"software": ["r" * 65]}}, "should match pattern"),
        ({"environment": {"software": [f"s{n:02}" for n in range(17)]}}, "at most 16 items"),
        ({"artifacts": [r"C:\Exports\model.3dm"]}, r"only relative artifacts, not C:\\Exports"),
        ({"artifacts": [r"\out\model.3dm"]}, r"only relative artifacts, not \\out"),
        ({"artifacts": [r"C:out\model.3dm"]}, r"only relative artifacts, not C:out"),
        ({"agent": {"workdir": r"C:\work"}}, r"needs a relative \[agent\]\.workdir"),
        ({"agent": {"workdir": r"\work"}}, r"needs a relative \[agent\]\.workdir"),
        (
            {"environment": {"container_features": ["perf"]}},
            "the solver names software, which takes no container_features",
        ),
    ],
)
def test_a_task_that_names_software_states_nothing_its_machine_decides(
    tmp_path: Path, layer: dict[str, Any], message: str
) -> None:
    task = _software_task(tmp_path / "task")
    edit_task_config(task, layer)
    with pytest.raises(TaskDefinitionError, match=message):
        load_task_folder(task)


@pytest.mark.parametrize("build", ["Dockerfile", "build.ps1", "build.sh"])
def test_a_task_that_names_software_builds_no_image(tmp_path: Path, build: str) -> None:
    task = _software_task(tmp_path / "task")
    (task / "environment").mkdir()
    (task / "environment" / build).write_text("\n")
    with pytest.raises(TaskDefinitionError, match=f"no image to build; remove environment/{build}"):
        load_task_folder(task)


def test_a_task_that_names_software_is_verified_on_an_image_of_its_own(tmp_path: Path) -> None:
    task = _software_task(tmp_path / "task")
    (task / "tests/Dockerfile").unlink()
    (task / "tests/test.sh").rename(task / "tests/test.ps1")
    # Shared on its machine, or separate on its image: neither is allowed.
    for verifier in ({"environment": None}, {"environment": None, "environment_mode": "separate"}):
        edit_task_config(task, {"verifier": verifier})
        with pytest.raises(TaskDefinitionError, match="needs a separate verifier with an image"):
            load_task_folder(task)


def test_setup_installing_software_belongs_on_the_machine(tmp_path: Path) -> None:
    task = _software_task(tmp_path / "task")
    (task / "environment/setup").mkdir(parents=True)
    (task / "environment/setup/setup.ps1").write_text("winget install Rhino\n")
    (finding,) = lint_repository(task)
    assert finding.message.startswith(
        "fixed installation or download belongs on the machine that has [environment].software"
    )


@pytest.mark.asyncio
async def test_only_a_station_serves_software(tmp_path: Path) -> None:
    task = load_task_folder(_software_task(tmp_path / "task"))
    registry = ProviderRegistry(
        RunConfig.model_validate({"sandbox": {"providers": ["docker:runc", "qemu"]}})
    )
    with pytest.raises(ProviderCapabilityError) as refused:
        await registry.select([task])
    assert str(refused.value) == (
        "no Provider in sandbox.providers (docker:runc, qemu) serves this Sandbox:\n"
        "  agentslastexam/demo solver:\n"
        "    docker:runc: starts container Sandboxes, not station; runs linux, not windows\n"
        "    qemu: starts vm Sandboxes, not station"
    )
    settings = {"providers": ["docker:runc", "qemu", "station"], "station": {"destination": "ws"}}
    registry = ProviderRegistry(RunConfig.model_validate({"sandbox": settings}))
    with pytest.raises(ProviderCapabilityError, match="station: network block is not one it"):
        await registry.select([task])
    # A Station's Lanes share its network, so it serves a Task whose agent may go anywhere.
    edit_task_config(task.folder.root, {"agent": {"network_mode": "public"}})
    task = load_task_folder(task.folder.root)
    await registry.select([task])
    # The solver on the Station, its verifier in a container of its own.
    assert (task.provider, task.verifier_provider) == ("station", "docker:runc")
