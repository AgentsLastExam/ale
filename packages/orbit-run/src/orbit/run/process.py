"""Running a Host program to completion from asyncio, and ending one that holds a session."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping

from orbit.core.errors import ProviderStartError

__all__ = ["end", "run"]

#: How long a session's program has to exit once its input ends, before it is killed.
END_TIMEOUT_SEC = 5


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


async def end(process: asyncio.subprocess.Process) -> None:
    """End the session ``process`` holds into a Sandbox as a command ends: close its input,
    which ends the guest's end of it once that reads it, and kill the process only if it is
    still running ``END_TIMEOUT_SEC`` later, or once the caller is cancelled.

    A session killed first may leave its far end unaware it ended: on Slurm the step takes
    Podman's monitor of its ``podman exec`` with it, and Podman, never told the session
    ended, then fails to stop the container for a save (the Slurm Provider's AGENTS.md).
    Nothing waits for a killed process: one whose output nobody reads any more would keep
    that wait from ever returning.
    """
    if process.returncode is not None:
        return
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), END_TIMEOUT_SEC)
    except TimeoutError:
        _kill(process)
    except BaseException:
        _kill(process)
        raise


def _kill(process: asyncio.subprocess.Process) -> None:
    # It may have exited while its output, which nobody reads, held the wait.
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
