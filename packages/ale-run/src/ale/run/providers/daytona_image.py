"""Getting a prepared container image to Daytona, exactly as prepared.

Daytona builds a sandbox from an image reference that its builder can pull. The prepared
image lives in the local Docker store, so it is adapted, pushed to Daytona's own registry
and referenced there by digest. Nothing is rebuilt remotely: the bytes Daytona runs are
the bytes ALE prepared, plus one small layer.

The adaptation layer exists because Daytona starts its own daemon as the image's final
``USER`` and runs every command, including ours over SSH, as that user. The framework
needs root to start guestd, so the layer ends in ``USER root``; it also makes sure the
image has ``iptables``, which the Provider uses to keep the agent away from the daemon's
unauthenticated local ports (see ``daytona.py``).

The registry credentials Daytona hands out expire within the hour, and its builder only
authenticates to registries registered with the organization. The Provider therefore keeps
one organization registry entry (``ale-transient``) holding the current credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.ids import content_hash
from ale.run.images import resolve_loaded_image
from ale.run.providers.daytona_api import DaytonaApi
from ale.run.sources import cache_root

__all__ = ["ImageConfig", "ImagePublisher", "PublishedImage", "image_config"]

#: Bump when the adaptation layer changes, so every image is adapted and pushed again.
ADAPTER_VERSION = 1
REGISTRY_ENTRY = "ale-transient"
REPOSITORY = "ale-task"
_REFRESH_BEFORE = timedelta(minutes=15)

#: POSIX sh, so it works in any Linux image. Mirrors how the framework layer installs sudo.
_FIREWALL_TOOLS = r"""set -eu
if command -v iptables >/dev/null 2>&1 && command -v ip6tables >/dev/null 2>&1; then
    exit 0
