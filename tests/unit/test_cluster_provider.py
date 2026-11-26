"""The cluster Provider, on Slurm: an episode's job sized on the login node, and that job,
inside it, as the one allocation of the episode's Sandboxes."""

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbit.core.capability import SandboxNeeds
from orbit.core.config import RunConfig, SlurmPartition, SlurmProviderConfig
from orbit.core.errors import (
    ProviderCapabilityError,
    ProviderCapacityError,
    ProviderStartError,
    SandboxHandOff,
    TaskDefinitionError,
)
from orbit.core.sandbox import (
    ImageKind,
    ImageRef,
    PreparedTaskImage,
    SandboxRequest,
    SandboxRole,
    snapshot_verifier_needs,
    verifiers_needs,
)
from orbit.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from orbit.run.oci import Reference, api, qualified
from orbit.run.providers import ProviderRegistry
from orbit.run.providers.cluster import images, managers, worker
from orbit.run.providers.cluster import job as job_module
from orbit.run.providers.cluster import slurm as slurm_module
from orbit.run.providers.cluster.job import Allocation, CurrentAllocation, JobReady
from orbit.run.providers.cluster.provider import (
    ENGINE_CPUS,
    ENGINE_OVERHEAD_MB,
    JOB_OVERHEAD_MB,
    MAX_LAYERS,
    MIN_LEFT_SEC,
    ClusterProvider,
    EpisodeJob,
    _capacity,
    _Chain,
    _handle,
    _parse,
    _profiles,
    _Snapshot,
    _volume_specs,
    step_memory_mb,
)
from orbit.run.providers.docker import gvisor

pytestmark = pytest.mark.unit

#: Orbit's directory on the cluster by default, below the home.
CACHE = ".cache/orbit/slurm"


def provider(max_nodes=1):
    partitions = [
        dict(name="cpu", max_cpus=64, max_memory_mb=32768),
        dict(
            name="small",
            max_cpus=4,
            max_memory_mb=16384,
            max_gpus=2,
            gpu_model=dict(name="NVIDIA H100 80GB HBM3", memory_mb=81559),
            max_nodes=max_nodes,
        ),
        dict(
            name="large",
            max_cpus=16,
            max_memory_mb=32768,
            max_gpus=1,
            gpu_model=dict(name="NVIDIA B200", memory_mb=183359),
        ),
    ]
    config = RunConfig.model_validate(
        {"sandbox": {"providers": ["slurm"], "slurm": {"partitions": partitions}}}
    )
    return ProviderRegistry(config).get("slurm")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resources", "served"),
    [
        # A Sandbox leaves the job's Orbit a CPU of its own.
        (Resources(cpus=63), True),
        (Resources(cpus=64), False),
        (Resources(gpus=2, gpu_types=("H100",)), True),
        (Resources(gpus=1, gpu_memory_mb=100000), True),
        (Resources(gpus=2, gpu_memory_mb=100000), False),
        (Resources(cpus=8, gpus=1, gpu_types=("H100",)), False),
        (Resources(gpus=3), False),
        (Resources(storage_mb=1000), True),
        (Resources(container_features=("nested_containers", "raw_sockets")), False),
        (Resources(container_features=("nested_containers", "mount", "fuse")), True),
        (Resources(nodes=2), False),
    ],
)
async def test_selection(resources, served):
    needs = SandboxNeeds(
        kind="container", os="linux", arch="x86_64", network_mode="block", resources=resources
    )
    why = await provider().unserved(needs, user="user", role=SandboxRole.SOLVER)
    assert (not why) is served


def needs(resources, *, verifier=None, saves=0):
    return SandboxNeeds(
        kind="container",
        os="linux",
        arch="x86_64",
        network_mode="block",
        resources=resources,
        snapshot=bool(saves),
        saves=saves,
        verifier=None
        if verifier is None
        else snapshot_verifier_needs(
            ImageKind.CONTAINER, OperatingSystem.LINUX, verifier, NetworkMode.OPEN
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("solver", "verifier", "served"),
    [
        # One job holds the solver and, after it, the verifier booted from its snapshot:
        # each dimension is the larger of the two, beside Orbit's own CPU.
        (Resources(gpus=1, gpu_types=("H100",)), Resources(cpus=3, gpus=2), True),
        (Resources(gpus=1, gpu_types=("H100",)), Resources(cpus=8, gpus=2), False),
        # A solver without GPUs runs on a GPU partition when its verifier needs one.
        (Resources(cpus=2), Resources(gpus=1, gpu_types=("H100",)), True),
        (Resources(cpus=2), Resources(memory_mb=40000), False),
    ],
)
async def test_a_solver_and_its_snapshot_verifier_share_one_partition(solver, verifier, served):
    why = await provider().unserved(
        needs(solver, verifier=verifier, saves=1), user="user", role=SandboxRole.SOLVER
    )
    assert (not why) is served, why


@pytest.mark.asyncio
async def test_any_number_of_snapshots_is_saved_in_a_row_container_engine_or_not():
    """A snapshot whose image or volume would stack more than was measured is flattened."""
    slurm = provider()
    for feature in (None, "raw_sockets", "nested_containers"):
        resources = Resources(container_features=(feature,) if feature else ())
        saved = needs(resources, saves=10 * MAX_LAYERS)
        assert not await slurm.unserved(saved, user="user", role=SandboxRole.SOLVER)


def test_a_job_is_sized_for_every_verifier_it_may_hold_in_turn():
    """The snapshot verifier, a validation round's, and the separate one that views the
    solver's volumes: each dimension is the largest."""
    verifiers = verifiers_needs(
        ImageKind.CONTAINER,
        OperatingSystem.LINUX,
        (
            (Resources(cpus=3, memory_mb=1024), NetworkMode.OPEN),
            (Resources(cpus=1, memory_mb=6144), NetworkMode.OPEN),
        ),
    )
    solver = needs(Resources(cpus=2, memory_mb=2048)).model_copy(update={"verifier": verifiers})
    assert _capacity(solver) == (3, step_memory_mb(6144), 0, 1)
    assert _capacity(solver, Resources(cpus=1, memory_mb=1024, gpus=1, nodes=2)) == (
        3,
        step_memory_mb(6144),
        1,
        2,
    )


def test_a_sandbox_booted_from_a_snapshot_views_its_volumes_and_copies_its_engines():
    root = [1000, 1000, 0o755]
    stacks = {
        "/work": dict(layers=["b", "a"], root=root, copied=False),
        "/var/lib/docker": dict(layers=["d"], root=[0, 0, 0o711], copied=True),
    }
    engines = ("/var/lib/docker", "/var/lib/containerd")
    assert _volume_specs(stacks, engines, viewing=False) == [
        dict(path="/work", kind="view", layers=["b", "a"], root=root),
        dict(path="/var/lib/docker", kind="copy", layers=["d"], copied=True),
        dict(path="/var/lib/containerd", kind="fresh", copied=True),
    ]
    # A verifier of another image views the agent's volumes, never its engine's.
    assert _volume_specs(stacks, (), viewing=True) == [
        dict(path="/work", kind="view", layers=["b", "a"], root=root)
    ]


def test_the_largest_sandbox_a_profile_declares_fits_its_partition_beside_orbit():
    for part in provider().config.partitions:
        for profile in _profiles(part, provider().config):
            assert profile.max_memory_mb is not None
            held = JOB_OVERHEAD_MB + ENGINE_OVERHEAD_MB
            assert step_memory_mb(profile.max_memory_mb) + held <= part.max_memory_mb
            assert step_memory_mb(profile.max_memory_mb + 1) + held > part.max_memory_mb
            assert profile.max_cpus == part.max_cpus - ENGINE_CPUS


def test_an_episodes_job_holds_its_largest_sandbox_the_worker_and_orbit():
    slurm = provider()
    solver = needs(Resources(cpus=2, memory_mb=2048))
    assert slurm.episode_job(solver) == EpisodeJob(
        partition=slurm.config.partitions[0],
        cpus=2 + ENGINE_CPUS,
        memory_mb=step_memory_mb(2048) + JOB_OVERHEAD_MB + ENGINE_OVERHEAD_MB,
        gpus=0,
        nodes=1,
    )
    # A separate verifier of a GPU makes it a job of a GPU queue, sized for both.
    verified = slurm.episode_job(solver, Resources(cpus=3, memory_mb=1024, gpus=1))
    assert verified is not None and verified.partition.name == "small"
    assert (verified.cpus, verified.gpus) == (3 + ENGINE_CPUS, 1)
    assert slurm.episode_job(needs(Resources(cpus=64))) is None


@pytest.mark.asyncio
async def test_a_job_without_gpus_takes_a_partition_without_them_where_the_cluster_has_one():
    """A CPU-only episode's job goes to a partition without GPUs where the cluster has one,
    and to a GPU partition only on a cluster of those alone; a verifier is no job's own, and
    runs in its solver's whatever partition it would take alone."""
    full = provider()
    cpu_only = needs(Resources(cpus=2))
    placed = full.episode_job(cpu_only)
    assert placed is not None and placed.partition.name == "cpu"
    gpu_only = ClusterProvider(
        full.config.model_copy(update={"partitions": full.config.partitions[1:]})
    )
    assert gpu_only.refusals(cpu_only, user="user", role=SandboxRole.VERIFIER) == ()
    assert gpu_only.refusals(cpu_only, user="user", role=SandboxRole.SOLVER) == ()
    placed = gpu_only.episode_job(cpu_only)
    assert placed is not None and placed.partition.name == "small" and placed.gpus == 0


PREPARED, OTHER, SAVED = ("sha256:" + digit * 64 for digit in "123")


def current(nodes=1, *, cpus=8, memory_mb=65536, gpus=2, deadline=None, partition="small"):
    return CurrentAllocation(
        job_id="77",
        nodes=tuple(f"n{rank}" for rank in range(nodes)),
        cpus=cpus,
        memory_mb=memory_mb,
        gpus=gpus,
        deadline=deadline,
        partition=partition,
    )


def in_job(max_nodes=1, **job):
    """The Provider in an episode's job of the cluster ``provider`` configures."""
    return ClusterProvider(provider(max_nodes=max_nodes).config, current(max_nodes, **job))


def chain(cpus=4, memory_mb=8192, gpus=2, nodes=1, *, held_by=None):
    """The job's allocation, its worker running, for its Sandboxes' ``cpus``, ``memory_mb``
    and ``gpus`` on each of its ``nodes``; ``held_by`` a Provider in the job, which holds
    it."""
    job = slurm_module.SlurmAllocation(provider(max_nodes=nodes).config, current(nodes))
    job._worker = SimpleNamespace(returncode=None)
    held = _Chain(
        job=job,
        ready=JobReady(runtime="runsc", node=job.nodes[0], nodes=job.nodes),
        seeds=(SAVED,) * nodes,
        cpus=cpus,
        memory_mb=memory_mb,
        gpus=gpus,
    )
    if held_by is not None:
        held_by.chain = held
    return held


def test_the_job_starts_sandboxes_while_they_fit_beside_what_runs():
    held = chain(memory_mb=2 * step_memory_mb(2048))
    first = held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1))
    second = held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1))
    assert (first, second) == ("1", "2")
    assert held.reserve(Resources(cpus=1, memory_mb=512)) is None
    held.running.pop(first)
    assert held.reserve(Resources(cpus=2, memory_mb=2048, gpus=2)) is None
    assert held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1)) == "3"


