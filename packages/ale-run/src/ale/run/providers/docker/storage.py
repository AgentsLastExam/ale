"""How a Docker leaf holds a Sandbox to its Task's ``storage_mb``.

Docker gives a container no writable storage of a fixed size under its containerd image
store or under rootless Docker, so a leaf measures what each Sandbox that names
``storage_mb`` has written over its image instead, and ends it once that passes the limit,
as Kubernetes holds a Pod to its ``ephemeral-storage``, its ``emptyDir`` volumes included.
What it has written is the disk its writable layer takes, ``SizeRw`` of
``docker container inspect --size``, which the daemon counts from outside the Sandbox, and the
disk its Docker volumes take, which a helper container outside it measures alike
(:class:`~ale.run.providers.docker.volumes.Meter`). This module measures and decides; the
Provider acts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from ale.core.errors import ProviderStartError
from ale.run.process import run
from ale.run.providers.docker import volumes

__all__ = ["WATCH_SEC", "Watch", "written"]

_log = logging.getLogger(__name__)

#: How often a watched Sandbox is measured. A measurement walks its writable layer and its
#: volumes, about a second of CPU per 100,000 files, and meanwhile a Sandbox writes past its
#: limit at up to the disk's speed ([Providers README](../README.md)).
WATCH_SEC = 5.0


async def written(container: str) -> int | None:
    """What ``container`` has written to its writable layer, in bytes: the disk the layer
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
    other Sandbox's measurement. A Sandbox with Docker volumes has its ``meter`` measure them
    meanwhile, and what they take counts too.
    """

    def __init__(
        self,
        container: str,
        limit_mb: int,
        act: Callable[[int], Awaitable[None]],
        meter: volumes.Meter | None = None,
    ):
        self.container = container
        #: What the Sandbox may write over its image, its Task's ``storage_mb``, in MiB.
        self.limit_mb = limit_mb
        self._act = act
        self._meter = meter
        self._task = asyncio.get_running_loop().create_task(self._poll())

    async def stop(self) -> None:
        """Measure no more, an action under way included, and end the meter's helper, which
        holds the Sandbox's volumes until Docker has removed it; one Docker does not remove
        is logged by name, and the Sandbox's removal goes on."""
        self._task.cancel()
        if self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)
        if self._meter is not None:
            try:
                await self._meter.close()
            except ProviderStartError as error:
                _log.warning("%s keeps %s's volumes: %s", self._meter.name, self.container, error)

    async def _written(self) -> int | None:
        """What the Sandbox has written, its volumes included; None once it is gone."""
        if self._meter is None:
            return await written(self.container)
        layer, on_volumes = await asyncio.gather(
            written(self.container), self._meter.measure(), return_exceptions=True
        )
        if layer is None:
            return None
        for result in (layer, on_volumes):
            if isinstance(result, BaseException):
                raise result
        assert isinstance(layer, int) and isinstance(on_volumes, int)
        return layer + on_volumes

    async def _poll(self) -> None:
        """Measure until it passes its limit or is gone, warning once when measurements
        start to fail and once when they work again."""
        failing = False
        while True:
            await asyncio.sleep(WATCH_SEC)
            try:
                size = await self._written()
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
            if size > self.limit_mb << 20:
                await self._act(size)
                return
