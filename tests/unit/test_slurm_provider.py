"""Slurm selection must find one feasible allocation of alike compute nodes."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbit.core.capability import SandboxNeeds
from orbit.core.config import RunConfig
from orbit.core.errors import AllocationEndedError, ProviderCapabilityError, ProviderStartError
from orbit.core.sandbox import (
    ImageKind,
    PreparedTaskImage,
    SandboxRequest,
    SandboxRole,
    snapshot_verifier_needs,
    verifiers_needs,
)
from orbit.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from orbit.run.oci import Reference, api, qualified
from orbit.run.providers import ProviderRegistry
from orbit.run.providers.docker import gvisor
from orbit.run.providers.slurm import images, worker
from orbit.run.providers.slurm.job import Job, JobReady
from orbit.run.providers.slurm.job import seconds as job_seconds
from orbit.run.providers.slurm.provider import (
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
        (Resources(cpus=64), True),
        (Resources(gpus=2, gpu_types=("H100",)), True),
        (Resources(gpus=1, gpu_memory_mb=100000), True),
        (Resources(gpus=2, gpu_memory_mb=100000), False),
        (Resources(cpus=8, gpus=1, gpu_types=("H100",)), False),
        (Resources(gpus=3), False),
        (Resources(storage_mb=1000), False),
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


def test_the_largest_sandbox_a_profile_declares_fits_its_partition():
    for part in provider().config.partitions:
        for profile in _profiles(part, provider().config):
            assert profile.max_memory_mb is not None
            assert step_memory_mb(profile.max_memory_mb) + JOB_OVERHEAD_MB <= part.max_memory_mb
            assert step_memory_mb(profile.max_memory_mb + 1) + JOB_OVERHEAD_MB > part.max_memory_mb


PREPARED, OTHER, SAVED = ("sha256:" + digit * 64 for digit in "123")


@pytest.fixture(autouse=True)
def running_jobs(monkeypatch):
    """Every job Slurm is asked about runs, unless a test says otherwise."""

    async def state(job):
        return "RUNNING"

    monkeypatch.setattr(Job, "state", state)


def chain(cpus=4, memory_mb=8192, gpus=2, nodes=1):
    job = Job(provider(max_nodes=nodes).config, provider(max_nodes=nodes).config.partitions[1])
    names = tuple(f"n{rank}" for rank in range(nodes))
    return _Chain(
        job=job,
        ready=JobReady(runtime="runsc", node=names[0], nodes=names),
        seeds=(SAVED,) * nodes,
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
        images=("sha256:1",),
        prepared_identity="p",
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=1,
    )
    held.snapshots["1"] = snapshot
    assert not held.idle  # only here: nothing wrote it out
    snapshot.archives = ("snapshots/x.tar",)
    assert not held.idle  # its verifier boots from it here still
    snapshot.boots = 0
    assert held.idle
    held.reserve(Resources(cpus=1, memory_mb=1024))
    assert not held.idle


def test_a_snapshot_handle_names_its_allocation_and_sandbox():
    held = chain()
    assert _parse(_handle(held.job, "3")) == (held.job.name, "3")
    for wrong in ("slurm:orbit-1", "slurm:x/1", "slurm:orbit-1/x", "docker:orbit-1/1"):
        with pytest.raises(ProviderCapabilityError):
            _parse(wrong)


@pytest.mark.asyncio
async def test_a_snapshot_boot_that_does_not_fit_gets_an_allocation_that_loads_it(monkeypatch):
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    snapshot = _Snapshot(
        index="1",
        images=(SAVED,),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=0,
        stacks=({"/work": dict(layers=["x.1.0"], root=[0, 0, 0o755], copied=False)},),
    )
    handle = _handle(held.job, "1")
    held.snapshots["1"] = snapshot
    slurm.snapshots[handle] = (held, snapshot)
    written, allocated, loaded = [], [], []

    async def write_out(chain, saved):
        written.append(saved)
        saved.archives = ("snapshots/out.0.tar",)
        return saved.archives

    async def allocate(needs, *, seeds, separate=None, patient=False):
        assert patient, "a boot from a snapshot waits in the queue for as long as it takes"
        allocated.append(("sha256:" + seeds[0]["image"], seeds[0]["archive"], seeds[0]["layers"]))
        return chain(cpus=8, memory_mb=step_memory_mb(8192), gpus=1)

    async def load(chain, image):
        loaded.append((chain, image.prepared_identity))
        return image.prepared_identity

    monkeypatch.setattr(slurm, "write_out", write_out)
    monkeypatch.setattr(slurm, "_allocate", allocate)
    monkeypatch.setattr(slurm, "_load", load)
    # Both images were prepared for the cluster.
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})
    image = PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=PREPARED,
        runtime_ref="images/solver.tar.gz",
        resolved_reference="ref",
        prepared_identity=PREPARED,
    )
    own = image.model_copy(
        update={"runtime_ref": "images/verifier.tar.gz", "prepared_identity": OTHER}
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
    booted, index, resolved, seed, specs = await slurm._boot(
        request(Resources(cpus=2, memory_mb=4096, gpus=1)), ()
    )
    assert (booted, index, resolved.observed_identity, seed) == (held, "1", SAVED, [SAVED])
    assert specs == [view] and held.viewing["1"] == {"x.1.0"}
    assert not written and not allocated
    # A verifier of the verifier's image views the volumes in the same allocation, which
    # loads that image.
    held.running.clear()
    viewer = request(Resources(cpus=1, memory_mb=1024)).model_copy(
        update={"snapshot": None, "volumes_from": handle, "prepared_image": own}
    )
    booted, index, resolved, seed, specs = await slurm._boot(viewer, ())
    assert (booted, seed, specs) == (held, [OTHER], [view]) and loaded == [(held, OTHER)]
    booted, index, _, _, _ = await slurm._boot(request(Resources(cpus=8, gpus=1)), ())
    assert booted is not held and written == [snapshot]
    assert allocated == [(SAVED, "snapshots/out.0.tar", {"x.1.0": "snapshots/out.0.x.1.0.tar"})]
    # A viewer that does not fit either loads its own image in an allocation of its own.
    viewer = viewer.model_copy(update={"resources": Resources(cpus=8, gpus=1)})
    await slurm._boot(viewer, ())
    assert allocated[-1] == (
        OTHER,
        "images/verifier.tar.gz",
        {"x.1.0": "snapshots/out.0.x.1.0.tar"},
    )
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


def test_slurm_prints_a_time_left_as_it_counts_it():
    assert [job_seconds(text) for text in ("05:07", "1:02:03", "2-00:00:01", "UNLIMITED", "")] == [
        307,
        3723,
        172801,
        None,
        None,
    ]


def test_an_allocation_takes_a_sandbox_only_while_it_lasts_long_enough():
    """Past what is left before its deadline, a verifier, or a solver that would move at
    once, gets an allocation of its own instead."""
    held = chain()
    held.deadline = datetime.now(UTC) + timedelta(seconds=600)
    assert held.reserve(Resources(cpus=1, memory_mb=1024), lasting=300) is not None
    assert held.reserve(Resources(cpus=1, memory_mb=1024), lasting=900) is None
    # One that names no time fits as the allocation was sized for it.
    assert held.reserve(Resources(cpus=1, memory_mb=1024)) is not None


@pytest.mark.asyncio
async def test_a_verifier_needed_longer_than_an_allocation_lasts_is_refused_at_selection():
    """Before anything is prepared or run: an allocation holds a Sandbox for its wall time
    less its drain."""
    slurm = provider()
    usable = (slurm.config.walltime_minutes - slurm.config.drain_minutes) * 60
    fits = needs(Resources(cpus=2)).model_copy(update={"duration_sec": usable})
    assert not await slurm.unserved(fits, user="user", role=SandboxRole.SOLVER)
    longer = fits.model_copy(update={"duration_sec": usable + 1})
    why = await slurm.unserved(longer, user="user", role=SandboxRole.SOLVER)
    assert why and "a verifier may take" in " ".join(why)


@pytest.mark.asyncio
async def test_a_solver_moves_from_an_allocation_at_its_deadline_which_then_ends(monkeypatch):
    """Its snapshot is written out for the allocation the moved solver boots in, and the one
    it leaves holds nothing more, so it ends at once instead of at its wall time."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    held.deadline = datetime.now(UTC) + timedelta(seconds=30)
    snapshot = _Snapshot(
        index="1",
        images=(SAVED,),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=0,
    )
    handle = _handle(held.job, "1")
    held.snapshots["1"] = snapshot
    slurm.snapshots[handle] = (held, snapshot)
    stopped, allocated = [], []

    async def write_out(chain, saved):
        saved.archives = ("snapshots/out.0.tar",)
        return saved.archives

    async def stop(job):
        stopped.append(job.name)

    async def allocate(needs, *, seeds, separate=None, patient=False):
        assert patient, "a boot from a snapshot waits in the queue for as long as it takes"
        allocated.append(("sha256:" + seeds[0]["image"], seeds[0]["archive"]))
        return chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)

    monkeypatch.setattr(slurm, "write_out", write_out)
    monkeypatch.setattr(slurm, "_allocate", allocate)
    monkeypatch.setattr(Job, "stop", stop)
    image = PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=PREPARED,
        runtime_ref="images/solver.tar.gz",
        resolved_reference="ref",
        prepared_identity=PREPARED,
    )
    request = SandboxRequest(
        episode_id="e",
        prepared_image=image,
        snapshot=handle,
        resources=Resources(cpus=2, memory_mb=4096, gpus=1),
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
    )
    booted, *_ = await slurm._boot(request, ())
    assert booted is not held and allocated == [(SAVED, "snapshots/out.0.tar")]
    assert held.closed and stopped == [held.job.name] and handle not in slurm.snapshots


