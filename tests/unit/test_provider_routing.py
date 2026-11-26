from __future__ import annotations

import importlib
import json
import platform
import subprocess
import sys
from collections.abc import Iterable, Sequence
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support import MemoryProvider, edit_task_config, write_task
from typer.testing import CliRunner

from orbit.core.capability import CapabilityProfile, SandboxNeeds
from orbit.core.config import RunConfig, SandboxRetentionConfig, load_run_config
from orbit.core.errors import ProviderCapabilityError, ProviderStartError
from orbit.core.sandbox import (
    DesktopResolution,
    ExecResult,
    GpuDevice,
    ImageKind,
    PreparedTaskImage,
    Sandbox,
    SandboxCheck,
    SandboxRequest,
    SandboxRole,
)
from orbit.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, Resources
from orbit.run import capprobe
from orbit.run.cli.main import _destroy_retained_sandbox, _list_retained_sandboxes, app
from orbit.run.episode import _Lease
from orbit.run.providers import ProviderRegistry, missing_features
from orbit.run.providers.desktop import require_resolution
from orbit.run.providers.docker import DockerProvider
from orbit.run.providers.qemu import QemuProvider
from orbit.run.scaffold import scaffold_task
from orbit.run.task_images import ImagePreparationResult, ImagePreparationStep
from orbit.run.tasksets.manifest import ManifestTask, load_task_folder

pytestmark = pytest.mark.unit
MACOS_IMAGE = "ghcr.io/example/macos@sha256:" + "a" * 64


def _settings(*providers: str, **sandbox: Any) -> RunConfig:
    return RunConfig.model_validate({"sandbox": {"providers": list(providers), **sandbox}})


def _task(path: Path, config: dict[str, Any] | None = None, **kwargs: Any) -> ManifestTask:
    return load_task_folder(
        write_task(path, name=f"agentslastexam/{path.name}", config=config, **kwargs)
    )


# --- configuration ---


def test_a_run_names_its_providers_in_order_by_their_ids() -> None:
    assert RunConfig().sandbox.providers == ("docker:runc", "qemu", "orchard")
    refused = {
        "names no Provider": [],
        "names qemu more than once": ["qemu", "docker:runc", "qemu"],
        "docker:kata": ["docker:runsc"],
    }
    for reason, providers in refused.items():
        with pytest.raises(ValidationError, match=reason):
            _settings(*providers)
    with pytest.raises(ValidationError, match="names no Docker leaf"):
        _settings("qemu", docker={"gpus": [0]})
    with pytest.raises(ValidationError, match="unique"):
        _settings("docker:runc", docker={"gpus": [0, 0]})
    with pytest.raises(ValidationError, match=r"sandbox\.fleet\.url"):
        _settings("docker:runc", "fleet")
    for removed in ({"container": {"provider": "docker"}}, {"vm": {}}, {"fleet": {}}):
        with pytest.raises(ValidationError):
            RunConfig.model_validate(removed)
    # An override replaces the whole list, in its own order.
    overridden = load_run_config(overrides=['sandbox.providers=["docker:kata", "qemu"]'])
    assert overridden.sandbox.providers == ("docker:kata", "qemu")


def test_the_registry_builds_each_provider_by_its_id_when_first_asked() -> None:
    registry = ProviderRegistry(_settings("docker:kata", "docker:runc", docker={"gpus": [1]}))
    assert registry._instances == {}
    kata = registry.get("docker:kata")
    assert isinstance(kata, DockerProvider)
    assert (kata.id, kata.name, kata.runtime, kata.gpus) == ("docker:kata", "docker", "kata", (1,))
    assert registry.get("docker:kata") is kata
    # One the list does not name, as a Sandbox it kept is reached through it.
    assert isinstance(registry.get("qemu"), QemuProvider)
    with pytest.raises(ProviderCapabilityError, match="names no leaf"):
        registry.get("docker:runsc")


def test_orbit_run_has_no_provider_flag() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--provider" not in result.output


# --- selection ---


@pytest.mark.asyncio
async def test_each_sandbox_goes_to_the_first_provider_that_serves_it(tmp_path: Path) -> None:
    plain = _task(tmp_path / "plain")
    perf = _task(tmp_path / "perf", {"environment": {"container_features": ["perf"]}})
    # A separate verifier is matched by its own needs, here without the solver's mount.
    checked = _task(
        tmp_path / "checked",
        {
            "environment": {"container_features": ["mount"]},
            "verifier": {"environment_mode": "separate", "environment": {"cpus": 1}},
        },
    )
    machine = _task(
        tmp_path / "machine", {"environment": {"image": "/images/linux.qcow2"}}, dockerfile=None
    )
    mac = _task(
        tmp_path / "mac",
        {
            "environment": {"os": "macos", "image": MACOS_IMAGE},
            "agent": {"user": "admin", "sudo": True, "network_mode": "public"},
        },
        dockerfile=None,
    )
    tasks = [plain, perf, checked, machine, mac]
    await ProviderRegistry(_settings("docker:runc", "docker:kata", "qemu", "orchard")).select(tasks)
    assert [(task.provider, task.verifier_provider) for task in tasks] == [
        ("docker:runc", None),
        ("docker:kata", None),
        ("docker:kata", "docker:runc"),
        ("qemu", None),
        ("orchard", None),
    ]
    # Of two that serve a Sandbox, the one named first takes it, whatever either's load.
    await ProviderRegistry(_settings("docker:kata", "docker:runc")).select([plain])
    assert plain.provider == "docker:kata"


