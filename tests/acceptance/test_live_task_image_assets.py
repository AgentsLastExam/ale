from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ale.core.sandbox import SandboxRequest
from ale.run.providers.docker import DockerProvider
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, provider_registry, write_task

pytestmark = [pytest.mark.needs_docker]


def write_asset_task(repository: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    path = write_task(
        repository / "tasks/live-image-asset",
        name="agentslastexam/live-image-asset",
        dockerfile=(
            f"FROM {CONTAINER_BASE}\n"
            "COPY assets/public.txt /home/user/input/public.txt\n"
            "RUN chown -R user:user /home/user/input\n"
        ),
        instruction="Inspect /home/user/input/public.txt.\n",
        test="#!/usr/bin/env bash\nexit 0\n",
        solve="#!/usr/bin/env bash\nexit 0\n",
    )
    (path / "environment" / "assets").mkdir()
    (path / "environment" / "assets" / "public.txt").write_text("public")
    (path / "environment" / "setup").mkdir()
    (path / "environment" / "setup" / "private-marker").write_text("must-not-enter-image")
    (path / "tests" / "private-marker").write_text("must-not-enter-image")
    (path / "solution" / "private-marker").write_text("must-not-enter-image")
    return path


@pytest.mark.asyncio
async def test_fixed_image_context_is_loaded_and_withholds_other_stages(
    tmp_path: Path,
) -> None:
    task_path = write_asset_task(tmp_path / "ale-tasks-live")
    task = load_tasks(task_path)[0]
    provider = DockerProvider()
    prepared = await prepare_task_image(task, provider_registry(provider))
    request = SandboxRequest(
        episode_id="live-image-asset",
        prepared_image=prepared,
        resources=task.spec.resources,
        network=task.spec.network,
    )
    async with await provider.create(request) as sandbox:
        result = await sandbox.exec(
            [
                "sh",
                "-c",
                'test "$(cat /home/user/input/public.txt)" = public '
                "&& ! find / -name private-marker -print -quit 2>/dev/null | grep -q .",
            ]
        )
        assert result.ok, result.stderr