def _saved(held, *, boots=0):
    snapshot = _Snapshot(
        index="1",
        images=(SAVED,),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=boots,
    )
    held.snapshots["1"] = snapshot
    return snapshot


def _booting(slurm, monkeypatch):
    allocated, stopped = [], []

    async def write_out(chain, saved):
        saved.archives = ("snapshots/out.0.tar",)
        return saved.archives

    async def stop(job, **_):
        stopped.append(job.name)

    async def allocate(needs, *, seeds, separate=None, patient=False):
        assert patient, "a boot from a snapshot waits in the queue for as long as it takes"
        allocated.append(("sha256:" + seeds[0]["image"], seeds[0]["archive"]))
        return chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)

    monkeypatch.setattr(slurm, "write_out", write_out)
    monkeypatch.setattr(slurm, "_allocate", allocate)
    monkeypatch.setattr(Job, "stop", stop)
    return allocated, stopped


def _snapshot_request(handle, **update):
    image = PreparedTaskImage(
        kind=ImageKind.CONTAINER,
        source="external-ref",
        input_identity=PREPARED,
        runtime_ref="images/solver.tar.gz",
        resolved_reference="ref",
        prepared_identity=PREPARED,
    )
    update.setdefault("resources", Resources(cpus=2, memory_mb=4096, gpus=1))
    return SandboxRequest(
        episode_id="e",
        prepared_image=image,
        snapshot=handle,
        network=NetworkPolicy(mode=NetworkMode.BLOCK),
        **update,
    )


