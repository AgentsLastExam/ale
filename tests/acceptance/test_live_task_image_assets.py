from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ale.core.sandbox import SandboxRequest
from ale.run.providers.docker import DockerProvider
from ale.run.task_images import prepare_task_image
from ale.run.tasksets.manifest import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.needs_docker]


def write_task(repository: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    path = repository / "tasks/live-image-asset"
    for directory in ("image", "verify", "oracle"):
        (path / directory).mkdir(parents=True, exist_ok=True)
    (path / "task.yaml").write_text(
        "spec_type: core/v1\nname: live-image-asset\nimage: {kind: container}\n",
        encoding="utf-8",
    )
    (path / "instruction.md").write_text("Inspect /home/user/input/public.txt.\n")
    (path / "image" / "Dockerfile").write_text(
        "FROM ghcr.io/agentslastexam/container-ubuntu22-base:latest\n"
        "COPY assets/public.txt /home/user/input/public.txt\n"
        "RUN chown -R user:user /home/user/input\n"
    )
    (path / "image" / "assets").mkdir()
    (path / "image" / "assets" / "public.txt").write_text("public")
    (path / "verify" / "private-marker").write_text("must-not-enter-image")
    (path / "verify" / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (path / "oracle" / "private-marker").write_text("must-not-enter-image")
    (path / "oracle" / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for entry in (path / "verify" / "run.sh", path / "oracle" / "run.sh"):
        entry.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_fixed_image_context_is_loaded_and_withholds_other_stages(
    tmp_path: Path,
) -> None:
    task_path = write_task(tmp_path / "ale-tasks-live")
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