@pytest.mark.asyncio
async def test_a_run_says_which_sandboxes_that_share_a_gpu_are_only_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docker:runc holds no process to its share of a GPU, which the Run says once at its
    start; docker:gvisor limits it, and a Sandbox that takes its GPUs whole shares nothing."""

    async def host_gpus(_provider: DockerProvider) -> dict[int, GpuDevice]:
        h100 = GpuDevice(
            id="GPU-0",
            model="NVIDIA H100 80GB HBM3",
            driver_version="570.1",
            memory_total_mb=81559,
            memory_reserved_mb=470,
        )
        return {0: h100}

    monkeypatch.setattr(DockerProvider, "_host_gpus", host_gpus)
    shared = _task(
        tmp_path / "shared",
        {
            "environment": {"gpus": 1, "gpu_memory_mb": 8192},
            "verifier": {"environment": {"gpus": 1, "gpu_memory_mb": 4096}},
        },
    )
    whole = _task(tmp_path / "whole", {"environment": {"gpus": 1}})
    notices = await ProviderRegistry(_settings("docker:runc", "docker:gvisor")).select(
        [shared, whole]
    )
    # One line for the leaf, counting its solver and its verifier.
    assert notices == (
        "docker:runc watches the gpu_memory_mb of 2 Sandboxes rather than enforcing it: a "
        "process past its share is killed",
    )
    assert (
        await ProviderRegistry(_settings("docker:gvisor", "docker:runc")).select([shared, whole])
        == ()
    )
    # More than any GPU here has is refused before anything starts.
    big = _task(tmp_path / "big", {"environment": {"gpus": 1, "gpu_memory_mb": 90000}})
    with pytest.raises(ProviderCapabilityError, match=r"\(76993 MiB\), none with gpu_memory_mb"):
        await ProviderRegistry(_settings("docker:runc")).select([big])


@pytest.mark.asyncio
async def test_a_run_any_sandbox_of_which_no_provider_serves_is_refused_whole(
    tmp_path: Path,
) -> None:
    tasks = [
        _task(tmp_path / "plain"),
        _task(tmp_path / "perf", {"environment": {"container_features": ["perf"]}}),
        _task(
            tmp_path / "fuse",
            {
                "verifier": {
                    "environment_mode": "separate",
                    "environment": {"cpus": 1, "container_features": ["fuse"]},
                }
            },
        ),
    ]
    registry = ProviderRegistry(_settings("docker:runc", "qemu"))
    with pytest.raises(ProviderCapabilityError) as refused:
        await registry.select(tasks)
    assert str(refused.value) == (
        "no Provider in sandbox.providers (docker:runc, qemu) serves these 2 Sandboxes:\n"
        "  agentslastexam/perf solver:\n"
        "    docker:runc: gives no container features perf (gives raw_sockets)\n"
        "    qemu: starts vm Sandboxes, not container; "
        "gives no container features perf (gives none)\n"
        "  agentslastexam/fuse verifier:\n"
        "    docker:runc: gives no container features fuse (gives raw_sockets)\n"
        "    qemu: starts vm Sandboxes, not container; "
        "gives no container features fuse (gives none)"
    )


@pytest.mark.asyncio
async def test_a_sandbox_that_names_storage_mb_goes_to_a_leaf_that_holds_it_to_that(
    tmp_path: Path,
) -> None:
    """Every Docker leaf measures what a Sandbox writes, into its volumes too, a container
    engine's storage among them."""
    sized = _task(tmp_path / "sized", {"environment": {"storage_mb": 4096}})
    leaves = ("docker:runc", "docker:kata", "docker:gvisor")
    for leaf in leaves:
        assert await ProviderRegistry(_settings(leaf, "firecracker")).select([sized]) == ()
        assert sized.provider == leaf
    engine = {"storage_mb": 4096, "memory_mb": 4096, "container_features": ["nested_containers"]}
    nested = _task(tmp_path / "nested", {"environment": engine})
    await ProviderRegistry(_settings("docker:gvisor", "daytona")).select([nested])
    assert nested.provider == "docker:gvisor"