@pytest.mark.asyncio
async def test_a_sandbox_boots_in_a_running_allocation_only_if_it_lasts_the_time_asked(
    monkeypatch,
):
    """The verifier's limit, which a solver moved for verification asks for too, or five
    minutes for a solver."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    held.deadline = datetime.now(UTC) + timedelta(seconds=600)
    _saved(held)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, held.snapshots["1"])
    allocated, _ = _booting(slurm, monkeypatch)
    booted, *_ = await slurm._boot(_snapshot_request(handle, duration_sec=500), ())
    assert booted is held and not allocated
    held.running.clear()
    booted, *_ = await slurm._boot(_snapshot_request(handle, duration_sec=900), ())
    assert booted is not held and allocated == [(SAVED, "snapshots/out.0.tar")]
    # Nothing runs there and its snapshot is written out: it ends, rather than idle.
    assert held.closed


@pytest.mark.asyncio
async def test_an_allocation_whose_deadline_came_ends_though_a_verifier_was_to_boot_there(
    monkeypatch,
):
    """A snapshot-verified solver's moves save it as a snapshot its verifier would boot from
    there: at the deadline, nothing will, so the allocation drains all the same."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    held.deadline = datetime.now(UTC) + timedelta(seconds=30)
    snapshot = _saved(held, boots=1)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    allocated, stopped = _booting(slurm, monkeypatch)
    booted, *_ = await slurm._boot(_snapshot_request(handle), ())
    assert booted is not held and allocated == [(SAVED, "snapshots/out.0.tar")]
    assert held.closed and stopped == [held.job.name] and snapshot.boots == 0
    assert handle not in slurm.snapshots