fi
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update && apt-get install -y --no-install-recommends iptables
    rm -rf /var/lib/apt/lists/*
elif command -v dnf >/dev/null 2>&1; then dnf install -y iptables && dnf clean all
elif command -v microdnf >/dev/null 2>&1; then microdnf install -y iptables
elif command -v yum >/dev/null 2>&1; then yum install -y iptables && yum clean all
elif command -v apk >/dev/null 2>&1; then apk add --no-cache iptables ip6tables
elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install iptables
else
    echo "ALE: Daytona needs iptables in the image, which has neither it nor a package" \
        "manager to install it with" >&2
    exit 1
fi
if ! command -v iptables >/dev/null 2>&1 || ! command -v ip6tables >/dev/null 2>&1; then
    echo "ALE: installing iptables did not provide iptables and ip6tables" >&2
    exit 1
fi
"""


@dataclasses.dataclass(frozen=True)
class PublishedImage:
    """An adapted image in Daytona's registry."""

    ref: str
    """``<registry>/<project>/ale-task@sha256:<manifest digest>``: what the sandbox builds from."""
    digest: str
    framed_id: str
    """The local image the Task prepared, with the framework layer."""
    adapted_id: str
    """The local image after the Daytona layer, which is what was pushed."""


@dataclasses.dataclass(frozen=True)
class ImageConfig:
    """What Docker would start the image with, which Daytona does not do."""

    argv: tuple[str, ...]
    """Entrypoint followed by command; empty when the image declares neither."""
    env: dict[str, str]
    user: str
    workdir: str


async def image_config(reference: str) -> ImageConfig:
    """The prepared image's own start-up configuration, read from the local image."""
    code, out, stderr = await _docker("image", "inspect", "--format", "{{json .Config}}", reference)
    if code != 0:
        raise ProviderStartError(f"image {reference} could not be inspected: {stderr.strip()}")
    config = json.loads(out) or {}
    environment = dict(
        entry.partition("=")[::2] for entry in config.get("Env") or [] if "=" in entry
    )
    return ImageConfig(
        argv=(*(config.get("Entrypoint") or ()), *(config.get("Cmd") or ())),
        env=environment,
        user=str(config.get("User") or ""),
        workdir=str(config.get("WorkingDir") or ""),
    )


class ImagePublisher:
    """Adapts, pushes and registers images for one Daytona organization."""

    def __init__(self, api: DaytonaApi) -> None:
        self._api = api
        self._access: dict[str, str] | None = None
        self._expires = datetime.min.replace(tzinfo=UTC)
        self._access_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    async def publish(self, framed_id: str, *, force: bool = False) -> PublishedImage:
        """The image in Daytona's registry, pushing it first if it is not there already.

        ``force`` pushes again even when a record says it was pushed, for when Daytona
        reports it lost the image.
        """
        key = content_hash({"adapter": ADAPTER_VERSION, "image": framed_id})
        record = _record_path(key)
        async with self._exclusive(key, record):
            found = None if force else await self._recorded(record, framed_id)
            if found is not None:
                return found
            adapted = await self._adapt(framed_id, key)
            digest, reference = await self._push(adapted, key)
            published = PublishedImage(reference, digest, framed_id, adapted)
            _write(record, published)
            return published

    async def lookup(self, framed_id: str) -> PublishedImage | None:
        """The recorded publication of ``framed_id``, without pushing anything."""
        key = content_hash({"adapter": ADAPTER_VERSION, "image": framed_id})
        return await self._recorded(_record_path(key), framed_id)

    async def refresh_access(self) -> None:
        """Discard cached registry credentials; the next use mints and registers new ones."""
        self._expires = datetime.min.replace(tzinfo=UTC)

    # --- adaptation ------------------------------------------------------------

    async def _adapt(self, framed_id: str, key: str) -> str:
        base = f"ale-daytona-base:{framed_id.removeprefix('sha256:')}"
        code, _, stderr = await _docker("tag", framed_id, base)
        if code != 0:
            raise ProviderStartError(f"could not reference the prepared image: {stderr.strip()}")
        with tempfile.TemporaryDirectory(prefix="ale-daytona-") as temporary:
            context = Path(temporary)
            (context / "Dockerfile").write_text(
                "\n".join(
                    [
                        f"FROM {base}",
                        "USER root",
                        "RUN " + json.dumps(["/bin/sh", "-c", _FIREWALL_TOOLS]),
                        f"LABEL ale.daytona={json.dumps(key)}",
                    ]
                )
                + "\n"
            )
            metadata_file = context / "metadata.json"
            code, _, stderr = await _docker(
                "buildx", "build", "--load", "--provenance=false", "--sbom=false",
                "--tag", f"ale-daytona:{key.removeprefix('sha256:')[:16]}",
                "--metadata-file", str(metadata_file), str(context),
                timeout=1800,
            )  # fmt: skip
            if code != 0:
                explained = [
                    line.split("ALE: ", 1)[1] for line in stderr.splitlines() if "ALE: " in line
                ]
                raise ProviderCapabilityError(
                    "could not adapt the Task image for Daytona: "
                    + (" ".join(explained) if explained else stderr.strip()[-2000:])
                )
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        return await resolve_loaded_image(metadata, run=_run)

    # --- registry --------------------------------------------------------------

    async def _push(self, adapted_id: str, key: str) -> tuple[str, str]:
        access = await self._fresh_access()
        repository = f"{access['registryUrl']}/{access['project']}/{REPOSITORY}"
        remote = f"{repository}:{key.removeprefix('sha256:')[:16]}"
        async with _logged_in(access) as environment:
            code, _, stderr = await _docker("tag", adapted_id, remote)
            if code != 0:
                raise ProviderStartError(f"could not tag the image for Daytona: {stderr.strip()}")
            try:
                code, stdout, stderr = await _docker(
                    "push", remote, environment=environment, timeout=3600
                )
            finally:
                await _docker("rmi", remote)  # the tag only; the image stays
        match = re.search(r"digest:\s*(sha256:[0-9a-f]{64})", stdout)
        if code != 0 or match is None:
            raise ProviderStartError(f"could not push the image to Daytona: {stderr.strip()}")
        return match.group(1), f"{repository}@{match.group(1)}"

    async def _fresh_access(self) -> dict[str, str]:
        async with self._access_lock:
            now = datetime.now(UTC)
            if self._access is None or self._expires - now < _REFRESH_BEFORE:
                access = await self._api.registry_push_access()
                await self._register(access)
                self._access = access
                self._expires = _parse_time(access["expiresAt"])
            return self._access

    async def _register(self, access: dict[str, str]) -> None:
        """Make Daytona's builder able to pull what was pushed with these credentials."""
        body = {
            "name": REGISTRY_ENTRY,
            "url": access["registryUrl"],
            "username": access["username"],
            "password": access["secret"],
            "project": access["project"],
        }
        for entry in await self._api.registries():
            if entry.get("name") == REGISTRY_ENTRY:
                await self._api.update_registry(entry["id"], body)
                return
        await self._api.create_registry(body)

    # --- records ---------------------------------------------------------------

    async def _recorded(self, record: Path, framed_id: str) -> PublishedImage | None:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            published = PublishedImage(**data)
        except (OSError, ValueError, TypeError):
            return None
        if published.framed_id != framed_id:
            return None
        code, observed, _ = await _docker(
            "image", "inspect", "--format", "{{.Id}}", published.adapted_id
        )
        return published if code == 0 and observed.strip() == published.adapted_id else None

    @contextlib.asynccontextmanager
    async def _exclusive(self, key: str, record: Path) -> AsyncIterator[None]:
        """One publication at a time per image, across tasks and across processes."""
        record.parent.mkdir(parents=True, exist_ok=True)
        local = self._locks.setdefault(key, asyncio.Lock())
        async with local:
            with record.with_suffix(".lock").open("a") as handle:
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.1)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _record_path(key: str) -> Path:
    return cache_root() / "daytona" / "images" / f"{key.removeprefix('sha256:')}.json"


def _write(record: Path, published: PublishedImage) -> None:
    partial = record.with_suffix(".partial")
    partial.write_text(json.dumps(dataclasses.asdict(published)), encoding="utf-8")
    os.replace(partial, record)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@contextlib.asynccontextmanager
async def _logged_in(access: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    """A Docker client configuration holding the registry login, kept out of ``~/.docker``."""
    with tempfile.TemporaryDirectory(prefix="ale-docker-") as config:
        environment = {**os.environ, "DOCKER_CONFIG": config}
        code, _, stderr = await _docker(
            "login", access["registryUrl"], "-u", access["username"], "--password-stdin",
            environment=environment, stdin=access["secret"].encode(),
        )  # fmt: skip
        if code != 0:
            raise ProviderStartError(f"could not log in to Daytona's registry: {stderr.strip()}")
        yield environment


async def _run(*argv: str, timeout: float = 300) -> tuple[int, str, str]:
    """``argv`` with its leading ``docker``, the shape ``resolve_loaded_image`` calls."""
    return await _docker(*argv[1:], timeout=timeout)


async def _docker(
    *argv: str,
    environment: dict[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: float = 300,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            raise ProviderStartError(f"docker {argv[0]} timed out after {timeout:g}s") from None
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
