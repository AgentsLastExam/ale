"""Slurm selection must find one feasible allocation on one compute node."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ale.core.capability import SandboxNeeds
from ale.core.config import RunConfig
from ale.core.errors import ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    ImageKind,
    PreparedTaskImage,
    SandboxRequest,
    SandboxRole,
    snapshot_verifier_needs,
    verifiers_needs,
)
from ale.core.taskspec import NetworkMode, NetworkPolicy, OperatingSystem, Resources
from ale.run.oci import Reference, api, qualified
from ale.run.providers import ProviderRegistry
from ale.run.providers.slurm import images, worker
from ale.run.providers.slurm.job import CACHE, Job, JobReady
from ale.run.providers.slurm.job import seconds as job_seconds
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
        dict(name="cpu", max_cpus=64, max_memory_mb=32768),
        dict(
            name="small",
            max_cpus=4,
            max_memory_mb=16384,
            max_gpus=2,
            gpu_model=dict(name="NVIDIA H100 80GB HBM3", memory_mb=81559),
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
    assert _capacity(solver) == (3, step_memory_mb(6144), 0)
    assert _capacity(solver, Resources(cpus=1, memory_mb=1024, gpus=1)) == (
        3,
        step_memory_mb(6144),
        1,
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
    held.reserve(Resources(cpus=1, memory_mb=1024))
    assert not held.idle


def test_a_snapshot_handle_names_its_allocation_and_sandbox():
    held = chain()
    assert _parse(_handle(held.job, "3")) == (held.job.name, "3")
    for wrong in ("slurm:ale-1", "slurm:x/1", "slurm:ale-1/x", "docker:ale-1/1"):
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

    async def allocate(needs, *, image, archive, layers=None):
        allocated.append((image, archive, layers))
        return chain(cpus=8, memory_mb=step_memory_mb(8192), gpus=1)

    async def load(chain, image):
        loaded.append((chain, image.prepared_identity))
        return image.prepared_identity

    monkeypatch.setattr(slurm, "write_out", write_out)
    monkeypatch.setattr(slurm, "_allocate", allocate)
    monkeypatch.setattr(slurm, "_load", load)
    # Both images were prepared for the cluster.
    monkeypatch.setattr(images, "recorded", lambda image: {"user": "user"})
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
    assert (booted, index, resolved.observed_identity, seed) == (held, "1", SAVED, SAVED)
    assert specs == view and held.viewing["1"] == {"x.1.0"}
    assert not written and not allocated
    # A verifier of the verifier's image views the volumes in the same allocation, which
    # loads that image.
    held.running.clear()
    viewer = request(Resources(cpus=1, memory_mb=1024)).model_copy(
        update={"snapshot": None, "volumes_from": handle, "prepared_image": own}
    )
    booted, index, resolved, seed, specs = await slurm._boot(viewer, ())
    assert (booted, seed, specs) == (held, OTHER, view) and loaded == [(held, OTHER)]
    booted, index, _, _, _ = await slurm._boot(request(Resources(cpus=8, gpus=1)), ())
    assert booted is not held and written == [snapshot]
    assert allocated == [(SAVED, "snapshots/out.tar.gz", {"x.1.0": "snapshots/out.x.1.0.tar"})]
    # A viewer that does not fit either loads its own image in an allocation of its own.
    viewer = viewer.model_copy(update={"resources": Resources(cpus=8, gpus=1)})
    await slurm._boot(viewer, ())
    assert allocated[-1] == (OTHER, "images/verifier.tar.gz", {"x.1.0": "snapshots/out.x.1.0.tar"})
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
    assert first.runtime_ref == images.archive(PREPARED) and images.recorded(first)
    again, reused = await images.prepare(config, recipe)
    assert reused and again == first and prepared == [PREPARED]
    (tmp_path / CACHE / first.runtime_ref).unlink()
    assert not images.recorded(first)
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
        image=SAVED,
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
        saved.archive = "snapshots/out.tar.gz"
        return saved.archive

    async def stop(job):
        stopped.append(job.name)

    async def allocate(needs, *, image, archive, layers=None):
        allocated.append((image, archive))
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
    assert booted is not held and allocated == [(SAVED, "snapshots/out.tar.gz")]
    assert held.closed and stopped == [held.job.name] and handle not in slurm.snapshots


def _saved(held, *, boots=0):
    snapshot = _Snapshot(
        index="1",
        image=SAVED,
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
        saved.archive = "snapshots/out.tar.gz"
        return saved.archive

    async def stop(job, **_):
        stopped.append(job.name)

    async def allocate(needs, *, image, archive, layers=None):
        allocated.append((image, archive))
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
    return SandboxRequest(
        episode_id="e",
        prepared_image=image,
        snapshot=handle,
        resources=Resources(cpus=2, memory_mb=4096, gpus=1),
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
    assert booted is not held and allocated == [(SAVED, "snapshots/out.tar.gz")]
    # It still lasts: it is kept for what may boot there yet.
    assert not held.closed


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
    assert booted is not held and allocated == [(SAVED, "snapshots/out.tar.gz")]
    assert held.closed and stopped == [held.job.name] and snapshot.boots == 0
    assert handle not in slurm.snapshots


@pytest.mark.asyncio
async def test_a_snapshot_of_an_allocation_slurm_ended_is_destroyed_with_its_files(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    slurm = provider()
    held = chain()
    snapshot = _saved(held)
    handle = _handle(held.job, "1")
    slurm.snapshots[handle] = (held, snapshot)
    files = tmp_path / CACHE / "snapshots"
    files.mkdir(parents=True)
    for name in ("tar.gz", "json", "x.1.0.tar"):
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
    from ale.core.errors import AllocationEndedError, GuestUnreachableError
    from ale.run.providers.slurm import job as job_module

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
        job_module.SandboxSteps(held.job, "1"),
        job_module.StepReady(image="i", devices=(), step="0"),
        Watcher(),
    )
    explained = await transport.explain(GuestUnreachableError("guest went away"))
    assert isinstance(explained, AllocationEndedError) and "PREEMPTED" in str(explained)


@pytest.mark.asyncio
async def test_an_allocations_deadline_is_slurms_own_count_less_its_drain(monkeypatch):
    slurm = provider()

    async def nothing(job, *args, **_):
        return None

    async def ready(job, timeout):
        return JobReady(runtime="runsc", node="n")

    async def left(job):
        return 3600

    monkeypatch.setattr(Job, "stage", lambda job, spec, **_: None)
    monkeypatch.setattr(Job, "submit", nothing)
    monkeypatch.setattr(Job, "ready", ready)
    monkeypatch.setattr(Job, "time_left", left)
    before = datetime.now(UTC)
    held = await slurm._allocate(needs(Resources(cpus=2)), image=SAVED, archive="images/x.tar.gz")
    drain = timedelta(minutes=slurm.config.drain_minutes)
    assert held.deadline is not None
    assert before + timedelta(seconds=3600) - drain <= held.deadline
    assert held.deadline <= datetime.now(UTC) + timedelta(seconds=3600) - drain