@pytest.mark.asyncio
async def test_a_snapshot_of_an_allocation_slurm_ended_is_destroyed_with_its_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    slurm = provider()
    held = chain()
    snapshot = _saved(held)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    files = tmp_path / CACHE / "snapshots"
    files.mkdir(parents=True)
    for name in ("tar", "json", "x.1.0.tar"):
        (files / f"{held.job.name}-1.{name}").write_text("")
    stopped = []

    async def control(job, *args, **_):
        raise ProviderStartError("srun: error: job has ended")

    async def state(job):
        return ""

    async def stop(job, **_):
        stopped.append(job.name)

    monkeypatch.setattr(Job, "control", control)
    monkeypatch.setattr(Job, "state", state)
    monkeypatch.setattr(Job, "stop", stop)
    await slurm.destroy_retained(handle)
    assert not list(files.iterdir()) and held.closed and stopped == [held.job.name]
    assert handle not in slurm.snapshots


@pytest.mark.asyncio
async def test_a_sandbox_slurm_ended_is_explained_so_before_an_oom_kill_it_outlived(
    monkeypatch,
):
    from orbit.core.errors import AllocationEndedError, GuestUnreachableError
    from orbit.run.providers.slurm import job as job_module

    class Watcher:
        async def settled(self, seconds):
            return None

        def oom_killed(self):
            return "1 OOM kill in the Sentry"

    async def slurm_command(*argv, **_):
        return "PREEMPTED" if "-nXj" in argv else ""

    monkeypatch.setattr(job_module, "slurm", slurm_command)
    held = chain()
    held.job.id = "77"
    transport = job_module.JobTransport(
        [],
        job_module.SandboxSteps(held.job, "1", "n0"),
        job_module.StepReady(image="i", devices=(), step="0", node="n0"),
        [Watcher()],
    )
    explained = await transport.explain(GuestUnreachableError("guest went away"))
    assert isinstance(explained, AllocationEndedError) and "PREEMPTED" in str(explained)