def test_a_snapshot_handle_names_its_allocation_and_sandbox():
    held = chain()
    assert _parse(_handle(held.job, "3")) == (held.job.name, "3")
    for wrong in ("slurm:orbit-1", "slurm:x/1", "slurm:orbit-1/x", "docker:orbit-1/1"):
        with pytest.raises(ProviderCapabilityError):
            _parse(wrong)


def _image(identity=PREPARED, ref="images/solver.tar.gz"):
    return PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=PREPARED,
        runtime_ref=ref,
        resolved_reference="ref",
        prepared_identity=identity,
    )


def _saved(slurm, held, *, index="1", stacks=()):
    """A snapshot the job saved, which its stores hold."""
    snapshot = _Snapshot(
        index=index,
        images=(SAVED,),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        stacks=stacks,
    )
    handle = _handle(held.job, index)
    slurm.snapshots[handle] = snapshot
    return handle, snapshot


def _snapshot_request(handle, **update):
    update.setdefault("resources", Resources(cpus=2, memory_mb=4096, gpus=1))
    return SandboxRequest(
        episode_id="e",
        prepared_image=_image(),
        snapshot=handle,
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
        **update,
    )


@pytest.mark.asyncio
async def test_a_snapshot_boots_in_the_job_from_its_store_while_it_has_room(monkeypatch):
    slurm = in_job()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, held_by=slurm)
    view = [dict(path="/work", kind="view", layers=["x.1.0"], root=[0, 0, 0o755])]
    handle, _ = _saved(
        slurm,
        held,
        stacks=({"/work": dict(layers=["x.1.0"], root=[0, 0, 0o755], copied=False)},),
    )
    loaded = []

    async def load(chain, image):
        loaded.append((chain, image.prepared_identity))
        return image.prepared_identity

    monkeypatch.setattr(slurm, "_load", load)
    # Both images were prepared for the cluster.
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})
    booted, index, resolved, seed, specs = await slurm._boot(
        _snapshot_request(handle), (), MIN_LEFT_SEC
    )
    assert (booted, index, resolved.observed_identity, seed) == (held, "1", SAVED, [SAVED])
    assert specs == [view] and held.viewing["1"] == {"x.1.0"}
    # Beside what runs, nothing fits: a wait for room, never another allocation.
    with pytest.raises(ProviderCapacityError, match="other Sandboxes"):
        await slurm._boot(_snapshot_request(handle), (), MIN_LEFT_SEC)
    held.running.clear()
    # A verifier of the verifier's image views the volumes in the same job, which loads
    # that image.
    viewer = _snapshot_request(handle, resources=Resources(cpus=1, memory_mb=1024)).model_copy(
        update={"snapshot": None, "volumes_from": handle, "prepared_image": _image(OTHER)}
    )
    booted, index, resolved, seed, specs = await slurm._boot(viewer, (), MIN_LEFT_SEC)
    assert (booted, seed, specs) == (held, [OTHER], [view]) and loaded == [(held, OTHER)]
    held.running.clear()
    # One the job never holds is refused.
    with pytest.raises(ProviderCapabilityError, match="too few"):
        await slurm._boot(_snapshot_request(handle, resources=Resources(cpus=8)), (), 300)
    with pytest.raises(ProviderCapabilityError, match="prepared solver image"):
        await slurm._boot(
            _snapshot_request(handle).model_copy(update={"prepared_image": _image(OTHER)}),
            (),
            MIN_LEFT_SEC,
        )
    assert not held.running


@pytest.mark.asyncio
async def test_a_snapshot_the_job_would_not_last_is_written_out_and_handed_off(monkeypatch):
    """The only hand-off: the job's own snapshot, which the episode's next job boots from."""
    slurm = in_job()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, held_by=slurm)
    held.deadline = datetime.now(UTC) + timedelta(seconds=600)
    handle, snapshot = _saved(slurm, held)
    written = []

    async def write_out(chain, saved):
        written.append(saved)
        saved.archives = ("snapshots/out.0.tar",)
        return saved.archives

    monkeypatch.setattr(slurm, "write_out", write_out)
    booted, *_ = await slurm._boot(_snapshot_request(handle, duration_sec=500), (), 500)
    assert booted is held and not written
    held.running.clear()
    with pytest.raises(SandboxHandOff) as handed:
        await slurm._boot(_snapshot_request(handle, duration_sec=900), (), 900)
    assert handed.value.handle == handle and written == [snapshot]
    # It stays the job's, written out once, and nothing was reserved for it.
    assert slurm.snapshots[handle] is snapshot and not held.running and not held.closed
    with pytest.raises(SandboxHandOff):
        await slurm._boot(_snapshot_request(handle, duration_sec=900), (), 900)
    assert written == [snapshot]
    # A verifier viewing its volumes hands nothing off: its time is the episode's to keep.
    viewer = _snapshot_request(handle).model_copy(update={"snapshot": None, "volumes_from": handle})
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})
    with pytest.raises(ProviderCapabilityError, match="deadline leaves"):
        await slurm._boot(viewer, (), 900)


@pytest.mark.asyncio
async def test_a_fresh_sandbox_the_job_would_not_last_is_refused(monkeypatch):
    slurm = in_job()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, held_by=slurm)
    held.deadline = datetime.now(UTC) + timedelta(seconds=60)
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})
    request = _snapshot_request(None)
    with pytest.raises(ProviderCapabilityError, match="deadline leaves"):
        await slurm.create(request)
    assert not held.running