@pytest.mark.asyncio
async def test_a_provider_refusing_by_its_own_rules_passes_the_sandbox_on(
    tmp_path: Path,
) -> None:
    """Daytona allocates whole GiB: what it would refuse at create it refuses here, and the
    next listed Provider takes the Sandbox."""
    task = _task(tmp_path / "odd", {"environment": {"memory_mb": 1536}})
    await ProviderRegistry(_settings("daytona", "docker:runc")).select([task])
    assert task.provider == "docker:runc"
    with pytest.raises(ProviderCapabilityError, match="memory_mb=1536 is not a whole number"):
        await ProviderRegistry(_settings("daytona")).select([task])


@pytest.mark.asyncio
async def test_a_solver_that_saves_or_boots_a_snapshot_goes_where_snapshots_are(
    tmp_path: Path,
) -> None:
    task = _task(tmp_path / "kept", {"environment": {"memory_mb": 2048}})
    registry = ProviderRegistry(_settings("daytona", "docker:runc"))
    await registry.select([task])
    assert task.provider == "daytona"
    await registry.select([task], snapshots=True)
    assert task.provider == "docker:runc"


@pytest.mark.asyncio
async def test_a_verifier_is_matched_with_the_open_network_it_runs_with(
    tmp_path: Path,
) -> None:
    class OpenOnly(MemoryProvider):
        async def profiles(self) -> tuple[CapabilityProfile, ...]:
            (profile,) = await super().profiles()
            return (profile.model_copy(update={"network_modes": frozenset({NetworkMode.OPEN})}),)

    task = _task(
        tmp_path / "sealed",
        {"verifier": {"environment_mode": "separate", "environment": {"cpus": 1}}},
    )
    registry = ProviderRegistry(_settings("daytona", "docker:runc"))
    registry._instances["daytona"] = OpenOnly("daytona")
    await registry.select([task])
    assert (task.provider, task.verifier_provider) == ("docker:runc", "daytona")


@pytest.mark.asyncio
async def test_a_refusal_names_only_what_each_provider_lacks_for_that_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchard's advice on setting up a Mac is for macOS Tasks only."""

    def missing() -> None:
        raise ProviderCapabilityError("restricted macOS networking needs Softnet")

    monkeypatch.setattr("orbit.run.providers.orchard.provider.check_softnet", missing)
    task = _task(tmp_path / "perf", {"environment": {"container_features": ["perf"]}})
    with pytest.raises(ProviderCapabilityError) as refused:
        await ProviderRegistry(RunConfig()).select([task])
    assert str(refused.value).endswith(
        "    orchard: starts vm Sandboxes, not container; runs macos, not linux; "
        "runs arm64, not x86_64; network block is not one it applies (open); "
        "gives no container features perf (gives none)"
    )


@pytest.mark.asyncio
async def test_a_provider_is_asked_only_when_those_before_it_serve_no_sandbox(
    tmp_path: Path,
) -> None:
    """A scheduler that a Run reaches over the network is not built for Tasks a leaf
    serves."""
    registry = ProviderRegistry(
        _settings("docker:runc", "fleet", fleet={"url": "http://control.invalid:7470"})
    )
    await registry.select([_task(tmp_path / "plain")])
    assert set(registry._instances) == {"docker:runc"}


@pytest.mark.asyncio
async def test_reverification_selects_only_its_verifiers(tmp_path: Path) -> None:
    task = _task(
        tmp_path / "kept",
        {
            "environment": {"container_features": ["perf"]},
            "verifier": {"environment_mode": "separate", "environment": {"cpus": 1}},
        },
    )
    await ProviderRegistry(_settings("docker:runc")).select([task], solvers=False)
    assert (task.provider, task.verifier_provider) == (None, "docker:runc")


def test_a_refused_selection_stops_the_command_before_anything_is_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = scaffold_task(tmp_path / "perf")
    edit_task_config(folder, {"environment": {"container_features": ["perf"]}})

    async def never(*_args: object) -> None:
        raise AssertionError("an image was prepared")

    monkeypatch.setattr("orbit.run.cli.tasks.prepare_task_image_result", never)
    result = CliRunner().invoke(
        app, ["prepare", str(folder), "--set", 'sandbox.providers=["docker:runc"]']
    )
    assert result.exit_code == 3, result.output
    assert "no Provider in sandbox.providers (docker:runc) serves this Sandbox" in result.output
    assert "docker:runc: gives no container features perf" in result.output


# --- preflight ---


