"""When a Provider that holds a Sandbox to its ``storage_mb`` by measuring what it wrote
measures it next.

A measurement walks what the Sandbox wrote, which costs more the more files it holds, and
between two measurements the Sandbox can write past its limit as fast as the disk takes it.
So the next one comes when the Sandbox could first reach its limit at that speed: often
where little is left, rarely where much is, within bounds. A Sandbox of a small limit, or
near a large one, is measured every :data:`MIN_SEC`.
"""

from __future__ import annotations

__all__ = ["MAX_SEC", "MIN_SEC", "WRITE_RATE", "interval", "measure_schedule"]

#: The fastest a Sandbox writes to a disk, in bytes a second: measured, one writing with
#: ``dd`` reached bursts of 2 GiB a second on an NVMe disk (Providers README).
WRITE_RATE = 2 << 30
#: The bounds: at most one walk every 5 s, and nothing it holds unseen for over 5 minutes.
MIN_SEC = 5.0
MAX_SEC = 300.0


def interval(headroom: int) -> float:
    """Seconds until the next measurement of a Sandbox ``headroom`` bytes below its limit."""
    return min(max(headroom / WRITE_RATE, MIN_SEC), MAX_SEC)


def measure_schedule(storage_mb: int, writers: int = 1) -> dict[str, float]:
    """What a Provider's measurer elsewhere, which cannot import this module, computes
    :func:`interval` from, where ``writers`` machines write to what it measures, as every
    node of a Sandbox of several writes the shared directory its node 0 counts."""
    return dict(storage_mb=storage_mb, rate=WRITE_RATE * writers, min_sec=MIN_SEC, max_sec=MAX_SEC)