@pytest.mark.asyncio
async def test_the_job_holds_for_its_sandboxes_what_its_worker_and_orbit_leave(monkeypatch):
    """Each node seeded, those past the first Sandbox's with its image alone, by the gVisor
    build this engine runs, until the job's own deadline."""
    deadline = datetime.now(UTC) + timedelta(hours=1)
    slurm = in_job(max_nodes=2, cpus=8, memory_mb=65536, gpus=2, deadline=deadline)
    staged = []

    async def open_(job, spec):
        staged.append(spec)
        return JobReady(runtime="runsc", node="n0", nodes=("n0", "n1"))

    monkeypatch.setattr(Allocation, "open", open_)
    seed = dict(archive="snapshots/s.0.tar", image="a" * 64, layers={"L": "snapshots/s.0.L.tar"})
    held = await slurm._open([seed])
    assert (held.cpus, held.memory_mb, held.gpus) == (
        8 - ENGINE_CPUS,
        65536 - JOB_OVERHEAD_MB - ENGINE_OVERHEAD_MB,
        2,
    )
    assert held.deadline == deadline and held.seeds == (A, A) and held.job.id == "77"
    (spec,) = staged
    assert spec["seeds"] == [seed, {**seed, "layers": {}}] and spec["gpus"] == 2
    assert (spec["version"], spec["build"]) == (gvisor.VERSION, gvisor.BUILD_ID)
    closed = []

    async def wrong(job, spec):
        return JobReady(runtime="runsc", node="n0", nodes=("n0", "n9"))

    async def close(job):
        closed.append(job)

    monkeypatch.setattr(Allocation, "open", wrong)
    monkeypatch.setattr(Allocation, "close", close)
    with pytest.raises(ProviderStartError, match="found the nodes"):
        await slurm._open([seed])
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_the_login_node_starts_no_sandbox():
    with pytest.raises(ProviderCapabilityError, match="episode jobs"):
        await provider().create(_snapshot_request(None))


@pytest.mark.asyncio
async def test_every_sandbox_of_the_episode_shares_the_one_job_its_first_opened(monkeypatch):
    """The first Sandbox opens the job's allocation with its image; a later one of another
    image loads that image into it."""
    slurm = in_job()
    opened, ran, started = [], [], []

    async def open_(seeds):
        opened.append(list(seeds))
        return chain(cpus=4, memory_mb=2 * step_memory_mb(4096), gpus=2)

    async def control(job, *args, nodes, timeout=60):
        ran.append((args, tuple(nodes)))

    async def start(chain, index, request, resolved, loaded, specs, **_):
        started.append((chain, index, loaded))
        return SimpleNamespace()

    monkeypatch.setattr(slurm, "_open", open_)
    monkeypatch.setattr(slurm, "_start", start)
    monkeypatch.setattr(Allocation, "control", control)
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})
    fresh = _snapshot_request(None, resources=Resources(cpus=2, memory_mb=4096, gpus=1))
    await slurm.create(fresh.model_copy(update={"prepared_image": _image(SAVED)}))
    await slurm.create(fresh.model_copy(update={"prepared_image": _image(OTHER, "images/v")}))
    assert len(opened) == 1
    assert opened[0] == [dict(archive="images/solver.tar.gz", image="3" * 64, layers={})]
    (first, one, loaded_first), (second, two, loaded_second) = started
    assert first is second is slurm.chain and (one, two) == ("1", "2")
    assert (loaded_first, loaded_second) == ([SAVED], [OTHER])
    assert ran == [(("load", "images/v", "2" * 64), ("n0",))]


def _written_out(tmp_path, name="orbit-old", index="2", **record):
    files = tmp_path / CACHE / "snapshots"
    files.mkdir(parents=True, exist_ok=True)
    record = (
        dict(
            handle=f"slurm:{name}/{index}",
            images=[A],
            prepared_identity=PREPARED,
            episode="e",
            roles=["solver"],
            reason="moved",
            stacks=[{"/work": dict(layers=["o.2.0"], root=[0, 0, 0o755], copied=False)}],
            written=[5],
            shared=None,
        )
        | record
    )
    (files / f"{name}-{index}.json").write_text(json.dumps(record))
    return record["handle"]


@pytest.mark.asyncio
async def test_a_new_job_boots_from_a_snapshot_an_earlier_one_wrote_out(tmp_path, monkeypatch):
    """The episode's next job, a Provider of its own: its first Sandbox opens the job with
    the snapshot's image and volume layers, which its stores then hold; once the job runs,
    one is loaded into it, image and layers alike."""
    monkeypatch.setenv("HOME", str(tmp_path))
    handle = _written_out(tmp_path)
    slurm = in_job()
    opened = []

    async def open_(seeds):
        opened.append(list(seeds))
        return chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)

    monkeypatch.setattr(slurm, "_open", open_)
    booted, _, resolved, loaded, specs = await slurm._boot(
        _snapshot_request(handle), (), MIN_LEFT_SEC
    )
    layers = {"o.2.0": "snapshots/orbit-old-2.0.o.2.0.tar"}
    assert opened == [[dict(archive="snapshots/orbit-old-2.0.tar", image="a" * 64, layers=layers)]]
    assert booted is slurm.chain and loaded == [A] and resolved.observed_identity == A
    assert specs[0][0]["layers"] == ["o.2.0"] and slurm.snapshots[handle].archives
    assert slurm._snapshot_state(handle) == (None, (5,))
    # Again: from the job's stores, with nothing loaded.
    booted.running.clear()
    await slurm._boot(_snapshot_request(handle), (), MIN_LEFT_SEC)
    assert len(opened) == 1

    # Into a job a Sandbox of the prepared image opened.
    other = in_job()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, held_by=other)
    ran = []

    async def control(job, *args, nodes, timeout=60):
        ran.append((args, tuple(nodes)))

    monkeypatch.setattr(Allocation, "control", control)
    booted, *_ = await other._boot(_snapshot_request(handle), (), MIN_LEFT_SEC)
    assert booted is held and handle in other.snapshots
    assert ran == [
        (
            ("load", "snapshots/orbit-old-2.0.tar", "a" * 64, "o.2.0", layers["o.2.0"]),
            ("n0",),
        )
    ]
    # Written out by an earlier job, it would not last a later one either: no hand-off.
    held.running.clear()
    held.deadline = datetime.now(UTC) + timedelta(seconds=60)
    with pytest.raises(ProviderCapabilityError, match="deadline leaves"):
        await other._boot(_snapshot_request(handle), (), MIN_LEFT_SEC)


def _held_from_record(slurm, handle):
    name, index = _parse(handle)
    slurm.snapshots[handle] = slurm._written_out(handle, name, index)


@pytest.mark.asyncio
async def test_a_snapshot_is_destroyed_with_its_store_layers_and_files(tmp_path, monkeypatch):
    """Its image untagged and its layers dropped from the job's stores, where nothing else
    needs them, and its files below the cache removed; a login node removes the files."""
    monkeypatch.setenv("HOME", str(tmp_path))
    slurm = in_job()
    held = chain(held_by=slurm)
    stack = {"/work": dict(layers=["x.1.0", "base"], root=[0, 0, 0o755], copied=False)}
    handle, _ = _saved(slurm, held, stacks=(stack,))
    _saved(
        slurm,
        held,
        index="2",
        stacks=({"/w": dict(layers=["base"], root=[0, 0, 0], copied=False)},),
    )
    files = tmp_path / CACHE / "snapshots"
    files.mkdir(parents=True)
    for name in ("0.tar", "json", "0.x.1.0.tar"):
        (files / f"{held.job.name}-1.{name}").write_text("")
    (files / f"{held.job.name}-10.json").write_text("{}")
    ran = []

    async def control(job, *args, nodes, timeout=60):
        ran.append((args, tuple(nodes)))

    monkeypatch.setattr(Allocation, "control", control)
    await slurm.destroy_retained(handle)
    assert ran == [(("rmi", "1"), ("n0",)), (("drop", "x.1.0"), ("n0",))]
    assert [path.name for path in files.iterdir()] == [f"{held.job.name}-10.json"]
    assert handle not in slurm.snapshots
    # One an earlier job wrote out is no tag of this job's.
    imported = _written_out(tmp_path)
    _held_from_record(slurm, imported)
    ran.clear()
    await slurm.destroy_retained(imported)
    assert ran == [(("drop", "o.2.0"), ("n0",))]
    assert not list(files.glob("orbit-old-2.*"))
    # From the login node, the files alone.
    login = provider()
    leftover = _written_out(tmp_path, index="3")
    await login.destroy_retained(leftover)
    assert not list(files.glob("orbit-old-3.*"))


@pytest.mark.asyncio
async def test_a_sandbox_whose_guest_went_is_explained_by_the_oom_kill_it_counted(tmp_path):
    from orbit.core.errors import GuestUnreachableError, MemoryExhaustedError

    class Watcher:
        async def settled(self, seconds):
            return None

        def oom_killed(self):
            return "1 OOM kill in the Sentry"

    held = chain()
    transport = job_module.JobTransport(
        [],
        job_module.SandboxSteps(held.job, "1", "n0"),
        job_module.StepReady(image="i", devices=(), step="0", node="n0"),
        [Watcher()],
    )
    explained = await transport.explain(GuestUnreachableError("guest went away"))
    assert isinstance(explained, MemoryExhaustedError) and "OOM kill" in str(explained)


@pytest.mark.asyncio
async def test_a_job_whose_worker_ended_starts_nothing_more(monkeypatch):
    """Its stores went with it, and every snapshot they held."""
    slurm = in_job()
    held = chain(held_by=slurm)
    held.job._worker = SimpleNamespace(returncode=1)
    with pytest.raises(ProviderStartError, match="worker ended"):
        await slurm.create(_snapshot_request(_saved(slurm, held)[0]))