class _Leaf(MemoryProvider):
    """A leaf giving ``features``, which keeps the needs each of its preflights was given,
    and whose preflight returns ``check``."""

    def __init__(self, provider_id: str, *features: str, check: SandboxCheck | None = None) -> None:
        super().__init__(provider_id, container_features=frozenset(map(ContainerFeature, features)))
        self.preflights: list[list[SandboxNeeds]] = []
        self.check = check

    async def preflight(self, needs: Sequence[SandboxNeeds] = ()) -> SandboxCheck | None:
        self.preflights.append(list(needs))
        return self.check


def _selected(task: ManifestTask, solver: str, verifier: str | None = None) -> ManifestTask:
    """``task`` as selection and preparation leave it."""
    task.provider, task.verifier_provider = solver, verifier
    task.prepared_image = _request(ImageKind.CONTAINER).prepared_image
    task.prepared_verifier_image = task.prepared_image if verifier else None
    return task


@pytest.mark.asyncio
async def test_each_selected_leaf_is_checked_once_for_each_set_of_features_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runc = _Leaf("docker:runc", "raw_sockets")
    kata = _Leaf("docker:kata", "perf", "mount", "raw_sockets")
    fleet = _Leaf("fleet", "perf")
    registry = ProviderRegistry(
        _settings("docker:runc", "docker:kata", "fleet", fleet={"url": "http://control.invalid"})
    )
    registry._instances.update({leaf.id: leaf for leaf in (runc, kata, fleet)})
    tasks = [
        _selected(_task(tmp_path / "plain"), "docker:runc"),
        _selected(
            _task(tmp_path / "perf", {"environment": {"container_features": ["perf"], "gpus": 1}}),
            "docker:kata",
        ),
        _selected(
            _task(tmp_path / "again", {"environment": {"container_features": ["perf"]}}),
            "docker:kata",
        ),
        _selected(
            _task(
                tmp_path / "checked",
                {
                    "environment": {"container_features": ["mount", "perf"]},
                    "verifier": {"environment_mode": "separate", "environment": {"cpus": 1}},
                },
            ),
            "docker:kata",
            "docker:runc",
        ),
        # A scheduler's leaves are on machines it manages.
        _selected(
            _task(tmp_path / "remote", {"environment": {"container_features": ["perf"]}}), "fleet"
        ),
    ]
    probed: list[tuple[SandboxRequest, set[ContainerFeature]]] = []

    async def probe(sandbox: Sandbox, features: Iterable[ContainerFeature]) -> dict[str, str]:
        probed.append((sandbox.request, set(features)))
        return {}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    await registry.preflight(tasks)
    await registry.preflight(tasks)

    assert [len(leaf.preflights) for leaf in (runc, kata, fleet)] == [1, 1, 0]
    # Each with the needs of the Sandboxes it was selected for.
    ((plain, verifier),) = runc.preflights
    assert (plain.resources.container_features, verifier.network_mode) == ((), NetworkMode.OPEN)
    assert [need.resources.container_features for need in kata.preflights[0]] == [
        (ContainerFeature.PERF,),
        (ContainerFeature.PERF,),
        (ContainerFeature.MOUNT, ContainerFeature.PERF),
    ]
    assert runc.requests == fleet.requests == []
    # Each set as its Sandboxes ask for it, with its grants and no other's.
    (perf, perf_wanted), (both, both_wanted) = probed
    assert perf_wanted == {ContainerFeature.PERF}
    assert both_wanted == {ContainerFeature.PERF, ContainerFeature.MOUNT}
    assert perf.resources.container_features == (ContainerFeature.PERF,)
    assert both.resources.container_features == (ContainerFeature.MOUNT, ContainerFeature.PERF)
    # Made as the first Sandbox that asks for its set, the perf Task's, but with no GPU.
    assert perf.role is SandboxRole.SHARED
    assert perf.network.mode is NetworkMode.BLOCK
    assert perf.resources.gpus == 0
    assert kata.destroyed == [sandbox.sandbox_id for sandbox in kata.sandboxes]


@pytest.mark.asyncio
async def test_feature_probe_keeps_gpus_when_the_leaf_requires_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class GpuOnlyLeaf(_Leaf):
        def refusals(self, needs, *, user, role):
            return () if needs.resources.gpus else ("this partition requires a GPU",)

    leaf = GpuOnlyLeaf("slurm", "mount")
    registry = ProviderRegistry(_settings("docker:runc"))
    registry._instances["slurm"] = leaf
    task = _selected(
        _task(tmp_path / "gpu", {"environment": {"gpus": 2, "container_features": ["mount"]}}),
        "slurm",
    )

    async def probe(sandbox, features):
        assert sandbox.request.resources.gpus == 2
        return {}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    await registry.preflight([task])
    assert len(leaf.requests) == 1


