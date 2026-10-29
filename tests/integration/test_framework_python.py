"""Every sandbox runs guestd on the framework's own Python, whatever the image has.

The framework's Python is built for glibc, so an image without glibc is refused by name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ale.core.errors import TaskDefinitionError
from ale.core.sandbox import PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run import guestd
from ale.run.environments.standard import StandardEnvironment
from ale.run.framework import MARKER, framework_identity, with_framework
from ale.run.harnesses.builtin import NopHarness
from ale.run.providers.docker import DockerProvider

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

#: Neither has a python3 anywhere; Alpine has musl instead of glibc.
NO_PYTHON = "FROM docker.io/library/ubuntu:24.04\nRUN useradd -m user\n"
MUSL = "FROM docker.io/library/alpine:3.20\nRUN adduser -D user\n"
KEEP_ALIVE = 'CMD ["sleep", "infinity"]\n'


def _built(context: Path, dockerfile: str, tag: str) -> PreparedTaskImage:
    """A local image as Task preparation would hand it on, by its ID."""
    (context / "Dockerfile").write_text(dockerfile + KEEP_ALIVE)
    subprocess.run(
        ["docker", "buildx", "build", "--load", "-q", "-t", tag, str(context)],
        capture_output=True,
        check=True,
    )
    image = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", tag],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return PreparedTaskImage(
        kind="container",
        source="solver-local",
        input_identity=image,
        image_source_identity=image,
        runtime_ref=image,
        prepared_identity=image,
    )


@pytest.mark.asyncio
async def test_an_image_without_python_gets_the_frameworks(tmp_path: Path) -> None:
    provider = DockerProvider()
    request = SandboxRequest(
        episode_id="no-python",
        prepared_image=await provider.prepare_image(
            _built(tmp_path, NO_PYTHON, "ale-test-no-python:latest")
        ),
        resources=Resources(cpus=1, memory_mb=512),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    async with await provider.create(request) as sandbox:
        own = await sandbox.exec(["sh", "-c", "command -v python3 || echo none"])
        assert own.stdout.strip() == "none"
        marker = await sandbox.read_file(MARKER)
        assert marker.decode().strip() == framework_identity(user="user", sudo=False, vm=False)
        version = await sandbox.exec(
            [guestd.PYTHON["linux"], "-c", "import sys; print(sys.version_info[:2])"]
        )
        assert version.stdout.strip() == "(3, 12)", version.stderr
        # Verification has no Python of the image's to use, so it gets the framework's.
        ctx = SimpleNamespace(spec=SimpleNamespace(os=OperatingSystem.LINUX))
        python, site = await StandardEnvironment(NopHarness())._site_packages(ctx, sandbox)  # type: ignore[arg-type]
        assert python == guestd.PYTHON["linux"] and site.startswith("/opt/ale/python/")


@pytest.mark.asyncio
async def test_an_image_without_glibc_is_refused_by_name(tmp_path: Path) -> None:
    image = _built(tmp_path, MUSL, "ale-test-musl:latest")
    with pytest.raises(TaskDefinitionError, match="glibc"):
        await with_framework(image.runtime_ref, user="user", sudo=False)
