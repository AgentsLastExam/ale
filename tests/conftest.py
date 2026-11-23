from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from orbit.core.errors import ProviderCapabilityError
from tests.support import CONTAINER_BASE, write_task

# Typer forces Rich terminal styling whenever GITHUB_ACTIONS is set, which splits option
# names in help text with escape codes. Tests read the same plain output everywhere.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")


# A unit test drives fakes, which never time out, so one that waits for something its fake
# never does would block the whole suite. It fails after a minute instead, with the stack it
# was waiting in.
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("unit") and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(60))


@pytest.fixture(autouse=True)
def default_orbit_repo_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ORBIT_REPO_PATH",
        str(Path(__file__).resolve().parents[1]),
    )


# Git hooks and `git rebase --exec` export GIT_DIR and related variables to the commands
# they run. Every Git command a test starts must act on its own temporary repository.
@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def x86_host_without_gpus(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit test runs as on an x86-64 Host with no GPU, which Orbit's Tasks and leaves
    assume, whatever machine runs it; a test of another Host says so itself."""
    if request.node.get_closest_marker("unit") is None:
        return

    async def no_gpus(_provider: object) -> None:
        raise ProviderCapabilityError("nvidia-smi is not installed on the Docker host")

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr("orbit.run.providers.docker.provider.DockerProvider._host_gpus", no_gpus)


@pytest.fixture
def orbit_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "orbit"
    (root / "packages" / "orbit-run").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "test-orbit"\n')
    monkeypatch.setenv("ORBIT_REPO_PATH", str(root))
    return root


@pytest.fixture
def write_task_repo(tmp_path: Path) -> Callable[..., Path]:
    def write(
        name: str = "tasks-example",
        *,
        tasks: tuple[str, ...] = ("demo",),
        asset_roots: tuple[str, ...] = (),
        config: Mapping[str, Any] | None = None,
        dockerfile: str | None = f"FROM {CONTAINER_BASE}\n",
        verifier_dockerfile: bool = False,
    ) -> Path:
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for task_name in tasks:
            task = write_task(
                root / "tasks" / task_name,
                name=f"agentslastexam/{task_name}",
                config=config,
                dockerfile=dockerfile,
            )
            if verifier_dockerfile:
                (task / "tests" / "Dockerfile").write_text(f"FROM {CONTAINER_BASE}\n")
            for relative in asset_roots:
                assets = task / relative
                assets.mkdir(parents=True, exist_ok=True)
                (assets / "fixture.txt").write_text(relative, encoding="utf-8")
        return root

    return write