def test_a_job_removes_only_the_stores_of_jobs_that_ended(tmp_path, monkeypatch):
    running = {"7"}
    for name, job in (("ours", "5"), ("running", "7"), ("ended", "9"), ("unmarked", None)):
        store = tmp_path / f"{worker.STORE_PREFIX}{name}"
        store.mkdir()
        if job is not None:
            (store / worker.OWNER).write_text(job)
    (tmp_path / "unrelated").mkdir()

    def squeue(argv, **_):
        listed = "RUNNING" if argv[3] in running else ""
        return subprocess.CompletedProcess(argv, 0 if listed else 1, stdout=listed, stderr="")

    monkeypatch.setenv("SLURM_JOB_ID", "5")
    monkeypatch.setattr(worker.subprocess, "run", squeue)
    assert worker.stale_stores(tmp_path) == [tmp_path / f"{worker.STORE_PREFIX}ended"]


@pytest.mark.parametrize(
    ("proxies", "flags"),
    [
        ({}, []),
        ({"https_proxy": "http://proxy.site:3128", "NO_PROXY": "localhost,10.0.0.0/8"}, []),
        ({"https_proxy": "proxy.site:3128", "HTTP_PROXY": "http://me:secret@proxy.site:3128"},
         ["--http-proxy=false"]),
        ({"ftp_proxy": "me@proxy.site:21"}, ["--http-proxy=false"]),
    ],
)  # fmt: skip
def test_builds_get_the_nodes_proxy_only_without_credentials(monkeypatch, capsys, proxies, flags):
    for name in worker.PROXIES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in proxies.items():
        monkeypatch.setenv(name, value)
    assert worker.build_proxy() == flags
    notice = capsys.readouterr().err
    assert ("credentials" in notice) == bool(flags) and "secret" not in notice


def test_both_builds_of_a_preparation_keep_a_proxy_with_credentials_out(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.chdir(job)
    framework = {"body": [], "tail": [], "python": "/python", "context": "/context"}
    (job / "recipe.json").write_text(json.dumps({"key": "k", "framework": framework}))
    monkeypatch.setenv("SLURM_JOB_ID", "5")
    monkeypatch.setenv("https_proxy", "http://me:secret@proxy.site:3128")
    monkeypatch.setattr(worker, "store", lambda recipe: (str(tmp_path), ["podman"], tmp_path))
    monkeypatch.setattr(worker, "hold_namespace", lambda *_: None)
    monkeypatch.setattr(worker, "clean", lambda *_: None)
    monkeypatch.setattr(worker, "output", lambda *_: "")
    builds = []

    def run(argv, **_):
        if argv[1] == "build":
            builds.append(argv)
            Path(argv[argv.index("--iidfile") + 1]).write_text("sha256:" + "0" * 64)
        return subprocess.CompletedProcess(argv, 0)

    def save(argv, **_):
        raise RuntimeError("saved")

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.subprocess, "Popen", save)
    with pytest.raises(RuntimeError, match="saved"):
        worker.prepare()
    assert [argv[2] for argv in builds] == ["--http-proxy=false", "--http-proxy=false"]


def test_a_volume_candidate_is_a_directory_with_no_link_on_its_way(tmp_path):
    (tmp_path / "home/user/work").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "link").symlink_to("data")
    (tmp_path / "file").write_text("")
    paths = ["/home/user/work", "/link", "/link/x", "/file", "/missing", "/data"]
    assert worker.directories_in(str(tmp_path), paths) == ["/home/user/work", "/data"]


def test_a_name_without_a_registry_is_docker_hubs():
    assert qualified("python:3.12") == "docker.io/library/python:3.12"
    assert qualified("owner/name") == "docker.io/owner/name"
    for named in ("ghcr.io/a/b:1", "localhost/a", "host:5000/a"):
        assert qualified(named) == named
    assert api(Reference.parse("docker.io/library/python")).registry == "registry-1.docker.io"
    assert api(Reference.parse("ghcr.io/a/b")).registry == "ghcr.io"


@pytest.mark.asyncio
async def test_a_reference_pinned_by_digest_needs_no_registry():
    digest = "sha256:" + "a" * 64
    assert await images.pin(f"python:3.12@{digest}") == f"docker.io/library/python@{digest}"


@pytest.mark.asyncio
async def test_an_image_is_prepared_on_the_cluster_once_per_key(tmp_path, monkeypatch):
    """The cache keeps what a preparation job saved; one whose archive is gone is prepared
    again."""
    monkeypatch.setenv("HOME", str(tmp_path))
    prepared = []

    async def run_job(config, recipe):
        prepared.append(recipe.key)
        (tmp_path / CACHE / images.archive(recipe.key)).write_bytes(b"image")
        return {"image": OTHER, "base": PREPARED, "repo_digests": []}

    monkeypatch.setattr(images, "_prepare", run_job)
    config = provider().config
    recipe = images.Recipe(
        key=PREPARED, source="external-ref", reference="ghcr.io/a/b@sha256:" + "4" * 64
    )
    first, reused = await images.prepare(config, recipe)
    assert not reused and first.prepared_identity == OTHER
    assert first.runtime_ref == images.archive(PREPARED) and images.recorded(
        provider().config, first
    )
    again, reused = await images.prepare(config, recipe)
    assert reused and again == first and prepared == [PREPARED]
    (tmp_path / CACHE / first.runtime_ref).unlink()
    assert not images.recorded(provider().config, first)
    _, reused = await images.prepare(config, recipe)
    assert not reused and prepared == [PREPARED, PREPARED]
    # Images are prepared where no GPU is held.
    assert images.build_partition(config).name == "cpu"


def test_the_job_takes_a_sandbox_only_while_it_lasts_long_enough():
    held = chain()
    held.deadline = datetime.now(UTC) + timedelta(seconds=600)
    assert held.lasts(300) and not held.lasts(900)
    held.deadline = None
    assert held.lasts(10**9)


def _slurm_job(monkeypatch, **variables):
    for name in ("SLURM_JOB_END_TIME", "SLURM_JOB_ID", "SLURM_JOB_PARTITION", "SLURMD_NODENAME"):
        monkeypatch.delenv(name, raising=False)
    defaults = dict(
        SLURM_JOB_ID="77",
        SLURM_JOB_PARTITION="small",
        SLURM_JOB_NODELIST="n[0-1]",
        SLURMD_NODENAME="n0",
    )
    for name, value in (defaults | variables).items():
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(slurm_module, "_hostnames", lambda nodelist: ("n0", "n1"))


def test_the_jobs_deadline_is_its_end_less_its_drain_with_no_scheduler_asked(monkeypatch):
    """Slurm's own end where it states one, else the wall time from the job's start."""
    config = provider(max_nodes=2).config
    started = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    end = started + timedelta(minutes=50)
    _slurm_job(monkeypatch, SLURM_JOB_END_TIME=str(int(end.timestamp())))
    job = dict(cpus=3, memory_mb=4096, gpus=1, walltime_minutes=60, started_at=started)
    found = managers.current_allocation(config, **job)
    drain = timedelta(minutes=config.drain_minutes)
    assert found == CurrentAllocation(
        job_id="77",
        nodes=("n0", "n1"),
        cpus=3,
        memory_mb=4096,
        gpus=1,
        deadline=end - drain,
        partition="small",
    )
    _slurm_job(monkeypatch)
    assert managers.current_allocation(config, **job).deadline == (
        started + timedelta(minutes=60) - drain
    )
    _slurm_job(monkeypatch, SLURMD_NODENAME="n1")
    with pytest.raises(ProviderStartError, match="not on the first node"):
        managers.current_allocation(config, **job)
    _slurm_job(monkeypatch, SLURM_JOB_PARTITION="other")
    with pytest.raises(ProviderStartError, match="the configuration lacks"):
        managers.current_allocation(config, **job)
    _slurm_job(monkeypatch, SLURM_JOB_ID=None)
    with pytest.raises(ProviderStartError, match="no SLURM_JOB_ID"):
        managers.current_allocation(config, **job)


def test_a_pbs_job_holds_what_its_queue_grants(monkeypatch):
    from orbit.core.config import PBSProviderConfig

    queue = dict(name="whole", max_cpus=16, max_memory_mb=65536, max_gpus=4)
    queue |= dict(gpu_model={"name": "GB200"}, chunk_resources=["ngpus"])
    config = PBSProviderConfig(shared_fs_dir="/shared", partitions=(queue,))
    monkeypatch.setenv("PBS_JOBID", "5.server")
    monkeypatch.setenv("PBS_QUEUE", "whole")
    monkeypatch.setenv("SLURM_JOB_END_TIME", "1")  # no PBS job's
    started = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    found = managers.current_allocation(
        config, cpus=2, memory_mb=4096, gpus=1, walltime_minutes=30, started_at=started
    )
    assert (found.job_id, found.cpus, found.memory_mb, found.gpus) == (
        "5.server",
        16,
        65536,
        1,
    )
    assert found.nodes == (job_module.socket.gethostname(),)
    assert found.deadline == started + timedelta(minutes=30 - config.drain_minutes)


