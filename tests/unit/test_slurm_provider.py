"""Slurm selection must find one feasible allocation on one compute node."""

import subprocess

import pytest

from ale.core.capability import SandboxNeeds
from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError
from ale.core.sandbox import (
    ImageKind,
    PreparedTaskImage,
    ResolvedImage,
    SandboxRequest,
    SandboxRole,
    snapshot_verifier_needs,
    verifiers_needs,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.providers import ProviderRegistry
from ale.run.providers.slurm import worker
from ale.run.providers.slurm.job import Job, JobReady
from ale.run.providers.slurm.provider import (
    JOB_OVERHEAD_MB,
    MAX_SAVES,
    _capacity,
    _Chain,
    _handle,
    _parse,
    _profiles,
    _Snapshot,
    _volume_specs,
    step_memory_mb,
)


def provider():
    partitions = [
        dict(host="cpu", name="cpu", max_cpus=64, max_memory_mb=32768),
        dict(
            host="gpu",
            name="small",
            max_cpus=4,
            max_memory_mb=16384,
            max_gpus=2,
            gpu_model=dict(name="NVIDIA H100 80GB HBM3", memory_mb=81559),
        ),
        dict(
            host="gpu",
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
        (Resources(cpus=64), True),
        (Resources(gpus=2, gpu_types=("H100",)), True),
        (Resources(gpus=1, gpu_memory_mb=100000), True),
        (Resources(gpus=2, gpu_memory_mb=100000), False),
        (Resources(cpus=8, gpus=1, gpu_types=("H100",)), False),
        (Resources(gpus=3), False),
        (Resources(storage_mb=1000), False),
        (Resources(container_features=("nested_containers", "raw_sockets")), False),
        (Resources(container_features=("nested_containers", "mount", "fuse")), True),
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
        else snapshot_verifier_needs(ImageKind.CONTAINER, OperatingSystem.LINUX, verifier),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("solver", "verifier", "served"),
    [
        # One allocation holds the solver and, after it, the verifier booted from its
        # snapshot: each dimension is the larger of the two.
        (Resources(gpus=1, gpu_types=("H100",)), Resources(cpus=4, gpus=2), True),
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
async def test_snapshots_are_saved_as_deep_as_measured_container_engine_or_not():
    slurm = provider()
    for feature in ("raw_sockets", "nested_containers"):
        saved = needs(Resources(container_features=(feature,)), saves=MAX_SAVES)
        assert not await slurm.unserved(saved, user="user", role=SandboxRole.SOLVER)
    deeper = needs(Resources(), saves=MAX_SAVES + 1)
    assert await slurm.unserved(deeper, user="user", role=SandboxRole.SOLVER)


def test_an_allocation_is_sized_for_every_verifier_it_may_hold_in_turn():
    """The snapshot verifier, a validation round's, and the separate one that views the
    solver's volumes: each dimension is the largest."""
    verifiers = verifiers_needs(
        ImageKind.CONTAINER,
        OperatingSystem.LINUX,
        (Resources(cpus=3, memory_mb=1024), Resources(cpus=1, memory_mb=6144)),
    )
    solver = needs(Resources(cpus=2, memory_mb=2048)).model_copy(update={"verifier": verifiers})
    assert _capacity(solver) == (3, step_memory_mb(6144), 0)
    assert _capacity(solver, Resources(cpus=1, gpus=1)) == (3, step_memory_mb(6144), 1)


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


def test_the_largest_sandbox_a_profile_declares_fits_its_partition():
    for part in provider().config.partitions:
        for profile in _profiles(part):
            assert profile.max_memory_mb is not None
            assert step_memory_mb(profile.max_memory_mb) + JOB_OVERHEAD_MB <= part.max_memory_mb
            assert step_memory_mb(profile.max_memory_mb + 1) + JOB_OVERHEAD_MB > part.max_memory_mb


PREPARED, OTHER, SAVED = ("sha256:" + digit * 64 for digit in "123")


def chain(cpus=4, memory_mb=8192, gpus=2):
    job = Job(provider().config.partitions[1])
    return _Chain(
        job=job,
        ready=JobReady(runtime="runsc", node="n"),
        seed=SAVED,
        cpus=cpus,
        memory_mb=memory_mb,
        gpus=gpus,
    )


def test_an_allocation_starts_sandboxes_while_they_fit_beside_what_runs():
    held = chain(memory_mb=2 * step_memory_mb(2048))
    first = held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1))
    second = held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1))
    assert (first, second) == ("1", "2")
    assert held.reserve(Resources(cpus=1, memory_mb=512)) is None
    held.running.pop(first)
    assert held.reserve(Resources(cpus=2, memory_mb=2048, gpus=2)) is None
    assert held.reserve(Resources(cpus=2, memory_mb=2048, gpus=1)) == "3"


