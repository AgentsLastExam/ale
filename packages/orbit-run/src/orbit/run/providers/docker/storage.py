"""How a Docker leaf holds a Sandbox to its Task's ``storage_mb``.

Docker gives a container no writable storage of a fixed size under its containerd image
store or under rootless Docker, so a leaf measures what each Sandbox that names
``storage_mb`` has written over its image instead, and ends it once that passes the limit,
as Kubernetes holds a Pod to its ``ephemeral-storage``. What it has written is the disk its
writable layer takes, ``SizeRw`` of ``docker container inspect --size``, which the daemon
counts from outside the Sandbox, and what it wrote to its volumes, which a helper outside it
measures (volumes.py). This module measures and decides; the Provider acts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from ale.core.errors import ProviderStartError
from ale.run.process import run

__all__ = ["WATCH_SEC", "Watch", "written"]

_log = logging.getLogger(__name__)

#: How often a watched Sandbox is measured. A measurement walks its writable layer, about a
#: second of the daemons' CPU per 100,000 files, and meanwhile a Sandbox writes past its
#: limit at up to the disk's speed ([Providers README](../README.md)).
WATCH_SEC = 5.0


async def written(container: str) -> int | None:
    """What ``container`` has written over its image, in bytes: the disk its writable layer
    takes, whoever wrote it. None once it is gone."""
    code, raw, stderr = await run(
        "docker", "container", "inspect", "--size", "--format", "{{json .SizeRw}}", container,
        timeout=60,
    )  # fmt: skip
    if code != 0:
        if "no such container" in stderr.lower():
            return None
        raise ProviderStartError(f"could not measure what {container} wrote: {stderr.strip()}")
    return int(json.loads(raw) or 0)


class Watch:
    """Measures what one Sandbox has written every :data:`WATCH_SEC` until it passes
    ``limit_mb``, then has it act, once, with the bytes measured.

    A task of its own per Sandbox, so that a layer of many files, slow to measure, delays no
    other Sandbox's measurement.
    """

    def __init__(
        self,
        container: str,
        limit_mb: int,
        act: Callable[[int], Awaitable[None]],
        *,
        base: int = 0,
        volumes: Callable[[], Awaitable[int]] | None = None,
    ):
        self.container = container
        #: What the Sandbox may write over its image, its Task's ``storage_mb``, in MiB.
        self.limit_mb = limit_mb
        #: What the snapshot it booted from holds over that image, in bytes, which counts too.
        self.base = base
        #: What it has written to its volumes, measured with each measurement of its layer.
        self._volumes = volumes
        self._act = act
        self._task = asyncio.get_running_loop().create_task(self._poll())

    def stop(self) -> None:
        """Measure no more; an action under way is cancelled too."""
        self._task.cancel()

    async def _poll(self) -> None:
        """Measure until it passes its limit or is gone, warning once when measurements
        start to fail and once when they work again."""
        failing = False
        while True:
            await asyncio.sleep(WATCH_SEC)
            try:
                size = await written(self.container)
                if size is not None and self._volumes is not None:
                    size += await self._volumes()
            except ProviderStartError:
                if not failing:
                    _log.warning(
                        "cannot measure what %s wrote; goes on trying",
                        self.container,
                        exc_info=True,
                    )
                failing = True
                continue
            if failing:
                _log.warning("measures what %s wrote again", self.container)
            failing = False
            if size is None:
                return
            if size + self.base > self.limit_mb << 20:
                await self._act(size + self.base)
                return