def test_the_worker_runs_on_orbits_node_by_command_and_on_others_by_step(monkeypatch, tmp_path):
    """Without the Host's credentials either way, and in the job's directory, which neither
    Orbit's process nor the caller's command changes to."""
    monkeypatch.setenv("ORBIT_TEST_API_KEY", "secret-value")
    unset = [option for name in sorted(job_module.host_credentials()) for option in ("-u", name)]
    assert "ORBIT_TEST_API_KEY" in unset[1::2]
    config = SlurmProviderConfig(
        partitions=(SlurmPartition(name="p", max_cpus=8, max_memory_mb=8192, max_nodes=2),),
        shared_fs_dir=str(tmp_path),
    )
    job = slurm_module.SlurmAllocation(config, current(2, partition="p"))
    here = job.step("exec", "1", "true", node="n0")
    assert here == [
        "env", "-C", str(job.directory), *unset, sys.executable, "-I", "worker.py",
        "exec", "1", "true",
    ]  # fmt: skip
    there = job.step("exec", "1", "true", node="n1")
    assert there[: 1 + len(unset)] == ["env", *unset]
    assert there[1 + len(unset) :] == [
        "srun", "--unbuffered", "--overlap", "--nodes=1", "--ntasks=1", "--nodelist=n1",
        "--jobid=77", f"--chdir={job.directory}", "python3", "-I", "worker.py",
        "exec", "1", "true",
    ]  # fmt: skip
    assert "secret-value" not in " ".join(here + there)


@pytest.mark.asyncio
async def test_a_command_on_several_nodes_runs_on_each_its_own_way(monkeypatch, tmp_path):
    ran = []

    async def run(*argv, timeout, **_):
        ran.append(argv)
        return (1, "", "no such container") if "srun" in argv else (0, "", "")

    monkeypatch.setattr(job_module, "run", run)
    config = SlurmProviderConfig(
        partitions=(SlurmPartition(name="p", max_cpus=8, max_memory_mb=8192, max_nodes=2),),
        shared_fs_dir=str(tmp_path),
    )
    job = slurm_module.SlurmAllocation(config, current(2, partition="p"))
    with pytest.raises(ProviderStartError, match="could not block on n1: no such container"):
        await job.control("block", "1", nodes=("n0", "n1"))
    assert [argv[argv.index("worker.py") + 1 :] for argv in ran] == [("block", "1")] * 2
    assert sum("srun" in argv for argv in ran) == 1


#: A worker that records what it was given, says it is ready and ends once stopped.
_WORKER = """
import json, os, sys, time
from pathlib import Path
Path("given.json").write_text(json.dumps(dict(argv=sys.argv, env=dict(os.environ))))
Path("ready").write_text(json.dumps(dict(runtime="runsc", node="n0", nodes=["n0"])))
while not Path("stop").read_text().strip():
    time.sleep(0.05)
if os.environ.get("ORBIT_TEST_CLEANUP_FAILS"):
    Path("cleanup-error").write_text("store busy")
"""


@pytest.mark.asyncio
async def test_the_jobs_worker_is_orbits_child_until_orbit_closes_the_job(monkeypatch, tmp_path):
    """It gets none of the Host's credentials; closing stops it and removes the directory,
    and only that: the job goes on."""
    monkeypatch.setenv("ORBIT_TEST_API_KEY", "secret-value")
    config = SlurmProviderConfig(
        partitions=(SlurmPartition(name="p", max_cpus=8, max_memory_mb=8192),),
        shared_fs_dir=str(tmp_path),
    )
    stopped = []

    async def scheduler(*argv, **_):
        stopped.append(argv)

    monkeypatch.setattr(slurm_module, "slurm", scheduler)

    def stage(self, spec):
        job_module.stage(self.config, self.directory, spec, name="allocation.json")
        (self.directory / "worker.py").write_text(_WORKER)

    monkeypatch.setattr(Allocation, "stage", stage)
    job = slurm_module.SlurmAllocation(config, current(partition="p"))
    ready = await job.open({"seeds": []})
    assert ready.nodes == ("n0",) and job.running
    given = json.loads((job.directory / "given.json").read_text())
    assert given["argv"][1:] == ["supervise"]
    assert "ORBIT_TEST_API_KEY" not in given["env"]
    assert json.loads((job.directory / "allocation.json").read_text())["seeds"] == []
    await job.close()
    assert not job.running and not job.directory.exists() and not stopped
    await job.close()  # once is enough
    # Cleanup that fails keeps the directory, for what it says.
    monkeypatch.setenv("ORBIT_TEST_CLEANUP_FAILS", "1")
    failing = slurm_module.SlurmAllocation(config, current(partition="p"))
    await failing.open({"seeds": []})
    with pytest.raises(ProviderStartError, match="store busy"):
        await failing.close()
    assert failing.directory.exists()
    # A job never opened closes as well.
    await slurm_module.SlurmAllocation(config, current(partition="p")).close()


@pytest.mark.asyncio
async def test_closing_the_job_keeps_only_the_snapshots_written_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    slurm = in_job()
    await slurm.close()  # nothing opened yet
    held = chain(held_by=slurm)
    closed = []

    async def close(job):
        closed.append(job)

    monkeypatch.setattr(Allocation, "close", close)
    shared = tmp_path / CACHE / "shared"
    for name in ("kept", "lost"):
        (shared / name).mkdir(parents=True)
    kept_handle, kept = _saved(slurm, held)
    kept.archives, kept.shared = ("snapshots/k.0.tar",), "shared/kept"
    (tmp_path / CACHE / "snapshots").mkdir()
    record = tmp_path / CACHE / "snapshots" / f"{held.job.name}-1.json"
    record.write_text(json.dumps(dict(handle=kept_handle, shared="shared/kept")))
    _, lost = _saved(slurm, held, index="2")
    lost.shared = "shared/lost"
    await slurm.close()
    assert closed == [held.job] and held.closed and slurm.chain is None and not slurm.snapshots
    assert record.exists() and (shared / "kept").exists() and not (shared / "lost").exists()
    await slurm.close()
    assert closed == [held.job]


@pytest.mark.asyncio
async def test_a_verifier_needed_longer_than_an_allocation_lasts_is_refused_at_selection():
    """Before anything is prepared or run: a job holds a Sandbox for its wall time less its
    drain."""
    slurm = provider()
    usable = (slurm.config.walltime_minutes - slurm.config.drain_minutes) * 60
    fits = needs(Resources(cpus=2)).model_copy(update={"duration_sec": usable})
    assert not await slurm.unserved(fits, user="user", role=SandboxRole.SOLVER)
    longer = fits.model_copy(update={"duration_sec": usable + 1})
    why = await slurm.unserved(longer, user="user", role=SandboxRole.SOLVER)
    assert why and "a verifier may take" in " ".join(why)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resources", "served"),
    [
        (Resources(cpus=3, gpus=2, nodes=2), True),
        (Resources(cpus=3, gpus=2, nodes=3), False),
        # Without GPUs it takes a partition without them, which here spans one node.
        (Resources(cpus=2, nodes=2), False),
    ],
)
async def test_a_sandbox_spans_as_many_nodes_as_its_partition_allows(resources, served):
    needs = SandboxNeeds(
        kind="container", os="linux", arch="x86_64", network_mode="block", resources=resources
    )
    why = await provider(max_nodes=2).unserved(needs, user="user", role=SandboxRole.SOLVER)
    assert (not why) is served, why


def test_a_job_of_several_nodes_holds_each_sandbox_on_its_first_nodes():
    held = chain(cpus=4, memory_mb=step_memory_mb(4096), gpus=2, nodes=2)
    assert held.reserve(Resources(cpus=1, memory_mb=512, nodes=3)) is None
    group = held.reserve(Resources(cpus=4, memory_mb=4096, gpus=2, nodes=2))
    assert group == "1"
    # Node 0 is full, so nothing more starts until the group ends.
    assert held.reserve(Resources(cpus=1, memory_mb=512)) is None
    held.running.pop(group)
    single = held.reserve(Resources(cpus=4, memory_mb=4096, gpus=2))
    # A single-node Sandbox holds node 0 alone: a two-node one does not fit beside it.
    assert held.reserve(Resources(cpus=1, memory_mb=512, nodes=2)) is None
    held.running.pop(single)
    assert held.reserve(Resources(cpus=1, memory_mb=512, nodes=2)) == "3"


A, B = ("sha256:" + digit * 64 for digit in "ab")


def _group_snapshot(slurm, held):
    stack = {"/work": dict(layers=["x.1.0.0"], root=[0, 0, 0o755], copied=False)}
    other = {"/work": dict(layers=["x.1.1.0"], root=[0, 0, 0o755], copied=False)}
    snapshot = _Snapshot(
        index="1",
        images=(A, B),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        stacks=(stack, other),
    )
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = snapshot
    return handle, snapshot


