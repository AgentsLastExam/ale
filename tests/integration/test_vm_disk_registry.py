"""A VM disk published to, and pulled from, a real OCI distribution registry."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ale.run import images
from tests.support import FakeQemuImg, write_qcow2

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

REGISTRY = "registry:3@sha256:82f6ff4a7f982960c8aa51617fea3aabc485df6e55cce80d22deb1de36f1bc3c"


@pytest.fixture
def registry() -> Iterator[str]:
    name = f"ale-test-registry-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5000", REGISTRY],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"cannot start a registry container: {error}")
    try:
        address = subprocess.run(
            ["docker", "port", name, "5000/tcp"], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://{address}/v2/", timeout=2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        yield address
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


@pytest.mark.asyncio
async def test_a_disk_round_trips_and_resumes_through_a_real_registry(
    tmp_path: Path, registry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry protocol is under test, not qcow2.
    monkeypatch.setattr(images, "_run", FakeQemuImg())
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "no-docker-config"))
    monkeypatch.setattr(images, "CHUNK_SIZE", 1 << 20)
    source = tmp_path / "base.qcow2"
    data = write_qcow2(
        source, data=os.urandom(3 << 20) + bytes(1 << 20) + os.urandom(1 << 19)
    ).read_bytes()
    reference = f"{registry}/ale-test/disk:1.0.0"
    published = await images.publish_vm_image(source, reference)

    target = tmp_path / "disk.qcow2"
    pinned = await images.pull_vm_image(reference, target)
    assert pinned == f"{registry}/ale-test/disk@{published.rpartition('@')[2]}"
    assert target.read_bytes() == data

    # A pull that stopped part-way, with one chunk garbled, is finished from what it left.
    partial = tmp_path / "again.qcow2.partial"
    partial.write_bytes(data[: 2 << 20] + os.urandom(1 << 20))
    with partial.open("r+b") as stream:
        stream.truncate(len(data))
    assert await images.pull_vm_image(pinned, tmp_path / "again.qcow2") == pinned
    assert (tmp_path / "again.qcow2").read_bytes() == data
