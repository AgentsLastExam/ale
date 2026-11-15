"""What the control plane shows: one document the web page and ``ale-fleet status`` read.

Both read the same JSON, so whatever an operator can see or do on the page they can from
the command line, and the other way round.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ale.core.fleet import SandboxStatus
from ale.fleet.store import Event

__all__ = [
    "Counts",
    "FleetStatus",
    "ImageCopies",
    "NodeStatus",
    "QueueStatus",
    "RunStatus",
    "SandboxRow",
    "TaskProgress",
]

_VIEW = ConfigDict(frozen=True, extra="forbid")


class Counts(BaseModel):
    model_config = _VIEW

    cpus: int
    memory_mb: int
    macos_vm_slots: int
    gpus: int


class NodeStatus(BaseModel):
    model_config = _VIEW

    name: str
    address: str
    online: bool
    draining: bool
    seen_sec_ago: float
    agent_version: str
    engine_commit: str
    platform: str
    labels: dict[str, str]
    profiles: tuple[str, ...]
    total: Counts
    used: Counts
    sandboxes: int
    images: int
    image_bytes: int
    disk_free_gb: int


class SandboxRow(BaseModel):
    model_config = _VIEW

    sandbox_id: str
    run_id: str
    task: str
    """The Task it serves, by its episode or its Run's plan; empty when neither says."""
    image: str
    """The key of the prepared image it runs."""
    node: str | None
    provider: str | None
    status: SandboxStatus
    reason: str
    episode_id: str
    role: str
    age_sec: float
    lease_left_sec: float | None
    queue_sec: float | None
    image_sec: float | None
    create_sec: float | None


class QueueStatus(BaseModel):
    model_config = _VIEW

    run_id: str
    label: str
    waiting: int
    reasons: dict[str, int]
    """Why the waiting Sandboxes wait, each reason with how many it holds back."""


class TaskProgress(BaseModel):
    model_config = _VIEW

    task: str
    episodes: int
    running: int
    completed: int
    failed: int
    infrastructure: int

    waiting: int
    """Planned episodes that have not started."""


class RunStatus(BaseModel):
    model_config = _VIEW

    run_id: str
    label: str
    concurrency: int | None
    tasks: tuple[TaskProgress, ...]
    sandboxes: int
    queue_sec: float | None
    image_sec: float | None
    create_sec: float | None
    """Mean seconds per Sandbox in each start segment, over the Sandboxes that reported it."""


class ImageCopies(BaseModel):
    """The Nodes that prepared one image, by its key."""

    model_config = _VIEW

    key: str
    tasks: tuple[str, ...]
    """The Tasks whose Sandboxes run it, as far as the control plane's Runs said."""
    bytes: int
    """The most any of them takes for it on disk."""
    prepare_sec: float
    """How long preparing it took them, on average."""
    nodes: tuple[str, ...]


class FleetStatus(BaseModel):
    model_config = _VIEW

    nodes: tuple[NodeStatus, ...]
    queue: tuple[QueueStatus, ...]
    sandboxes: tuple[SandboxRow, ...]
    runs: tuple[RunStatus, ...]
    images: tuple[ImageCopies, ...]
    events: tuple[Event, ...]