@pytest.mark.asyncio
async def test_a_check_the_preflight_of_a_leaf_returns_runs_once_in_a_sandbox_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the Sandbox that probes its features when one does, else in one started as its
    first Sandbox asks, with no GPU."""
    checked: list[SandboxRequest] = []

    async def check(sandbox: Sandbox) -> None:
        checked.append(sandbox.request)

    probed = _Leaf("docker:gvisor", "mount", check=check)
    plain = _Leaf("docker:runc", check=check)
    registry = ProviderRegistry(_settings("docker:gvisor", "docker:runc"))
    registry._instances.update({leaf.id: leaf for leaf in (probed, plain)})
    tasks = [
        _selected(_task(tmp_path / "plain-on-gvisor"), "docker:gvisor"),
        _selected(
            _task(tmp_path / "mount", {"environment": {"container_features": ["mount"]}}),
            "docker:gvisor",
        ),
        _selected(_task(tmp_path / "gpu", {"environment": {"gpus": 1}}), "docker:runc"),
    ]

    async def probe(_sandbox: Sandbox, _features: object) -> dict[ContainerFeature, str]:
        return {}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    await registry.preflight(tasks)
    await registry.preflight(tasks)

    (in_probe, alone) = checked
    assert [request.resources.container_features for request in probed.requests] == [
        (ContainerFeature.MOUNT,)
    ]
    assert in_probe is probed.requests[0]
    assert alone is plain.requests[0] and alone.resources.gpus == 0
    assert alone.network.mode is NetworkMode.BLOCK
    assert len(probed.destroyed) == len(plain.destroyed) == 1


@pytest.mark.asyncio
async def test_the_preflight_of_a_leaf_runs_again_only_for_needs_it_has_not_passed_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later Task may need what the first did not, another runtime or a GPU; the check
    its preflight leaves runs once."""
    checked: list[SandboxRequest] = []

    async def check(sandbox: Sandbox) -> None:
        checked.append(sandbox.request)

    gvisor = _Leaf("docker:gvisor", "raw_sockets", check=check)
    registry = ProviderRegistry(_settings("docker:gvisor"))
    registry._instances[gvisor.id] = gvisor
    plain = _selected(_task(tmp_path / "plain"), "docker:gvisor")
    raw = _selected(
        _task(tmp_path / "raw", {"environment": {"container_features": ["raw_sockets"]}}),
        "docker:gvisor",
    )
    gpu = _selected(_task(tmp_path / "gpu", {"environment": {"gpus": 1}}), "docker:gvisor")

    async def probe(_sandbox: Sandbox, _features: object) -> dict[ContainerFeature, str]:
        return {}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    for tasks in ([plain], [plain], [plain, raw], [raw, gpu]):
        await registry.preflight(tasks)
    asked = [
        [(need.resources.container_features, need.resources.gpus) for need in needs]
        for needs in gvisor.preflights
    ]
    assert asked == [[((), 0)], [((ContainerFeature.RAW_SOCKETS,), 0)], [((), 1)]]
    assert len(checked) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["check", "probe"])