@pytest.mark.asyncio
async def test_each_node_of_a_sandbox_boots_from_the_same_node_of_its_snapshot(monkeypatch):
    slurm = in_job(max_nodes=2)
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, nodes=2, held_by=slurm)
    handle, _ = _group_snapshot(slurm, held)
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})

    async def load(chain, image):
        return image.prepared_identity

    monkeypatch.setattr(slurm, "_load", load)
    group = _snapshot_request(handle, resources=Resources(cpus=2, memory_mb=4096, nodes=2))
    booted, index, resolved, loaded, specs = await slurm._boot(group, (), MIN_LEFT_SEC)
    assert (booted, loaded, resolved.observed_identity) == (held, [A, B], A)
    assert [spec[0]["layers"] for spec in specs] == [["x.1.0.0"], ["x.1.1.0"]]
    assert held.viewing[index] == {"x.1.0.0", "x.1.1.0"}
    held.running.clear()
    # A snapshot verifier of one node boots from node 0 alone.
    _, _, _, loaded, specs = await slurm._boot(
        _snapshot_request(handle, resources=Resources(cpus=2, memory_mb=4096)), (), MIN_LEFT_SEC
    )
    assert loaded == [A] and len(specs) == 1
    held.running.clear()
    # It never spans nodes the snapshot does not keep.
    with pytest.raises(ProviderCapabilityError, match="keeps 2 node"):
        await slurm._boot(
            _snapshot_request(handle, resources=Resources(cpus=1, memory_mb=512, nodes=3)),
            (),
            MIN_LEFT_SEC,
        )
    # A separate verifier views node k's volumes on its node k.
    viewer = _snapshot_request(
        handle, resources=Resources(cpus=2, memory_mb=4096, nodes=2)
    ).model_copy(update={"snapshot": None, "volumes_from": handle})
    _, _, _, loaded, specs = await slurm._boot(viewer, (), MIN_LEFT_SEC)
    assert loaded == [PREPARED, PREPARED]
    assert [spec[0]["layers"] for spec in specs] == [["x.1.0.0"], ["x.1.1.0"]]


@pytest.mark.asyncio
async def test_each_node_writes_its_own_part_of_a_snapshot_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / CACHE / "snapshots").mkdir(parents=True)
    slurm = in_job(max_nodes=2)
    held = chain(nodes=2, held_by=slurm)
    _, snapshot = _group_snapshot(slurm, held)
    ran = []

    async def control(job, *args, nodes, timeout=60):
        ran.append((args, tuple(nodes)))

    monkeypatch.setattr(Allocation, "control", control)
    archives = await slurm.write_out(held, snapshot)
    name = held.job.name
    assert archives == (f"snapshots/{name}-1.0.tar", f"snapshots/{name}-1.1.tar")
    assert sorted(ran) == [
        (("export", "a" * 64, archives[0], "x.1.0.0"), ("n0",)),
        (("export", "b" * 64, archives[1], "x.1.1.0"), ("n1",)),
    ]
    record = (tmp_path / CACHE / "snapshots" / f"{name}-1.json").read_text()
    assert '"images": ["' + A in record and snapshot.archives == archives


def test_a_sandbox_step_spans_the_first_nodes_of_the_allocation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / worker.SANDBOXES / "1").mkdir(parents=True)
    started = []
    monkeypatch.setattr(
        worker.subprocess, "Popen", lambda argv, **_: started.append(argv) or SimpleNamespace()
    )
    request = dict(nodes=[{}, {}], cpus=4, memory_mb=1024, gpus=8)
    worker.launch("1", request, ["n0", "n1", "n2"], gpus=True)
    argv = started[0]
    assert "--nodelist=n0,n1" in argv and "--ntasks=2" in argv and "--ntasks-per-node=1" in argv
    assert "--kill-on-bad-exit=1" in argv and "--gres=gpu:8" in argv


def test_a_save_stops_the_container_again_where_podman_lost_a_session_s_end(monkeypatch, tmp_path):
    """A guest session whose srun on the Host was killed leaves Podman one it never saw end:
    the first stop fails on it, and a second finishes it; one that fails again fails the
    save."""
    codes: list[int] = []
    ran = []

    def run(argv, *, check=False, stdout=None):
        ran.append(argv[1])
        code = codes.pop(0)
        if check and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker, "output", lambda *argv: "sha256:abc")
    monkeypatch.setattr(worker, "layers_of", lambda command, image: 1)
    own = tmp_path / "1" / "nodes" / "0"
    own.mkdir(parents=True)
    codes[:] = [125, 0]
    saved = worker.save(["podman"], str(tmp_path), "sandbox-1", own, [], lambda: 0, max_layers=200)
    assert saved == set()
    assert ran == ["stop", "stop"]
    assert json.loads((own / "saved").read_text())["image"] == "abc"
    codes[:] = [125, 125]
    with pytest.raises(subprocess.CalledProcessError):
        worker.save(["podman"], str(tmp_path), "sandbox-1", own, [], lambda: 0, max_layers=200)


def test_a_router_derives_its_wireguard_public_key_as_x25519_does():
    """RFC 7748, section 6.1."""
    private = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    public = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert worker.public_key(private).hex() == public


def test_a_router_forwards_only_between_its_sandbox_and_the_other_nodes(monkeypatch):
    """Policy routing is the seal between the nodes: from the Sandbox only to the group's
    addresses through the tunnel, from the tunnel only to the Sandbox, nothing to the
    uplink; the private key reaches netlink by a pipe alone."""
    ran = []

    def run(argv, *, input=None, check=False):
        ran.append((list(argv), input))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    private = "11" * 32
    worker.group(dict(router=10, sandbox=20, rank=1, port=4000, private=private, peers=[]))
    router = ["nsenter", "-t", "10", "-n"]
    commands = [" ".join(argv[len(router) :]) for argv, _ in ran if argv[:4] == router]
    assert "ip route add 10.214.0.0/24 dev wg0 table 100" in commands
    assert "ip route add 10.214.0.2 dev peers-router table 101" in commands
    assert commands.count("ip route add unreachable default table 100") == 1
    assert commands.count("ip route add unreachable default table 101") == 1
    assert "ip rule add iif peers-router lookup 100 pref 100" in commands
    assert "ip rule add iif wg0 lookup 101 pref 101" in commands
    assert not any("default via" in command for command in commands)
    sandbox = [" ".join(argv[4:]) for argv, _ in ran if argv[:4] == ["nsenter", "-t", "20", "-n"]]
    assert "ip address add 10.214.0.2/24 dev peers" in sandbox
    assert all(private not in " ".join(argv) for argv, _ in ran)
    assert any(given and private.encode() in given for _, given in ran)


def test_each_node_gets_the_nodes_variables_and_nccl_keeps_to_infiniband_only_on_all():
    from orbit.run.providers.cluster.job import StepReady
    from orbit.run.providers.cluster.provider import _group_variables

    def ready(node, hcas):
        return StepReady(image="i", devices=(), step="0", node=node, hcas=hcas)

    both = _group_variables([ready("a", ("/dev/infiniband/uverbs1",)), ready("b", ("x",))])
    assert both[1] == {
        "ORBIT_NODES": "node0,node1",
        "ORBIT_NODE_RANK": "1",
        "NCCL_SOCKET_IFNAME": "peers",
        "GLOO_SOCKET_IFNAME": "peers",
        "NCCL_NET": "IB",
    }
    assert "NCCL_NET" not in _group_variables([ready("a", ("x",)), ready("b", ())])[0]
    assert _group_variables([ready("a", ("x",))]) == []


@pytest.mark.asyncio
async def test_a_command_gets_the_nodes_variables_below_the_proxys_and_its_own():
    from orbit.run.providers.tunnel import TunneledSandbox

    sent = []

    class Client:
        async def exec(self, argv, **options):
            sent.append(options["env"])
            return 0, "", "", False

    fake = SimpleNamespace(
        _variables={"ORBIT_NODE_RANK": "0", "NCCL_NET": "IB", "HTTP_PROXY": "never"},
        _sealed=True,
        _relay="127.0.0.1:9",
        _peers=("node0", "node1"),
        _client=Client(),
        _as=lambda identity: None,
    )
    await TunneledSandbox.exec(fake, ["true"], env={"NCCL_NET": "Socket"})
    env = sent[0]
    assert env["ORBIT_NODE_RANK"] == "0" and env["NCCL_NET"] == "Socket"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:9" and "node1" in env["NO_PROXY"]


