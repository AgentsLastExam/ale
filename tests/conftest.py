from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.support import CONTAINER_BASE, write_task

# Typer forces Rich terminal styling whenever GITHUB_ACTIONS is set, which splits option
# names in help text with escape codes. Tests read the same plain output everywhere.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")


@pytest.fixture(autouse=True)
def default_ale_repo_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ALE_REPO_PATH",
        str(Path(__file__).resolve().parents[1]),
    )


@pytest.fixture
def ale_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ale"
    (root / "packages" / "ale-run").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "test-ale"\n')
    monkeypatch.setenv("ALE_REPO_PATH", str(root))
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
