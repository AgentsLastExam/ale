from __future__ import annotations

import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml


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
        stage_assets: tuple[str, ...] = (),
        manifest_suffix: str = "",
        image: dict[str, str] | None = None,
        with_image_dockerfile: bool = True,
        verifier_image: dict[str, str] | None = None,
        with_verifier_dockerfile: bool = False,
    ) -> Path:
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for task_name in tasks:
            task = root / "tasks" / task_name
            for directory in ("image", "verify", "oracle"):
                (task / directory).mkdir(parents=True, exist_ok=True)
            manifest: dict[str, object] = {
                "spec_type": "core/v1",
                "name": task_name,
                "image": image or {"kind": "container"},
            }
            if verifier_image is not None:
                manifest["verify"] = {
                    "environment_mode": "separate",
                    "image": verifier_image,
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            (task / "task.yaml").write_text(
                yaml.safe_dump(manifest, sort_keys=False) + manifest_suffix,
                encoding="utf-8",
            )
            (task / "instruction.md").write_text("Do the task.\n", encoding="utf-8")
            if with_image_dockerfile:
                (task / "image" / "Dockerfile").write_text(
                    "FROM ghcr.io/agentslastexam/sandbox-base-cli:latest\n",
                    encoding="utf-8",
                )
            if with_verifier_dockerfile:
                (task / "verify" / "Dockerfile").write_text(
                    "FROM ghcr.io/agentslastexam/sandbox-base-cli:latest\n",
                    encoding="utf-8",
                )
            for stage in ("verify", "oracle"):
                entry = task / stage / "run.sh"
                entry.write_text(
                    textwrap.dedent(
                        """\
                        #!/usr/bin/env bash
                        set -euo pipefail
                        """
                    ),
                    encoding="utf-8",
                )
                entry.chmod(0o755)
            for stage in stage_assets:
                assets = task / stage / "assets"
                assets.mkdir(parents=True, exist_ok=True)
                (assets / "fixture.txt").write_text(stage, encoding="utf-8")
        return root

    return write