async def test_needs_whose_check_or_probe_failed_are_preflighted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A registry used again, as a Fleet Node's is, runs the leaf's preflight again rather
    than take needs whose check or probe failed for passed, and the check until it
    passes."""
    checked: list[SandboxRequest] = []

    async def check(sandbox: Sandbox) -> None:
        checked.append(sandbox.request)
        if failing == "check" and len(checked) == 1:
            raise ProviderStartError("Docker does not see what the sandbox writes")

    gvisor = _Leaf("docker:gvisor", "mount", check=check)
    registry = ProviderRegistry(_settings("docker:gvisor"))
    registry._instances[gvisor.id] = gvisor
    task = _selected(
        _task(tmp_path / "mount", {"environment": {"container_features": ["mount"]}}),
        "docker:gvisor",
    )
    probes: list[object] = []

    async def probe(_sandbox: Sandbox, _features: object) -> dict[ContainerFeature, str]:
        probes.append(_features)
        if failing == "probe" and len(probes) == 1:
            return {ContainerFeature.MOUNT: "mount EPERM"}
        return {}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    with pytest.raises((ProviderStartError, ProviderCapabilityError)):
        await registry.preflight([task])
    await registry.preflight([task])
    await registry.preflight([task])
    assert len(gvisor.preflights) == 2
    assert len(checked) == (2 if failing == "check" else 1)
    assert len(probes) == (1 if failing == "check" else 2)


@pytest.mark.asyncio
async def test_a_feature_a_leaf_lacks_on_this_host_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kata = _Leaf("docker:kata", "perf", "mount")
    registry = ProviderRegistry(_settings("docker:kata"))
    registry._instances[kata.id] = kata
    task = _task(tmp_path / "perf", {"environment": {"container_features": ["perf", "mount"]}})

    async def probe(_sandbox: Sandbox, _features: object) -> dict[ContainerFeature, str]:
        return {ContainerFeature.PERF: "perf_event_open EACCES"}

    monkeypatch.setattr("orbit.run.providers.missing_features", probe)
    with pytest.raises(ProviderCapabilityError) as refused:
        await registry.preflight([_selected(task, "docker:kata")])
    assert str(refused.value) == (
        "on this Host, a Sandbox of each leaf below lacks container features its Tasks need:\n"
        "  docker:kata: perf (perf_event_open EACCES)"
    )
    assert kata.destroyed == [kata.sandboxes[0].sandbox_id]


def test_orbit_run_refuses_to_start_when_a_leaf_lacks_a_feature_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = scaffold_task(tmp_path / "perf")
    edit_task_config(folder, {"environment": {"container_features": ["perf"]}})
    kata = _Leaf("docker:kata", "perf")
    image = _request(ImageKind.CONTAINER).prepared_image

    async def prepare(task: ManifestTask, _provider: object) -> ImagePreparationResult:
        return ImagePreparationResult(
            task=str(task.spec.name), role="solver", image=image, steps=()
        )

    async def lacking(_sandbox: Sandbox, _features: object) -> dict[ContainerFeature, str]:
        return {ContainerFeature.PERF: "perf_event_open EACCES"}

    async def episode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an episode started")

    monkeypatch.setattr("orbit.run.providers.leaf", lambda _id, **settings: kata)
    monkeypatch.setattr("orbit.run.cli.tasks.prepare_task_image_result", prepare)
    monkeypatch.setattr("orbit.run.providers.missing_features", lacking)
    monkeypatch.setattr("orbit.run.cli.tasks.run_episode", episode)
    result = CliRunner().invoke(
        app,
        ["run", str(folder), "--agent", "oracle", "--set", 'sandbox.providers=["docker:kata"]'],
    )
    assert result.exit_code == 3, result.output
    assert "docker:kata: perf (perf_event_open EACCES)" in result.output
    assert len(kata.preflights) == 1
    assert kata.destroyed == [kata.sandboxes[0].sandbox_id]


@pytest.mark.asyncio
async def test_the_probe_runs_as_root_on_the_frameworks_python_for_each_feature() -> None:
    class Probed:
        argv: tuple[str, ...] = ()

        async def exec(self, argv: list[str], *, timeout_sec: float) -> ExecResult:
            self.argv = tuple(argv)
            report = {
                "mount": {"mount_tmpfs": "ok", "mount_namespace": "EPERM"},
                "perf": {"perf_event_open": "ok"},
            }
            return ExecResult(exit_code=0, stdout=json.dumps(report))

    sandbox = Probed()
    wanted = {ContainerFeature.PERF, ContainerFeature.MOUNT, ContainerFeature.NESTED_CONTAINERS}
    missing = await missing_features(sandbox, wanted)  # type: ignore[arg-type]
    assert missing == {ContainerFeature.MOUNT: "mount_namespace EPERM"}
    assert sandbox.argv[:3] == ("/opt/orbit/python/bin/python3", "-I", "-c")
    # Starting a container is no syscall: conformance checks nested containers.
    assert sandbox.argv[4:] == ("mount", "perf")
    assert await missing_features(sandbox, [ContainerFeature.NESTED_CONTAINERS]) == {}  # type: ignore[arg-type]


def test_each_feature_is_shown_by_the_probes_of_what_it_promises() -> None:
    assert capprobe.FEATURES == {
        "nested_containers": (),
        "perf": ("perf_event_open",),
        "bpf": ("bpf_map",),
        "io_uring": ("io_uring",),
        "fuse": ("fuse_device",),
        "raw_sockets": ("raw_socket",),
        "user_namespaces": ("user_namespace",),
        # Mounting a file system, and in a mount namespace of its own.
        "mount": ("mount_tmpfs", "mount_namespace"),
    }
    assert set(capprobe.FEATURES) == set(ContainerFeature)


@pytest.mark.skipif(
    (sys.platform, platform.machine()) != ("linux", "x86_64"), reason="x86-64 Linux syscalls"
)
def test_the_probe_reports_each_probe_of_each_feature() -> None:
    source = Path(capprobe.__file__).read_text()
    result = subprocess.run(
        [sys.executable, "-I", "-c", source, *capprobe.FEATURES],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert {feature: set(outcomes) for feature, outcomes in report.items()} == {
        feature: set(probes) for feature, probes in capprobe.FEATURES.items()
    }
    outcomes = [outcome for probes in report.values() for outcome in probes.values()]
    assert all(outcome == "ok" or outcome.startswith("E") for outcome in outcomes), report


# --- leasing ---


def _request(kind: ImageKind, *, role: str = "solver") -> SandboxRequest:
    digest = "sha256:" + ("1" if kind is ImageKind.CONTAINER else "2") * 64
    prepared: dict[str, object] = {
        "kind": kind,
        "source": "external-ref",
        "input_identity": digest,
        "runtime_ref": "image@" + digest if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": digest,
        "resolved_reference": "registry/image@" + digest,
    }
    return SandboxRequest(
        episode_id="episode",
        role=role,
        prepared_image=PreparedTaskImage.model_validate(prepared),
        resources=Resources(),
        network=NetworkPolicy(),
    )


class _Sandbox:
    def __init__(self, name: str) -> None:
        self.sandbox_id = name
        self.destroyed = False

    async def destroy(self) -> None:
        self.destroyed = True

    def release_resources(self) -> None:
        pass


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[SandboxRequest] = []

    async def create(self, request: SandboxRequest) -> _Sandbox:
        self.requests.append(request)
        return _Sandbox(f"{self.name}-{len(self.requests)}")


@pytest.mark.asyncio
async def test_a_lease_takes_each_sandbox_from_the_provider_selected_for_its_role() -> None:
    """The selection routes a request, not its image's kind: both are containers here."""
    kata, runc = _Provider("docker"), _Provider("qemu")
    registry = SimpleNamespace(get={"docker:kata": kata, "docker:runc": runc}.__getitem__)
    lease = _Lease(
        registry,  # type: ignore[arg-type]
        SandboxRetentionConfig(),
        solver="docker:kata",
        verifier="docker:runc",
    )

    solver = await lease.acquire(_request(ImageKind.CONTAINER))
    verifier = await lease.acquire(_request(ImageKind.CONTAINER, role="verifier"))
    await lease.release(solver)  # type: ignore[arg-type]
    await lease.release(verifier)  # type: ignore[arg-type]

    assert [request.role for request in kata.requests] == [SandboxRole.SOLVER]
    assert [request.role for request in runc.requests] == [SandboxRole.VERIFIER]
    assert {outcome.provider for outcome in lease.outcomes} == {"docker", "qemu"}