@pytest.mark.asyncio
async def test_an_allocations_deadline_is_slurms_own_count_less_its_drain(monkeypatch):
    slurm = provider()

    async def nothing(job, *args, **_):
        return None

    waited = []

    async def ready(job, timeout):
        waited.append(timeout)
        return JobReady(runtime="runsc", node="n", nodes=("n",))

    async def left(job):
        return 3600

    staged = []
    monkeypatch.setattr(Job, "stage", lambda job, spec, **_: staged.append(spec))
    monkeypatch.setattr(Job, "submit", nothing)
    monkeypatch.setattr(Job, "ready", ready)
    monkeypatch.setattr(Job, "time_left", left)
    before = datetime.now(UTC)
    held = await slurm._allocate(
        needs(Resources(cpus=2)), seeds=[dict(archive="images/x.tar.gz", image=SAVED, layers={})]
    )
    drain = timedelta(minutes=slurm.config.drain_minutes)
    assert held.deadline is not None
    assert before + timedelta(seconds=3600) - drain <= held.deadline
    assert held.deadline <= datetime.now(UTC) + timedelta(seconds=3600) - drain
    # A first allocation waits in the queue so long; one mid-episode as long as Slurm keeps it.
    await slurm._allocate(
        needs(Resources(cpus=2)),
        seeds=[dict(archive="x.tar", image=SAVED, layers={})],
        patient=True,
    )
    assert waited == [slurm.config.queue_timeout_sec, None]
    # Its nodes fetch the gVisor build this engine runs.
    assert (staged[0]["version"], staged[0]["build"]) == (gvisor.VERSION, gvisor.BUILD_ID)


@pytest.mark.asyncio
async def test_an_allocation_that_ends_once_its_snapshot_boots_elsewhere(monkeypatch):
    """A snapshot verifier that gets an allocation of its own leaves the solver's with
    nothing to hold: it ends then, not with the episode. One where a Sandbox still runs
    stays."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    snapshot = _saved(held, boots=1)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    _, stopped = _booting(slurm, monkeypatch)
    held.deadline = datetime.now(UTC) + timedelta(seconds=600)
    # A verifier whose limit the allocation does not last.
    larger = _snapshot_request(handle, duration_sec=900)
    held.running["2"] = (1, step_memory_mb(512), 0, 1)
    await slurm._boot(larger, ())
    assert snapshot.boots == 0 and not held.closed and not stopped
    held.running.clear()
    await slurm._boot(larger, ())
    assert held.closed and stopped == [held.job.name]


@pytest.mark.asyncio
async def test_a_snapshot_lost_with_its_allocation_is_reported_as_that(monkeypatch):
    """A round's verifier that lost its allocation runs again on the round's snapshot, but
    that snapshot was in the node's store only: the second try says so."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    _saved(held)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, held.snapshots["1"])
    allocated, stopped = _booting(slurm, monkeypatch)

    async def gone(job):
        return ""

    async def ended(job):
        return "PREEMPTED"

    monkeypatch.setattr(Job, "state", gone)
    monkeypatch.setattr(Job, "ended", ended)
    for _ in range(2):  # the round's verifier again, then the next turn's solver
        with pytest.raises(AllocationEndedError, match=r"\(PREEMPTED\).*not written out"):
            await slurm._boot(_snapshot_request(handle), ())
    assert not allocated and not stopped


@pytest.mark.asyncio
async def test_a_job_without_a_queue_deadline_waits_until_slurm_starts_it(
    tmp_path, monkeypatch, caplog
):
    """A boot from a snapshot waits in the queue past ``queue_timeout_sec``, saying why now
    and then; any other gives up at it."""
    from orbit.run.providers.slurm import job as job_module

    monkeypatch.setattr(job_module, "cache", lambda config: tmp_path)
    monkeypatch.setattr(job_module, "QUEUE_NOTICE_SEC", 0)
    clock = [0.0]
    states = ["PENDING"] * 5 + ["RUNNING"]

    async def tick(seconds):
        clock[0] += 400  # far past any queue deadline between two looks

    async def state(job):
        if len(states) == 1 and job.directory.is_dir():
            (job.directory / "ready").write_text(
                JobReady(runtime="runsc", node="n", nodes=("n",)).model_dump_json()
            )
        return states.pop(0) if len(states) > 1 else states[0]

    async def reason(job):
        return "Resources"

    # The job module's own clock and sleep, which nothing else in the test shares.
    loop = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(
        job_module, "asyncio", SimpleNamespace(sleep=tick, get_running_loop=lambda: loop)
    )
    monkeypatch.setattr(Job, "state", state)
    monkeypatch.setattr(Job, "pending_reason", reason)
    job = Job(provider().config, provider().config.partitions[0])
    job.directory.mkdir()
    with caplog.at_level("INFO", logger="orbit.execution"):
        ready = await job.ready(None)
    assert ready.node == "n"
    assert any(
        record.getMessage() == "a Slurm job still waits in the queue" for record in caplog.records
    )
    states[:] = ["PENDING"] * 5
    with pytest.raises(ProviderStartError, match="during queueing"):
        await Job(provider().config, provider().config.partitions[0]).ready(900)


