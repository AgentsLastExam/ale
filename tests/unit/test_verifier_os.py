"""A separate verifier on another operating system than its Task's."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support import CONTAINER_BASE, write_task

from ale.core.config import RunConfig
from ale.core.errors import TaskDefinitionError
from ale.core.sandbox import ImageKind, PreparedTaskImage
from ale.core.taskspec import OperatingSystem, VerificationMode
from ale.run.harnesses.builtin import NopHarness
from ale.run.lint import lint_repository
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers import ProviderRegistry
from ale.run.tasksets.manifest import ManifestTask, load_task_folder

pytestmark = pytest.mark.unit

#: A Windows Task whose verifier is a Linux container built from ``tests/Dockerfile``.
WINDOWS_TASK: dict[str, Any] = {
    "artifacts": [r"output\model.3dm"],
    "environment": {"os": "windows", "image": "/images/windows.qcow2", "memory_mb": 4096},
    "agent": {"workdir": "Documents"},
    "verifier": {"environment": {"os": "Linux", "cpus": 1}},
}


def _windows_task(path: Path, config: dict[str, Any] | None = None) -> Path:
    task = write_task(path, config=WINDOWS_TASK | (config or {}), dockerfile=None, solve=None)
    (task / "solution").mkdir()
    (task / "solution/solve.ps1").write_text("Set-Content output\\model.3dm '3dm'\n")
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\n")
    return task


def test_a_windows_task_may_verify_in_a_linux_container(tmp_path: Path) -> None:
    task = _windows_task(tmp_path / "task")
    spec = load_task_folder(task).spec
    assert (spec.os, spec.verify.os, spec.verifier_os) == (
        OperatingSystem.WINDOWS,
        OperatingSystem.LINUX,
        OperatingSystem.LINUX,
    )
    assert spec.verify.environment_mode is VerificationMode.SEPARATE
    assert spec.verify.image is not None and spec.verify.image.kind is ImageKind.CONTAINER
    # The solver's start directory, and the same directory of the verifier's account.
    assert spec.start_directory(r"C:\Users\user") == r"C:\Users\user\Documents"
    assert spec.start_directory("/home/user", OperatingSystem.LINUX) == "/home/user/Documents"
    # Its tests entry is the verifier's system's, its solution the Task's.
    assert lint_repository(task) == []
    (task / "tests/test.sh").rename(task / "tests/test.ps1")
    with pytest.raises(TaskDefinitionError, match=r"tests/test\.sh"):
        load_task_folder(task)
    assert "no tests/test.sh" in "\n".join(str(item) for item in lint_repository(task))


def test_the_verifiers_system_is_the_tasks_unless_it_names_another(tmp_path: Path) -> None:
    spec = load_task_folder(
        write_task(tmp_path / "task", config={"verifier": {"environment": {"cpus": 1}}})
    ).spec
    assert spec.verify.os is None and spec.verifier_os is OperatingSystem.LINUX


@pytest.mark.parametrize(
    ("layer", "message"),
    [
        (
            {"artifacts": [r"output\model.3dm", r"C:\Exports\summary.txt"]},
            r"receives only relative artifacts, not C:\\Exports\\summary\.txt",
        ),
        # Not absolute on Windows, and below no start directory either.
        ({"artifacts": [r"\out\x.txt"]}, r"receives only relative artifacts, not \\out"),
        ({"artifacts": [r"C:out\x.txt"]}, r"receives only relative artifacts, not C:out"),
        ({"agent": {"workdir": r"C:\work"}}, r"needs a relative \[agent\]\.workdir"),
        ({"agent": {"workdir": r"\work"}}, r"needs a relative \[agent\]\.workdir"),
    ],
)
def test_a_verifier_on_another_system_receives_only_relative_paths(
    tmp_path: Path, layer: dict[str, Any], message: str
) -> None:
    with pytest.raises(TaskDefinitionError, match=f"a linux verifier of a windows Task {message}"):
        load_task_folder(_windows_task(tmp_path / "task", layer))


def test_a_verifier_on_another_system_needs_an_image_of_its_own(tmp_path: Path) -> None:
    task = _windows_task(tmp_path / "task")
    # Without one it would run the solver's, which runs Windows.
    (task / "tests/Dockerfile").unlink()
    with pytest.raises(TaskDefinitionError, match="verifier of a windows Task needs an image"):
        load_task_folder(task)


def test_only_a_linux_verifier_builds_tests_dockerfile(tmp_path: Path) -> None:
    task = write_task(
        tmp_path / "task",
        config={"verifier": {"environment": {"os": "windows", "cpus": 1}}},
        test=None,
    )
    (task / "tests").mkdir()
    (task / "tests/test.ps1").write_text("exit 0\n")
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\n")
    with pytest.raises(TaskDefinitionError, match="only a Linux verifier builds"):
        load_task_folder(task)


@pytest.mark.asyncio
async def test_selection_and_provisioning_ask_for_the_verifiers_own_system(
    tmp_path: Path,
) -> None:
    task: ManifestTask = load_task_folder(_windows_task(tmp_path / "task"))
    await ProviderRegistry(
        RunConfig.model_validate({"sandbox": {"providers": ["qemu", "docker:runc"]}})
    ).select([task])
    assert (task.provider, task.verifier_provider) == ("qemu", "docker:runc")

    requested = []

    async def acquire(request):  # type: ignore[no-untyped-def]
        requested.append(request)
        return SimpleNamespace(
            request=request, resolved_image=None, allocation=None, agent_home="/home/user"
        )

    digest = "sha256:" + "a" * 64
    ctx = SimpleNamespace(
        spec=task.spec,
        episode_id="episode",
        session=SimpleNamespace(gateway_url=""),
        sandboxes=SimpleNamespace(acquire=acquire),
        prepared_verifier_image=PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source="verifier-local",
            input_identity=digest,
            image_source_identity=digest,
            runtime_ref="ale-verifier:local",
            prepared_identity=digest,
        ),
    )
    await EpisodeProtocol(NopHarness())._provision_verifier(ctx)  # type: ignore[arg-type]
    assert requested[0].os is OperatingSystem.LINUX
