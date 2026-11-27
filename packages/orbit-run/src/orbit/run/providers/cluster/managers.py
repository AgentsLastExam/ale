"""Each workload manager's jobs: the batch job the login node submits, an image's
preparation or an episode's, and the allocation Orbit holds in the job it runs in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from orbit.core.config import (
    ClusterPartition,
    ClusterProviderConfig,
    LSFProviderConfig,
    PBSProviderConfig,
    SlurmProviderConfig,
)
from orbit.run.providers.cluster.job import Allocation, CurrentAllocation, Job
from orbit.run.providers.cluster.lsf import LSFAllocation, LSFJob
from orbit.run.providers.cluster.pbs import PBSAllocation, PBSJob
from orbit.run.providers.cluster.slurm import SlurmAllocation, SlurmJob

#: By the cluster's configuration.
JOBS: dict[type[ClusterProviderConfig], type[SlurmJob | PBSJob | LSFJob]] = {
    SlurmProviderConfig: SlurmJob,
    PBSProviderConfig: PBSJob,
    LSFProviderConfig: LSFJob,
}
ALLOCATIONS: dict[type[ClusterProviderConfig], type[Allocation]] = {
    SlurmProviderConfig: SlurmAllocation,
    PBSProviderConfig: PBSAllocation,
    LSFProviderConfig: LSFAllocation,
}


def create_job(config: ClusterProviderConfig, partition: ClusterPartition) -> Job:
    return JOBS[type(config)](config, partition)  # type: ignore[arg-type]


def spans_nodes(config: ClusterProviderConfig) -> bool:
    """Whether one of the cluster's Sandboxes may span several nodes of its job."""
    return JOBS[type(config)].spans_nodes


async def preflight(config: ClusterProviderConfig, partitions: Sequence[ClusterPartition]) -> None:
    """Check, from the login node, that the cluster runs ``partitions``' jobs."""
    await JOBS[type(config)].preflight(config, partitions)


def current_allocation(
    config: ClusterProviderConfig,
    *,
    cpus: int,
    memory_mb: int,
    gpus: int,
    walltime_minutes: int,
    started_at: datetime,
) -> CurrentAllocation:
    """The job this process runs in (:meth:`CurrentAllocation.from_environment`)."""
    return CurrentAllocation.from_environment(
        ALLOCATIONS[type(config)],
        config,
        cpus=cpus,
        memory_mb=memory_mb,
        gpus=gpus,
        walltime_minutes=walltime_minutes,
        started_at=started_at,
    )


def create_allocation(config: ClusterProviderConfig, current: CurrentAllocation) -> Allocation:
    return ALLOCATIONS[type(config)](config, current)
