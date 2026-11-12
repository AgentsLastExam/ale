"""The microVM backend against the same suite the container backend passes.

It runs the container base itself, not a VM disk: the point of this backend is that a
container Task needs nothing new to run as a microVM. Needs KVM, ``/dev/net/tun``, Docker
and the runner image (``images/build.sh ale-firecracker-runner``).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import uuid
from collections.abc import Iterator

import pytest
from tests.support import CONTAINER_BASE

from ale.core.errors import GuestUnreachableError, ProviderCapabilityError
from ale.core.sandbox import Identity, ImageRef, PreparedTaskImage, SandboxRequest
from ale.core.taskspec import NetworkMode, NetworkPolicy, Resources
from ale.core.testkit import ProviderConformance
from ale.run.framework import framework_identity
from ale.run.providers.firecracker import FirecrackerProvider

pytestmark = [pytest.mark.conformance, pytest.mark.needs_kvm, pytest.mark.needs_docker]


@pytest.fixture(autouse=True)
def _kvm() -> None:
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        pytest.skip("/dev/kvm is unavailable")


class TestFirecrackerProvider(ProviderConformance):
    """Every assertion in the shared suite, run against a microVM of the container base."""

    image = ImageRef(kind="container", reference=CONTAINER_BASE)
    #: The base starts its own desktop under Xvnc, which runs in a microVM unchanged.
    gui_image = image
    framework = framework_identity(user="user", vm=False)

    @pytest.fixture(autouse=True)
    def _provider(self) -> None:
        self.provider = FirecrackerProvider()

    async def request(self, **overrides: object) -> SandboxRequest:
        # A microVM's memory holds its kernel and page cache as well as the image's
        # processes, and the base starts a GNOME session.
        resources = overrides.get("resources", Resources())
        assert isinstance(resources, Resources)
        if resources.gpus == 0:
            overrides["resources"] = resources.model_copy(
                update={"memory_mb": max(resources.memory_mb, 2048)}
            )
        return await super().request(**overrides)


def _build(dockerfile: str) -> str:
    """Build an image and return its ID, which serves as its prepared identity."""
    built = subprocess.run(
        ["docker", "build", "--quiet", "-"],
        input=dockerfile.encode(),
        check=True,
        capture_output=True,
    )
    return built.stdout.decode().strip()


def _request(image: str, **overrides: object) -> SandboxRequest:
    base: dict[str, object] = {
        "episode_id": f"firecracker-{uuid.uuid4().hex[:8]}",
        "prepared_image": PreparedTaskImage(
            kind="container",
            source="solver-local",
            input_identity=image,
            image_source_identity=image,
            runtime_ref=image,
            prepared_identity=image,
        ),
        "resources": Resources(cpus=1, memory_mb=512),
        "network": NetworkPolicy(mode=NetworkMode.BLOCK),
    }
    return SandboxRequest(**(base | overrides))  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def plain_image() -> Iterator[str]:
    """A headless image whose command, account, directory and variables are its own."""
    image = _build(
        "FROM debian:trixie-slim\n"
        "RUN useradd --create-home --uid 1000 user && useradd --uid 1500 runner \\\n"
        "    && mkdir /work && chown runner /work\n"
        "ENV GREETING=hello\n"
        "USER runner\n"
        "WORKDIR /work\n"
        'ENTRYPOINT ["sh", "-c"]\n'
        'CMD ["printf \'%s %s %s\' \\"$(id -un)\\" \\"$PWD\\" \\"$GREETING\\" > /work/started'
        ' && exec sleep infinity"]\n'
    )
    yield image
    subprocess.run(["docker", "rmi", "--force", image], check=False, capture_output=True)


@pytest.mark.asyncio
async def test_the_image_command_runs_as_its_user_in_its_workdir_with_its_env(
    plain_image: str,
) -> None:
    """The init does what Docker does with an image's configuration."""
    provider = FirecrackerProvider()
    async with await provider.create(
        _request(plain_image, env={"TASK_VARIABLE": "set"})
    ) as sandbox:
        assert await sandbox.read_file("/work/started") == b"runner /work hello"
        agent = await sandbox.exec(
            ["sh", "-c", 'printf "%s %s" "$GREETING" "$TASK_VARIABLE"'], identity=Identity.AGENT
        )
        assert agent.stdout == "hello set"
        assert sandbox.allocation.desktop_resolution is None
        with pytest.raises(Exception):  # noqa: B017 — guestd reports the missing desktop
            await sandbox.screenshot()
        kernel = await sandbox.exec(["uname", "-r"])
        assert kernel.ok and kernel.stdout.strip() != os.uname().release


@pytest.mark.asyncio
async def test_sandboxes_of_one_image_share_its_disk_and_not_their_writes(
    plain_image: str,
) -> None:
    provider = FirecrackerProvider()
    request = _request(plain_image)
    async with await provider.create(request) as first, await provider.create(request) as second:
        assert first.resolved_image.observed_ref == second.resolved_image.observed_ref
        await first.write_file("/etc/written-by-first", b"first")
        assert (await first.exec(["test", "-e", "/etc/written-by-first"])).ok
        assert not (await second.exec(["test", "-e", "/etc/written-by-first"])).ok


@pytest.mark.asyncio
async def test_an_image_without_a_command_is_refused_before_it_boots() -> None:
    image = _build("FROM debian:trixie-slim\nRUN useradd --create-home user\nCMD []\n")
    try:
        provider = FirecrackerProvider()
        request = _request(image)
        with pytest.raises(ProviderCapabilityError, match="no command"):
            await provider.prepare_image(request.prepared_image)
        with pytest.raises(ProviderCapabilityError, match="no command"):
            await provider.create(request)
    finally:
        subprocess.run(["docker", "rmi", "--force", image], check=False, capture_output=True)


#: The pids of ``sleep`` processes, from ``/proc``: the image has no ``pkill``.
_SLEEP_PIDS = """awk '$2 == "(sleep)" {print $1}'"""


@pytest.mark.asyncio
async def test_a_sandbox_stops_when_its_command_exits(plain_image: str) -> None:
    """A microVM lives exactly as long as its image's command, as a container does."""
    provider = FirecrackerProvider()
    sandbox = await provider.create(_request(plain_image))
    try:
        # The machine stops with its command, so the reply to this one may never come.
        with contextlib.suppress(GuestUnreachableError):
            await sandbox.exec(["sh", "-c", f"kill $(cat /proc/[0-9]*/stat | {_SLEEP_PIDS})"])
        state = subprocess.run(
            ["docker", "wait", sandbox.container],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert state.stdout.strip() == "0"
    finally:
        await sandbox.destroy()
