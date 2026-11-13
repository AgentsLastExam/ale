"""Running a Host program to completion from asyncio."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ale.core.errors import ProviderStartError

__all__ = ["run"]


async def run(
    *argv: str,
    timeout: float,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``argv``; its exit code, and its stdout and stderr decoded as UTF-8.

    A program still running when ``timeout`` expires, or when the caller is cancelled, is
    killed and reaped, so neither leaves it behind. A timeout raises
    :class:`ProviderStartError`: a Host command that hangs is the environment's failure,
    which an episode must not record as its phase timing out.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if env is None else dict(env),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            raise ProviderStartError(f"{' '.join(argv[:3])} timed out after {timeout:g}s") from None
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