@pytest.mark.asyncio
async def test_each_nodes_sshd_gets_its_variables_and_its_relays_while_sealed():
    from orbit.run.guestd import nodes
    from orbit.run.providers.cluster.provider import ClusterSandbox

    joined = {}

    class Client:
        def __init__(self, name):
            self.name = name

        async def exec(self, argv, env):
            assert argv[-1] == "join"
            joined[self.name] = json.loads(env[nodes.VARIABLE])
            return 0, "", "", False

    fake = SimpleNamespace(
        nodes=("n0", "n1"),
        _clients=(Client("n0"), Client("n1")),
        _node_variables=({"ORBIT_NODE_RANK": "0"}, {"ORBIT_NODE_RANK": "1"}),
        _peers=("node0", "node1"),
        request=SimpleNamespace(user="agent"),
        keys={"host": ["private", "ssh-ed25519 AAAA orbit-host"]},
    )
    await ClusterSandbox.join(fake, ["127.0.0.1:1", "127.0.0.1:2"])
    sealed = joined["n1"]
    assert (sealed["rank"], sealed["names"], sealed["addresses"]) == (
        1,
        ["node0", "node1"],
        ["10.214.0.1", "10.214.0.2"],
    )
    assert sealed["account"] == "agent" and sealed["keys"] == fake.keys
    assert sealed["variables"]["ORBIT_NODE_RANK"] == "1"
    assert sealed["variables"]["HTTP_PROXY"] == "http://127.0.0.1:2"
    await ClusterSandbox.join(fake, [None, None])
    assert joined["n1"]["variables"] == {"ORBIT_NODE_RANK": "1"}


def test_a_node_gives_only_its_hcas_whose_infiniband_port_is_active(tmp_path):
    for hca, layer, state, verbs in (
        ("mlx5_0", "Ethernet", "4: ACTIVE", "uverbs0"),
        ("mlx5_1", "InfiniBand", "4: ACTIVE", "uverbs1"),
        ("mlx5_2", "InfiniBand", "1: DOWN", "uverbs2"),
    ):
        port = tmp_path / hca / "ports" / "1"
        port.mkdir(parents=True)
        (port / "link_layer").write_text(layer + "\n")
        (port / "state").write_text(state + "\n")
        (tmp_path / hca / "device" / "infiniband_verbs" / verbs).mkdir(parents=True)
    assert worker.infiniband(tmp_path) == ["/dev/infiniband/uverbs1"]


def test_a_job_sweeps_only_empty_store_directories_some_minutes_old(tmp_path):
    import os
    import time

    old, fresh, full = (tmp_path / f"{worker.STORE_PREFIX}{name}" for name in ("o", "f", "u"))
    for store in (old, fresh, full):
        store.mkdir()
    (full / worker.OWNER).write_text("5")
    hour_ago = time.time() - 3600
    for store in (old, full):
        os.utime(store, (hour_ago, hour_ago))
    assert worker.empty_stores(tmp_path) == [old]


@pytest.mark.asyncio
async def test_a_snapshot_record_of_another_form_is_left_out_of_the_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    files = tmp_path / CACHE / "snapshots"
    files.mkdir(parents=True)
    (files / "orbit-old-1.json").write_text(
        json.dumps(dict(handle="slurm:orbit-old/1", image=SAVED, episode="e", roles=["solver"]))
    )
    assert await provider().list_retained() == []
    with pytest.raises(ProviderCapabilityError, match="records snapshots otherwise"):
        await in_job()._boot(_snapshot_request("slurm:orbit-old/1"), (), MIN_LEFT_SEC)


_AS_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the modes these check")


@_AS_ROOT
def test_a_shared_directory_is_copied_whole_its_links_modes_and_times_too(tmp_path):
    source = tmp_path / "source"
    (source / "pkg/sub").mkdir(parents=True)
    (source / "pkg/m.py").write_text("x = 1\n")
    (source / "pkg/m.py").chmod(0o640)
    os.utime(source / "pkg/m.py", ns=(1_000_000_000, 2_000_000_000))
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    (source / "pkg/link").symlink_to("m.py")
    os.link(source / "pkg/m.py", source / "pkg/sub/hard.py")
    os.mkfifo(source / "pipe", 0o600)
    (source / "secret").write_text("kept")
    (source / "secret").chmod(0)
    (source / "pkg/sub").chmod(0o500)
    destination = tmp_path / "copy"
    worker.copy_tree(str(source), str(destination))
    assert (destination / "pkg/m.py").read_text() == "x = 1\n"
    assert stat.S_IMODE((destination / "pkg/m.py").stat().st_mode) == 0o640
    assert (destination / "pkg/m.py").stat().st_mtime_ns == 2_000_000_000
    assert stat.S_IMODE((destination / "run.sh").stat().st_mode) == 0o755
    assert os.readlink(destination / "pkg/link") == "m.py"
    assert (destination / "pkg/m.py").stat().st_ino == (
        destination / "pkg/sub/hard.py"
    ).stat().st_ino
    assert stat.S_ISFIFO((destination / "pipe").lstat().st_mode)
    assert stat.S_IMODE((destination / "secret").stat().st_mode) == 0
    assert stat.S_IMODE((source / "secret").stat().st_mode) == 0
    assert stat.S_IMODE((destination / "pkg/sub").stat().st_mode) == 0o500
    worker.remove_tree(str(destination))
    worker.remove_tree(str(source))
    assert not destination.exists() and not source.exists()
    worker.remove_tree(str(source))  # nothing left to remove


@_AS_ROOT
def test_a_shared_directory_its_owner_may_not_list_is_removed_too(tmp_path):
    root = tmp_path / "shared"
    (root / "locked/inner").mkdir(parents=True)
    (root / "locked/inner/f").write_text("x")
    (root / "locked/inner").chmod(0)
    (root / "locked").chmod(0)
    worker.remove_tree(str(root))
    assert not root.exists()


def test_what_a_node_wrote_counts_each_files_blocks_once(tmp_path):
    with open(tmp_path / "a", "wb") as file:
        file.write(os.urandom(1 << 20))
        os.fsync(file.fileno())  # so that a file system that allocates lazily has its blocks
    os.link(tmp_path / "a", tmp_path / "b")
    with open(tmp_path / "sparse", "wb") as file:
        file.truncate(1 << 30)
    counted = worker.usage([str(tmp_path), str(tmp_path / "missing")])
    assert (1 << 20) <= counted < (1 << 20) + (64 << 10)


def test_a_sandbox_far_below_its_storage_is_measured_rarely_and_near_it_often():
    from orbit.run.providers import storage

    assert storage.interval(0) == storage.interval(-5) == storage.MIN_SEC
    assert storage.interval(10 << 30) == storage.MIN_SEC
    assert storage.interval(100 << 30) == 50.0
    assert storage.interval(10 << 40) == storage.MAX_SEC
    schedule = storage.measure_schedule(4096)
    assert schedule["storage_mb"] == 4096
    for headroom in (0, 100 << 30, 10 << 40):
        assert worker.interval(schedule, headroom) == storage.interval(headroom)
    # Two nodes write what node 0 counts: twice as soon.
    assert worker.interval(storage.measure_schedule(4096, writers=2), 100 << 30) == 25.0


@pytest.mark.asyncio
async def test_a_job_runs_its_command_in_its_directory_as_the_account_and_queue_ask(
    monkeypatch, tmp_path
):
    """Submitted without the Host's credentials, which sbatch would hand the job."""
    import shlex

    submitted = []

    async def run(*argv, timeout, env=None, **_):
        submitted.append((argv, env))
        return 0, "4242;cluster", ""

    monkeypatch.setattr(slurm_module, "run", run)
    monkeypatch.setenv("ORBIT_TEST_API_KEY", "secret-value")
    part = SlurmPartition(name="gpu", qos="short", max_cpus=8, max_memory_mb=8192, constraint="ib")
    config = SlurmProviderConfig(
        partitions=(part,), account="project-a", shared_fs_dir=str(tmp_path), node_local_dir="/l/$U"
    )
    job = slurm_module.SlurmJob(config, part)
    job.stage({"seeds": []}, name="allocation.json")
    assert json.loads((job.directory / "allocation.json").read_text())["node_local_dir"] == "/l/$U"
    argv = ["/engine/bin/python3", "-I", "worker.py", "prepare"]
    await job.submit(cpus=2, memory_mb=1024, gpus=1, minutes=10, argv=argv)
    ((command, env),) = submitted
    assert command[:3] == ("flock", str(job.directory / "submission"), "sh")
    assert "ORBIT_TEST_API_KEY" not in env and env["PATH"] == os.environ["PATH"]
    sbatch = shlex.split(command[-1])
    for wanted in ("--account=project-a", "--constraint=ib", "--qos=short", "--gres=gpu:1"):
        assert wanted in sbatch
    assert "--no-requeue" in sbatch and job.id == "4242"
    # What a site's epilog leaves in the job's working directory stays out of the cache.
    assert "--chdir=/tmp" in sbatch and f"--output={job.directory}/job.log" in sbatch
    assert (
        f"--wrap=cd {shlex.quote(str(job.directory))} && test ! -s stop && exec "
        + ("/engine/bin/python3 -I worker.py prepare >>job.log 2>&1")
        in sbatch
    )
    bare = part.model_copy(update={"constraint": None})
    plain = slurm_module.SlurmJob(
        SlurmProviderConfig(partitions=(bare,), shared_fs_dir=str(tmp_path)), bare
    )
    plain.stage()
    assert sorted(path.name for path in plain.directory.iterdir()) == ["stop", "worker.py"]
    submitted.clear()
    await plain.submit(cpus=2, memory_mb=1024, gpus=0, minutes=10, argv=argv)
    assert "--account" not in submitted[0][0][-1] and "--constraint" not in submitted[0][0][-1]
    assert "--gres" not in submitted[0][0][-1]