def test_an_allocation_is_kept_while_its_episode_needs_a_snapshot_in_it():
    held = chain()
    snapshot = _Snapshot(
        index="1",
        image="sha256:1",
        prepared_identity="p",
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=1,
    )
    held.snapshots["1"] = snapshot
    assert not held.idle  # only here: nothing wrote it out
    snapshot.archive = "snapshots/x.tar.gz"
    assert not held.idle  # its verifier boots from it here still
    snapshot.boots = 0
    assert held.idle
    held.reserve(Resources())
    assert not held.idle


def test_a_snapshot_handle_names_its_host_allocation_and_sandbox():
    held = chain()
    assert _parse(_handle(held.job, "3")) == ("gpu", held.job.name, "3")
    for wrong in ("slurm:gpu", "slurm:gpu/x/1", "slurm:gpu/ale-1/x", "docker:gpu/ale-1/1"):
        with pytest.raises(ProviderCapabilityError):
            _parse(wrong)


@pytest.mark.asyncio
async def test_a_snapshot_boot_that_does_not_fit_gets_an_allocation_that_loads_it(monkeypatch):
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    snapshot = _Snapshot(
        index="1",
        image=SAVED,
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=0,
        stacks={"/work": dict(layers=["x.1.0"], root=[0, 0, 0o755], copied=False)},
    )
    handle = _handle(held.job, "1")
    held.snapshots["1"] = snapshot
    slurm.snapshots[handle] = (held, snapshot)
    written, allocated, loaded = [], [], []

    async def write_out(chain, saved):
        written.append(saved)
        saved.archive = "snapshots/out.tar.gz"
        return saved.archive

    async def allocate(needs, *, image, archive=None, host=None, layers=None):
        allocated.append((image, archive, host, layers))
        return chain(cpus=8, memory_mb=step_memory_mb(8192), gpus=1)

    async def framed(request):
        return ResolvedImage(
            kind=ImageKind.CONTAINER,
            prepared_identity=OTHER,
            observed_identity=OTHER,
            observed_ref=OTHER,
        )

    async def load(chain, image):
        loaded.append((chain, image))
        return image

    monkeypatch.setattr(slurm, "write_out", write_out)
    monkeypatch.setattr(slurm, "_allocate", allocate)
    monkeypatch.setattr(slurm, "_framed", framed)
    monkeypatch.setattr(slurm, "_load", load)
    image = PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=PREPARED,
        runtime_ref="ref",
        resolved_reference="ref",
        prepared_identity=PREPARED,
    )

    def request(resources):
        return SandboxRequest(
            episode_id="e",
            prepared_image=image,
            snapshot=handle,
            resources=resources,
            network=NetworkPolicy(mode=NetworkMode.BLOCK),
        )

    view = [dict(path="/work", kind="view", layers=["x.1.0"], root=[0, 0, 0o755])]
    booted, index, resolved, seed, specs = await slurm._boot(request(Resources(cpus=2, gpus=1)), ())
    assert (booted, index, resolved.observed_identity, seed) == (held, "1", SAVED, SAVED)
    assert specs == view and held.viewing["1"] == {"x.1.0"}
    assert not written and not allocated
    # A verifier of the verifier's image views the volumes in the same allocation, which
    # loads that image.
    held.running.clear()
    viewer = request(Resources(cpus=1)).model_copy(
        update={"snapshot": None, "volumes_from": handle}
    )
    booted, index, resolved, seed, specs = await slurm._boot(viewer, ())
    assert (booted, seed, specs) == (held, OTHER, view) and loaded == [(held, OTHER)]
    booted, index, _, _, _ = await slurm._boot(request(Resources(cpus=8, gpus=1)), ())
    assert booted is not held and written == [snapshot]
    assert allocated == [
        (SAVED, "snapshots/out.tar.gz", "gpu", {"x.1.0": "snapshots/out.x.1.0.tar"})
    ]
    with pytest.raises(ProviderCapabilityError, match="prepared solver image"):
        await slurm._boot(
            request(Resources()).model_copy(
                update={"prepared_image": image.model_copy(update={"prepared_identity": OTHER})}
            ),
            (),
        )


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