@pytest.mark.asyncio
async def test_every_leased_sandbox_takes_the_runs_desktop_resolution() -> None:
    provider = _Provider("docker")
    registry = SimpleNamespace(get=lambda _: provider)
    resolution = DesktopResolution(width=1366, height=768)
    lease = _Lease(
        registry,  # type: ignore[arg-type]
        SandboxRetentionConfig(),
        resolution,
        solver="docker:runc",
        verifier="docker:runc",
    )

    await lease.acquire(_request(ImageKind.CONTAINER))
    await lease.acquire(_request(ImageKind.CONTAINER, role="verifier"))

    assert [request.desktop_resolution for request in provider.requests] == [resolution] * 2


@pytest.mark.asyncio
async def test_a_desktop_of_another_size_is_refused() -> None:
    class Client:
        async def screen_size(self) -> tuple[int, int]:
            return 1280, 800

    wanted = DesktopResolution(width=1280, height=800)
    assert await require_resolution(Client(), wanted) == wanted  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match="1280x800, not the requested 1024x768"):
        await require_resolution(Client(), DesktopResolution())  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match=r"Xvfb :1 -screen 0 \$\{VNC_RESOLUTION\}x24"):
        await require_resolution(Client(), DesktopResolution(), container=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_sandbox_without_a_selected_provider_is_refused_at_admission() -> None:
    class Registry:
        def get(self, provider_id: str) -> None:
            raise ProviderCapabilityError(f"{provider_id!r} names no leaf")

    lease = _Lease(Registry(), SandboxRetentionConfig(), solver="docker:gone")  # type: ignore[arg-type]
    with pytest.raises(ProviderCapabilityError, match="'docker:gone' names no leaf"):
        await lease.acquire(_request(ImageKind.CONTAINER))
    with pytest.raises(ProviderCapabilityError, match="no Provider was selected for a verifier"):
        await lease.acquire(_request(ImageKind.CONTAINER, role="verifier"))


@pytest.mark.asyncio
async def test_retained_sandbox_commands_route_by_the_provider_named_in_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("orbit.run.cli.main")
    destroyed: list[tuple[str, str]] = []

    class Retaining:
        def __init__(self, name: str, slurm: object = None) -> None:
            self.name = name
            self.slurm = slurm

        async def list_retained(self) -> list[dict[str, object]]:
            return [{"handle": f"{self.name}:z"}, {"handle": f"{self.name}:a"}]

        async def destroy_retained(self, handle: str) -> None:
            destroyed.append((self.name, handle))

    monkeypatch.setattr(cli, "provider_named", Retaining)

    assert [item["handle"] for item in await _list_retained_sandboxes()] == [
        "daytona:a",
        "daytona:z",
        "docker:a",
        "docker:z",
        "firecracker:a",
        "firecracker:z",
        "orchard:a",
        "orchard:z",
        "pbs:a",
        "pbs:z",
        "qemu:a",
        "qemu:z",
        "slurm:a",
        "slurm:z",
    ]
    await _destroy_retained_sandbox("docker:orbit-docker")
    await _destroy_retained_sandbox("firecracker:orbit-fc")
    await _destroy_retained_sandbox("qemu:orbit-qemu")
    await _destroy_retained_sandbox("daytona:abc")
    await _destroy_retained_sandbox("orchard:orbit-vm:uid")
    await _destroy_retained_sandbox("orchard:snapshot:abc")
    await _destroy_retained_sandbox("slurm:login/orbit-0/1")
    assert destroyed == [
        ("docker", "docker:orbit-docker"),
        ("firecracker", "firecracker:orbit-fc"),
        ("qemu", "qemu:orbit-qemu"),
        ("daytona", "daytona:abc"),
        ("orchard", "orchard:orbit-vm:uid"),
        ("orchard", "orchard:snapshot:abc"),
        ("slurm", "slurm:login/orbit-0/1"),
    ]
    with pytest.raises(ProviderCapabilityError, match="must start"):
        await _destroy_retained_sandbox("orbit-ambiguous")


@pytest.mark.asyncio
async def test_a_machine_without_docker_lists_none_of_its_sandboxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Mac, say, lists its Orchard VMs; the Docker-based Providers have nothing there."""
    import asyncio
    import shutil

    from orbit.run.providers import provider_named

    async def no_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a machine without Docker ran a process for its listing")

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    for name in ("docker", "qemu", "firecracker"):
        assert await provider_named(name).list_retained() == []


def test_prepare_uses_selection_and_starts_no_runtime_services(
    monkeypatch: pytest.MonkeyPatch,
    write_task_repo,  # type: ignore[no-untyped-def]
) -> None:
    repository = write_task_repo("ale-tasks-prepare", tasks=("b", "a"))
    task_cli = import_module("orbit.run.cli.tasks")
    observed: list[str] = []
    digest = "sha256:" + "a" * 64

    async def fake_prepare(tasks, _providers, _continued=None):  # type: ignore[no-untyped-def]
        observed.extend(str(task.spec.name) for task in tasks)
        for task in tasks:
            task.provider = "docker:runc"
        image = PreparedTaskImage(
            kind=ImageKind.CONTAINER,
            source="solver-local",
            input_identity=digest,
            image_source_identity=digest,
            runtime_ref="orbit-task:fixture",
            prepared_identity=digest,
        )
        return tuple(
            ImagePreparationResult(
                task=str(task.spec.name),
                role="solver",
                image=image,
                steps=(ImagePreparationStep("oci-build", "executed", digest),),
            )
            for task in tasks
        )

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("prepare started a runtime service")

    monkeypatch.setattr(task_cli, "_prepare_images", fake_prepare)
    for name in ("Gateway", "Ledger", "EpisodeProtocol"):
        monkeypatch.setattr(task_cli, name, forbidden)

    result = CliRunner().invoke(app, ["prepare", str(repository)])

    assert result.exit_code == 0, result.output
    assert observed == ["agentslastexam/a", "agentslastexam/b"]
    assert "agentslastexam/a solver provider=docker:runc kind=container source=local" in (
        result.output
    )
    assert "oci-build" in result.output


def test_prepare_accepts_only_config_and_set_options() -> None:
    result = CliRunner().invoke(app, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--set" in result.output
    for forbidden in ("--runs-dir", "--agent", "--model", "--episodes", "--build"):
        assert forbidden not in result.output


@pytest.mark.asyncio
async def test_retained_slurm_snapshots_are_found_in_the_cache_a_run_configuration_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from orbit.core.config import SandboxConfig, SlurmProviderConfig
    from orbit.run.providers import provider_named
    from orbit.run.providers.slurm import SlurmProvider

    named = provider_named(
        "slurm", SandboxConfig(slurm=SlurmProviderConfig(shared_fs_dir=str(tmp_path)))
    )
    assert isinstance(named, SlurmProvider) and named.config.shared_fs_dir == str(tmp_path)
    assert provider_named("slurm").config.shared_fs_dir == "~/.cache/orbit/slurm"  # type: ignore[attr-defined]


def test_batch_settings_survive_serialization_and_reject_unsupported_allocations() -> None:
    pbs = _settings(
        "pbs", pbs={"partitions": [{"name": "cpu", "max_cpus": 4, "max_memory_mb": 8192}]}
    )
    assert RunConfig.model_validate(pbs.model_dump()).sandbox.pbs == pbs.sandbox.pbs
    with pytest.raises(ValidationError, match="max_nodes"):
        _settings(
            "pbs",
            pbs={
                "partitions": [
                    {"name": "gpu", "max_cpus": 4, "max_memory_mb": 8192, "max_nodes": 2}
                ]
            },
        )