@pytest.mark.asyncio
async def test_an_image_is_prepared_by_the_engines_python_on_the_cluster(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    submitted, stopped = [], []

    async def engine(config):
        return Path("/cache/engine/abc/python/bin/python3")

    async def submit(job, **resources):
        submitted.append(resources)
        (job.directory / "prepared").write_text(json.dumps({"image": OTHER}))

    async def stop(job, **_):
        stopped.append(job.name)

    monkeypatch.setattr(images, "engine", engine)
    monkeypatch.setattr(images, "_framework", lambda config, user: (tmp_path, tmp_path))
    monkeypatch.setattr(slurm_module.SlurmJob, "submit", submit)
    monkeypatch.setattr(slurm_module.SlurmJob, "stop", stop)
    recipe = images.Recipe(
        key=PREPARED, source="external-ref", reference="ghcr.io/a/b@sha256:" + "4" * 64
    )
    prepared = await images._prepare(provider().config, recipe)
    assert prepared == {"image": OTHER} and len(stopped) == 1
    (resources,) = submitted
    assert resources["argv"] == [
        "/cache/engine/abc/python/bin/python3",
        "-I",
        "worker.py",
        "prepare",
    ]
    assert (resources["cpus"], resources["gpus"]) == (images.BUILD_CPUS, 0)


def test_a_job_loads_a_written_out_snapshots_layers_with_its_image(monkeypatch, tmp_path):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker, "node_store", lambda: {"directory": "/store", "runsc": "runsc"})
    monkeypatch.setattr(worker, "podman", lambda directory: ["podman"])
    monkeypatch.setattr(worker, "load", lambda *args: calls.append(("load", *args)))
    monkeypatch.setattr(worker, "load_layers", lambda *args: calls.append(("layers", *args)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker.py", "load", "snapshots/s.0.tar", "a" * 64, "L", "snapshots/s.0.L.tar"],
    )
    worker.main()
    assert calls == [
        ("load", ["podman"], "snapshots/s.0.tar", "a" * 64),
        ("layers", ["podman"], "/store", {"L": "snapshots/s.0.L.tar"}),
    ]


def test_a_node_keeps_its_stores_where_the_configuration_names_expanded_there(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SLURM_JOB_ID", "7")
    monkeypatch.setenv("PLACE", str(tmp_path / "local"))
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    directory, _, scratch = worker.store({"node_local_dir": "$PLACE/stores"})
    assert scratch == tmp_path / "local/stores" and Path(directory).parent == scratch
    assert (Path(directory) / worker.OWNER).read_text() == "7"
    monkeypatch.setenv("SLURM_TMPDIR", str(tmp_path / "slurm"))
    (tmp_path / "slurm").mkdir()
    _, _, default = worker.store({"node_local_dir": None})
    assert default == tmp_path / "slurm"


@pytest.mark.asyncio
async def test_a_sandbox_a_node_stopped_past_its_storage_says_so(tmp_path):
    from orbit.core.errors import GuestUnreachableError, StorageExhaustedError
    from orbit.run.providers.cluster.job import JobTransport

    box = tmp_path / "sandboxes" / "3" / "nodes" / "1"
    box.mkdir(parents=True)
    (box / "storage").write_text(json.dumps(dict(written_mb=4102, storage_mb=4096)))

    async def ended():
        return ""

    async def settled(seconds):
        return None

    job = SimpleNamespace(id="9", directory=tmp_path, ended=ended)
    watcher = SimpleNamespace(settled=settled, oom_killed=lambda: "")
    transport = JobTransport(["true"], SimpleNamespace(job=job, index="3"), None, [watcher] * 2)
    explained = await transport.explain(GuestUnreachableError("lost"))
    assert isinstance(explained, StorageExhaustedError)
    assert str(explained) == (
        "Orbit's storage watch stopped the Sandbox: node 1 wrote 4102 MiB over its image, past "
        "the 4096 MiB its Task declares"
    )


@pytest.mark.asyncio
async def test_a_shared_directory_stays_while_a_snapshot_written_out_names_it(tmp_path):
    from orbit.core.config import SlurmProviderConfig
    from orbit.run.providers.cluster import ClusterProvider

    slurm = ClusterProvider(SlurmProviderConfig(shared_fs_dir=str(tmp_path)))
    (tmp_path / "shared" / "orbit-a-1").mkdir(parents=True)
    (tmp_path / "shared" / "orbit-a-1" / "f").write_text("kept")
    (tmp_path / "snapshots").mkdir()
    record = tmp_path / "snapshots" / "orbit-a-1.json"
    record.write_text(json.dumps(dict(shared="shared/orbit-a-1", written=[5, 7])))
    assert slurm._snapshot_state("slurm:orbit-a/1") == ("shared/orbit-a-1", (5, 7))
    slurm.shared_users["shared/orbit-a-1"] = 1
    record.unlink()
    await slurm.release_shared("shared/orbit-a-1")
    assert (tmp_path / "shared" / "orbit-a-1" / "f").exists()  # a Sandbox still uses it
    slurm.shared_users["shared/orbit-a-1"] = 0
    record.write_text(json.dumps(dict(shared="shared/orbit-a-1")))
    await slurm.release_shared("shared/orbit-a-1")
    assert (tmp_path / "shared" / "orbit-a-1").exists()  # the written-out snapshot keeps it
    record.unlink()
    await slurm.release_shared("shared/orbit-a-1")
    assert not (tmp_path / "shared" / "orbit-a-1").exists()
    assert slurm._snapshot_state("slurm:orbit-a/1") == (None, ())


@_AS_ROOT
def test_a_walk_follows_no_link_and_gives_an_unsearchable_directory_its_mode_back(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "big").write_bytes(b"x" * (1 << 20))
    root = tmp_path / "shared"
    (root / "hidden").mkdir(parents=True)
    (root / "hidden" / "f").write_bytes(b"y" * 8192)
    (root / "hidden").chmod(0o644)  # listable, not searchable
    (root / "away").symlink_to(tmp_path / "outside")
    counted = worker.usage([str(root)])
    assert 8192 <= counted < (1 << 20)
    assert stat.S_IMODE((root / "hidden").stat().st_mode) == 0o644
    worker.remove_tree(str(root))
    assert not root.exists() and (tmp_path / "outside" / "big").exists()


@pytest.mark.asyncio
async def test_an_image_named_without_a_task_carries_the_nodes_openssh(monkeypatch):
    """No Task says whether a Sandbox of a bare reference spans several nodes."""
    asked = []

    async def pin(reference):
        return reference + "@sha256:" + "a" * 64

    async def prepare_recipe(self, recipe):
        asked.append(recipe)
        return "prepared", False

    monkeypatch.setattr(images, "pin", pin)
    monkeypatch.setattr(ClusterProvider, "prepare_recipe", prepare_recipe)
    await provider().prepare_image(ImageRef(kind=ImageKind.CONTAINER, reference="base:1"))
    (recipe,) = asked
    assert recipe.multinode
    shape = dict(reference=recipe.reference, context=None, user=recipe.user, keep_alive=False)
    assert recipe.key == images.key("external-ref", **shape, multinode=True)
    assert recipe.key != images.key("external-ref", **shape, multinode=False)


@pytest.mark.parametrize(
    ("phase", "failure"),
    [("build", TaskDefinitionError), ("framework", ProviderStartError), ("", ProviderStartError)],
)
@pytest.mark.parametrize("plain", [False, True], ids=["worker-json", "plain-text"])
async def test_image_preparation_attributes_the_worker_failure(tmp_path, phase, failure, plain):
    config = provider().config
    job = job_module.Job(config, config.partitions[0])
    job.directory = tmp_path
    job.id = "241"
    message = 'build command failed:\n"missing-file"'
    for name, value in (("error", message), ("failed", phase)):
        if value:
            path = tmp_path / name
            if plain:
                path.write_text(value)
            else:
                worker.write(path, value)
    (tmp_path / "job.log").write_text("worker stderr")
    recipe = images.Recipe(key="key", source="solver-local", context=tmp_path)
    with pytest.raises(failure) as caught:
        await images._prepared(job, 1, recipe)
    detail = str(caught.value)
    assert "slurm job 241" in detail and message in detail and "worker stderr" in detail
    if phase:
        assert f"during {phase}" in detail
    if phase == "build":
        assert str(tmp_path / "Dockerfile") in detail
