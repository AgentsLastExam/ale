"""The Fleet against the shared Provider contract: a control plane and a Docker Node.

Both run as their own processes on this machine, as `ale fleet serve` and `ale fleet node`
would, so every Sandbox the suite creates goes through placement, a grant, the node agent's
API and the Gateway tunnel before it reaches the Node's Docker Provider.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from tests.support import CONTAINER_BASE

from ale.core.sandbox import ImageRef
from ale.core.testkit import ProviderConformance
from ale.run.framework import framework_identity
from ale.run.providers.fleet import FleetClient, FleetProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_docker]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


TOKEN = secrets.token_urlsafe(16)
CONTROL_PORT = _free_port()
NODE_PORT = _free_port()
CONTROL = f"http://127.0.0.1:{CONTROL_PORT}"
ALE = str(Path(sys.executable).with_name("ale"))


def _nodes() -> list[dict]:
    request = urllib.request.Request(
        f"{CONTROL}/v1/nodes", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


@pytest.fixture(scope="module", autouse=True)
def fleet(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    root = tmp_path_factory.mktemp("fleet")
    env = os.environ | {"ALE_FLEET_TOKEN": TOKEN, "ALE_CACHE_DIR": str(root / "node-cache")}
    logs = [(root / "control.log").open("wb"), (root / "node.log").open("wb")]
    processes = [
        subprocess.Popen(
            [ALE, "fleet", "serve", "--host", "127.0.0.1", "--port", str(CONTROL_PORT),
             "--db", str(root / "fleet.db")],
            env=env, stdout=logs[0], stderr=subprocess.STDOUT,
        ),
        subprocess.Popen(
            [ALE, "fleet", "node", "--control-plane", CONTROL,
             "--address", f"http://127.0.0.1:{NODE_PORT}", "--port", str(NODE_PORT),
             "--name", "conformance", "--provider", "docker"],
            env=env, stdout=logs[1], stderr=subprocess.STDOUT,
        ),
    ]  # fmt: skip
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                if any(node["online"] for node in _nodes()):
                    break
            except OSError:
                pass
            if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                pytest.fail(
                    "the Fleet did not start: "
                    + (root / "node.log").read_text(errors="replace")[-2000:]
                )
            time.sleep(0.5)
        yield
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(60)
            except subprocess.TimeoutExpired:
                process.kill()
        for log in logs:
            log.close()


@pytest.fixture(autouse=True)
async def close_connections() -> AsyncIterator[None]:
    """Each test has its own event loop, and the Run's connection to the Fleet is per loop."""
    yield
    await TestFleetProvider.provider.client.close()  # type: ignore[attr-defined]


class TestFleetProvider(ProviderConformance):
    """Every assertion in the shared suite, through the Fleet to a Docker Node.

    Its Node prepares every image itself, so a Sandbox runs the image the Node prepared for
    the request's.
    """

    provider = FleetProvider(FleetClient(CONTROL, TOKEN))
    image = ImageRef(kind="container", reference=CONTAINER_BASE)
    gui_image = ImageRef(kind="container", reference=CONTAINER_BASE)
    framework = framework_identity(user="user", vm=False)

    @pytest.mark.asyncio
    async def test_ready_sandbox_reports_observed_image_and_resources(self) -> None:
        request = await self.request()
        async with await self.provider.create(request) as sandbox:
            prepared = sandbox.request.prepared_image
            assert request.prepared_image.prepared_identity != prepared.prepared_identity
            assert sandbox.resolved_image.prepared_identity == prepared.prepared_identity
            assert sandbox.resolved_image.observed_identity.startswith("sha256:")
            assert sandbox.resolved_image.observed_ref
            assert sandbox.allocation.cpus == request.resources.cpus
            assert sandbox.allocation.memory_mb == request.resources.memory_mb
            assert sandbox.allocation.network_mode is request.network.mode
            assert sandbox.allocation.provider == self.provider.name
            assert sandbox.allocation.node == "conformance/docker"