@pytest.mark.asyncio
async def test_a_written_out_snapshot_of_an_allocation_slurm_ended_boots_from_its_archive(
    monkeypatch,
):
    """Its store went with the allocation, but the archive in the cache did not."""
    slurm = provider()
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1)
    snapshot = _saved(held)
    snapshot.archives = ("snapshots/out.0.tar",)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    allocated, stopped = _booting(slurm, monkeypatch)

    async def gone(job):
        return ""

    monkeypatch.setattr(Job, "state", gone)
    booted, *_ = await slurm._boot(_snapshot_request(handle), ())
    assert booted is not held and held.closed and stopped == [held.job.name]
    assert allocated == [(SAVED, f"snapshots/{held.job.name}-1.0.tar")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resources", "served"),
    [
        (Resources(cpus=4, gpus=2, nodes=2), True),
        (Resources(cpus=4, gpus=2, nodes=3), False),
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


def test_an_allocation_of_several_nodes_holds_each_sandbox_on_its_first_nodes():
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


def _group_snapshot(held):
    stack = {"/work": dict(layers=["x.1.0.0"], root=[0, 0, 0o755], copied=False)}
    other = {"/work": dict(layers=["x.1.1.0"], root=[0, 0, 0o755], copied=False)}
    snapshot = _Snapshot(
        index="1",
        images=(A, B),
        prepared_identity=PREPARED,
        episode_id="e",
        roles=("solver",),
        reason="r",
        boots=0,
        stacks=(stack, other),
    )
    held.snapshots["1"] = snapshot
    return snapshot


@pytest.mark.asyncio
async def test_each_node_of_a_sandbox_boots_from_the_same_node_of_its_snapshot(monkeypatch):
    slurm = provider(max_nodes=2)
    held = chain(cpus=2, memory_mb=step_memory_mb(4096), gpus=1, nodes=2)
    snapshot = _group_snapshot(held)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    monkeypatch.setattr(images, "recorded", lambda config, image: {"user": "user"})

    async def load(chain, image):
        return image.prepared_identity

    monkeypatch.setattr(slurm, "_load", load)
    group = _snapshot_request(handle, resources=Resources(cpus=2, memory_mb=4096, nodes=2))
    booted, index, resolved, loaded, specs = await slurm._boot(group, ())
    assert (booted, loaded, resolved.observed_identity) == (held, [A, B], A)
    assert [spec[0]["layers"] for spec in specs] == [["x.1.0.0"], ["x.1.1.0"]]
    assert held.viewing[index] == {"x.1.0.0", "x.1.1.0"}
    held.running.clear()
    # A snapshot verifier of one node boots from node 0 alone.
    _, _, _, loaded, specs = await slurm._boot(
        _snapshot_request(handle, resources=Resources(cpus=2, memory_mb=4096)), ()
    )
    assert loaded == [A] and len(specs) == 1
    held.running.clear()
    # It never spans nodes the snapshot does not keep.
    with pytest.raises(ProviderCapabilityError, match="keeps 2 node"):
        await slurm._boot(
            _snapshot_request(handle, resources=Resources(cpus=1, memory_mb=512, nodes=3)), ()
        )
    # A separate verifier views node k's volumes on its node k.
    viewer = _snapshot_request(
        handle, resources=Resources(cpus=2, memory_mb=4096, nodes=2)
    ).model_copy(update={"snapshot": None, "volumes_from": handle})
    _, _, _, loaded, specs = await slurm._boot(viewer, ())
    assert loaded == [PREPARED, PREPARED]
    assert [spec[0]["layers"] for spec in specs] == [["x.1.0.0"], ["x.1.1.0"]]


@pytest.mark.asyncio
async def test_each_node_writes_its_own_part_of_a_snapshot_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / CACHE / "snapshots").mkdir(parents=True)
    slurm = provider(max_nodes=2)
    held = chain(nodes=2)
    snapshot = _group_snapshot(held)
    ran = []

    async def control(job, *args, nodes, timeout=60):
        ran.append((args, tuple(nodes)))

    monkeypatch.setattr(Job, "control", control)
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
    from orbit.run.providers.slurm.job import StepReady
    from orbit.run.providers.slurm.provider import _group_variables

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
async def test_each_nodes_daemon_gets_its_variables_and_its_relays_while_sealed():
    from orbit.run.providers.slurm.provider import SlurmSandbox

    written = {}

    class Client:
        def __init__(self, name):
            self.name = name

        async def write_file(self, path, data):
            written[(self.name, path)] = json.loads(data)

    fake = SimpleNamespace(
        nodes=("n0", "n1"),
        _clients=(Client("n0"), Client("n1")),
        _node_variables=({"ORBIT_NODE_RANK": "0"}, {"ORBIT_NODE_RANK": "1"}),
        _peers=("node0", "node1"),
    )
    await SlurmSandbox._peer_variables(fake, ["127.0.0.1:1", "127.0.0.1:2"])
    sealed = written[("n1", worker.PEERS_ENV)]
    assert sealed["ORBIT_NODE_RANK"] == "1" and sealed["HTTP_PROXY"] == "http://127.0.0.1:2"
    await SlurmSandbox._peer_variables(fake, [None, None])
    assert written[("n1", worker.PEERS_ENV)] == {"ORBIT_NODE_RANK": "1"}


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
        await provider()._boot(_snapshot_request("slurm:orbit-old/1"), ())


@pytest.mark.asyncio
async def test_a_job_goes_to_the_account_and_the_partitions_constraint(monkeypatch, tmp_path):
    from orbit.core.config import SlurmPartition, SlurmProviderConfig
    from orbit.run.providers.slurm import job as job_module

    submitted = []

    async def slurm(*argv, **_):
        submitted.append(argv)
        return "4242;cluster"

    monkeypatch.setattr(job_module, "slurm", slurm)
    part = SlurmPartition(name="gpu", qos="short", max_cpus=8, max_memory_mb=8192, constraint="ib")
    config = SlurmProviderConfig(
        partitions=(part,), account="project-a", shared_fs_dir=str(tmp_path), node_local_dir="/l/$U"
    )
    job = Job(config, part)
    job.stage({"seeds": []})
    assert json.loads((job.directory / "allocation.json").read_text())["node_local_dir"] == "/l/$U"
    await job.submit(cpus=2, memory_mb=1024, gpus=0, minutes=10)
    command = submitted[0][-1]
    assert "--account=project-a" in command and "--constraint=ib" in command
    assert "--qos=short" in command and job.id == "4242"
    bare = part.model_copy(update={"constraint": None})
    plain = Job(SlurmProviderConfig(partitions=(bare,), shared_fs_dir=str(tmp_path)), bare)
    plain.stage({"seeds": []})
    submitted.clear()
    await plain.submit(cpus=2, memory_mb=1024, gpus=0, minutes=10)
    assert "--account" not in submitted[0][-1] and "--constraint" not in submitted[0][-1]


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
