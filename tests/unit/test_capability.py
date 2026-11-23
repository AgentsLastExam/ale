"""Capability profiles and the one matcher every Provider check and Fleet placement use."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.test_fleet_control import linux as linux_node
from tests.unit.test_fleet_control import mac as mac_node

from orbit.core.capability import Arch, CapabilityProfile, GpuModel, SandboxNeeds, unmet
from orbit.core.errors import ProviderCapabilityError
from orbit.core.fleet import NodeDeclaration, SandboxDemand
from orbit.core.sandbox import PreparedTaskImage, SandboxRequest, SandboxRole
from orbit.core.taskspec import (
    ContainerFeature,
    ImageKind,
    NetworkMode,
    NetworkPolicy,
    OperatingSystem,
    Resources,
)
from orbit.run.framework import fleet_framework_identity
from orbit.run.providers.daytona import DaytonaProvider
from orbit.run.providers.fleet import FleetProvider
from orbit.run.providers.orchard import OrchardProvider
from orbit.run.providers.orchard.client import CONTROL_ACCOUNT

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64
PROFILE = CapabilityProfile(
    provider="docker:runc",
    kind=ImageKind.CONTAINER,
    os=OperatingSystem.LINUX,
    arch=Arch.X86_64,
    gui=True,
    network_modes=frozenset({NetworkMode.BLOCK, NetworkMode.OPEN}),
    snapshot=True,
    gpus=True,
    max_cpus=8,
    max_memory_mb=16384,
    container_features=frozenset({ContainerFeature.RAW_SOCKETS}),
)


def needs(**overrides: object) -> SandboxNeeds:
    base: dict[str, object] = {
        "kind": ImageKind.CONTAINER,
        "os": OperatingSystem.LINUX,
        "arch": Arch.X86_64,
        "network_mode": NetworkMode.BLOCK,
        "resources": Resources(),
    }
    return SandboxNeeds.model_validate(base | overrides)


class TestUnmet:
    def test_a_profile_that_serves_every_need_has_nothing_unmet(self) -> None:
        served = needs(
            resources=Resources(
                cpus=8,
                memory_mb=16384,
                gpus=2,
                container_features=(ContainerFeature.RAW_SOCKETS,),
            ),
            snapshot=True,
        )
        assert unmet(PROFILE, served) == ()

    @pytest.mark.parametrize(
        ("asked", "reason"),
        [
            ({"kind": ImageKind.VM}, "starts container Sandboxes, not vm"),
            ({"os": OperatingSystem.WINDOWS}, "runs linux, not windows"),
            ({"arch": Arch.ARM64}, "runs x86_64, not arm64"),
            (
                {"network_mode": NetworkMode.ALLOWLIST},
                "network allowlist is not one it applies (block, open)",
            ),
            ({"resources": Resources(cpus=9)}, "9 CPUs exceed its 8"),
            (
                {"resources": Resources(memory_mb=16385)},
                "16385 MB of memory exceed its 16384 MB",
            ),
            (
                {
                    "resources": Resources(
                        container_features=(ContainerFeature.PERF, ContainerFeature.BPF)
                    )
                },
                "gives no container features bpf, perf (gives raw_sockets)",
            ),
        ],
    )
    def test_each_need_a_profile_misses_is_one_reason(
        self, asked: dict[str, object], reason: str
    ) -> None:
        assert unmet(PROFILE, needs(**asked)) == (reason,)

    def test_a_sandbox_that_names_storage_mb_needs_a_profile_that_limits_its_writes(self) -> None:
        """A limit nothing enforces lets a Sandbox fill the Host's disk unnoticed."""
        sized = needs(resources=Resources(storage_mb=4096))
        assert unmet(PROFILE, sized) == ("limits no writable storage (storage_mb 4096)",)
        assert unmet(PROFILE.model_copy(update={"storage_limit": True}), sized) == ()
        assert unmet(PROFILE, needs()) == ()

    def test_a_profile_without_snapshots_serves_no_sandbox_that_saves_or_boots_one(self) -> None:
        plain = PROFILE.model_copy(update={"snapshot": False})
        assert unmet(plain, needs(snapshot=True)) == ("saves and boots no snapshots",)
        assert unmet(plain, needs()) == ()

    def test_a_profile_that_saves_few_snapshots_in_a_row_serves_no_longer_chain(self) -> None:
        """A chain of saves, one booted from the snapshot before, as validation rounds make."""
        short = PROFILE.model_copy(update={"snapshot": True, "max_saves": 3})
        assert unmet(short, needs(snapshot=True, saves=3)) == ()
        assert unmet(short, needs(snapshot=True, saves=4)) == (
            "saves 4 snapshots in a row, more than its 3",
        )
        assert (
            unmet(PROFILE.model_copy(update={"snapshot": True}), needs(snapshot=True, saves=40))
            == ()
        )

    def test_a_gpu_needs_a_profile_that_attaches_one_of_an_accepted_model(self) -> None:
        gpu = needs(resources=Resources(gpus=1, gpu_types=("H100",)))
        assert unmet(PROFILE.model_copy(update={"gpus": False}), gpu) == ("attaches no GPUs",)
        # Models a profile learns only as it attaches them are checked then.
        assert unmet(PROFILE, gpu) == ()
        known = PROFILE.model_copy(update={"gpu_models": (GpuModel(name="NVIDIA A100-SXM4-80GB"),)})
        assert unmet(known, gpu) == ("attaches NVIDIA A100-SXM4-80GB, none of gpu_types ['H100']",)
        harbor = needs(resources=Resources(gpus=1, gpu_types=("H100", "a100-80gb")))
        assert unmet(known, harbor) == ()
        assert unmet(known, needs(resources=Resources(gpus=1))) == ()

    def test_a_gpu_memory_need_is_refused_only_where_no_accepted_model_has_that_much(
        self,
    ) -> None:
        """A whole-GPU leaf serves it with a GPU that has the memory; a model whose memory
        the leaf does not know may have it."""
        a100 = GpuModel(name="NVIDIA A100-SXM4-80GB", memory_mb=81920)
        l4 = GpuModel(name="NVIDIA L4", memory_mb=23034)
        known = PROFILE.model_copy(update={"gpu_models": (l4, a100)})
        assert unmet(known, needs(resources=Resources(gpus=1, gpu_memory_mb=81920))) == ()
        assert unmet(known, needs(resources=Resources(gpus=1, gpu_memory_mb=81921))) == (
            "attaches NVIDIA A100-SXM4-80GB (81920 MiB), NVIDIA L4 (23034 MiB), "
            "none with gpu_memory_mb 81921",
        )
        # Every model it accepts counts, and only those.
        l4_only = needs(resources=Resources(gpus=1, gpu_types=("L4",), gpu_memory_mb=40000))
        assert unmet(known, l4_only) == (
            "attaches NVIDIA A100-SXM4-80GB (81920 MiB), NVIDIA L4 (23034 MiB), "
            "none of gpu_types ['L4'] with gpu_memory_mb 40000",
        )
        unknown = PROFILE.model_copy(update={"gpu_models": (l4, GpuModel(name="NVIDIA H200"))})
        assert unmet(unknown, needs(resources=Resources(gpus=1, gpu_memory_mb=100000))) == ()

    def test_every_need_a_profile_misses_is_named(self) -> None:
        macos = needs(
            kind=ImageKind.VM,
            os=OperatingSystem.MACOS,
            arch=Arch.ARM64,
            network_mode=NetworkMode.ALLOWLIST,
            resources=Resources(cpus=16, memory_mb=32768, gpus=1),
            snapshot=True,
        )
        bare = PROFILE.model_copy(update={"gpus": False, "snapshot": False})
        assert len(unmet(bare, macos)) == 8


class TestNeeds:
    def test_a_tasks_sandbox_runs_arm64_for_macos_and_x86_64_otherwise(self) -> None:
        assert Arch.of(OperatingSystem.MACOS) is Arch.ARM64
        assert Arch.of(OperatingSystem.LINUX) is Arch.X86_64
        assert Arch.of(OperatingSystem.WINDOWS) is Arch.X86_64

    def test_the_fleet_asks_for_the_arch_a_tasks_os_runs(self) -> None:
        fleet = FleetProvider(SimpleNamespace())  # type: ignore[arg-type]
        image = PreparedTaskImage(
            kind=ImageKind.VM,
            source="external-ref",
            input_identity=DIGEST,
            runtime_ref="vm@" + DIGEST,
            prepared_identity=DIGEST,
            resolved_reference="vm@" + DIGEST,
        )
        arch = {
            system: fleet.plan_item(
                "t", image, system, NetworkPolicy(), Resources(), episodes=1
            ).demand.arch
            for system in OperatingSystem
        }
        assert arch == {system: Arch.of(system) for system in OperatingSystem}

    def test_a_request_and_its_fleet_demand_need_the_same(self) -> None:
        resources = Resources(gpus=1, container_features=(ContainerFeature.PERF,))
        request = SandboxRequest(
            episode_id="e1",
            prepared_image=PreparedTaskImage(
                kind=ImageKind.CONTAINER,
                source="external-ref",
                input_identity=DIGEST,
                runtime_ref="image@" + DIGEST,
                prepared_identity=DIGEST,
                resolved_reference="image@" + DIGEST,
            ),
            resources=resources,
            network=NetworkPolicy(mode=NetworkMode.OPEN),
            retention="snapshot",
        )
        demand = SandboxDemand(
            kind=ImageKind.CONTAINER,
            os=OperatingSystem.LINUX,
            arch=Arch.X86_64,
            network_mode=NetworkMode.OPEN,
            resources=resources,
            image=DIGEST,
            framework=DIGEST,
            retention="snapshot",
        )
        assert (
            request.needs
            == demand.needs
            == needs(network_mode=NetworkMode.OPEN, resources=resources, snapshot=True, saves=1)
        )
        # Validation rounds plan more saves in a row, one each.
        planned = {"planned_saves": 2}
        assert (
            request.model_copy(update=planned).needs
            == demand.model_copy(update=planned).needs
            == needs(network_mode=NetworkMode.OPEN, resources=resources, snapshot=True, saves=3)
        )
        booted = request.model_copy(update={"retention": "destroy", "snapshot": "docker:x"})
        assert booted.needs.snapshot
        # The verifiers booted from its snapshot need the verifier's network, on its leaf.
        mode = {"verifier_network_mode": NetworkMode.BLOCK}
        sealed = request.model_copy(update={"snapshot_verifier": resources, **mode}).needs
        assert sealed == demand.model_copy(update={"verifier": resources, **mode}).needs
        assert sealed.verifier is not None
        assert sealed.verifier.network_mode is NetworkMode.BLOCK


class TestProviders:
    async def test_a_provider_names_its_closest_profile_and_its_own_refusals(self) -> None:
        daytona = DaytonaProvider()
        asked = needs(resources=Resources(memory_mb=1536, cpus=2), snapshot=True)
        assert await daytona.unserved(asked, user="user", role=SandboxRole.SOLVER) == (
            "saves and boots no snapshots",
            "memory_mb=1536 is not a whole number of GiB, which is the unit Daytona allocates",
        )
        assert await daytona.unserved(needs(), user="user", role=SandboxRole.SOLVER) == ()

    async def test_orchard_refuses_an_account_its_vms_cannot_honour(self) -> None:
        orchard = OrchardProvider()
        mac = needs(
            kind=ImageKind.VM,
            os=OperatingSystem.MACOS,
            arch=Arch.ARM64,
            network_mode=NetworkMode.OPEN,
        )
        (root,) = await orchard.unserved(mac, user="root", role=SandboxRole.SOLVER)
        assert "must not be root" in root
        (control,) = await orchard.unserved(mac, user=CONTROL_ACCOUNT, role=SandboxRole.SOLVER)
        assert "must declare [agent].sudo = true" in control
        assert await orchard.unserved(mac, user=CONTROL_ACCOUNT, role=SandboxRole.VERIFIER) == ()
        storage = mac.model_copy(update={"resources": Resources(storage_mb=1536)})
        assert await orchard.unserved(storage, user="user", role=SandboxRole.SOLVER) == (
            "Orchard storage_mb must be a whole GiB",
        )

    async def test_orchard_says_how_to_set_up_restricted_networking_only_for_macos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing() -> None:
            raise ProviderCapabilityError("restricted macOS networking needs Softnet")

        monkeypatch.setattr("orbit.run.providers.orchard.provider.check_softnet", missing)
        orchard = OrchardProvider()
        mac = needs(kind=ImageKind.VM, os=OperatingSystem.MACOS, arch=Arch.ARM64)
        assert await orchard.unserved(mac, user="user", role=SandboxRole.SOLVER) == (
            "network block is not one it applies (open)",
            "restricted macOS networking needs Softnet",
        )
        linux = await orchard.unserved(needs(), user="user", role=SandboxRole.SOLVER)
        assert not any("Softnet" in reason for reason in linux)

    async def test_the_fleet_asks_its_leaves_rules_of_the_nodes_that_run_this_engine(
        self,
    ) -> None:
        """A Node's leaf applies its own rules as it creates a Sandbox, so a Run asks them
        before it selects the Fleet; a Node of another engine serves this Run nothing."""
        framework = fleet_framework_identity()
        orchard = CapabilityProfile(
            provider="orchard",
            kind=ImageKind.VM,
            os=OperatingSystem.MACOS,
            arch=Arch.ARM64,
            gui=True,
            network_modes=frozenset({NetworkMode.OPEN}),
            storage_limit=True,
        )
        nodes = [
            mac_node("mac").model_copy(update={"framework": framework, "profiles": (orchard,)}),
            linux_node("old").model_copy(update={"profiles": (PROFILE,)}),
        ]

        async def registered() -> list[NodeDeclaration]:
            return nodes

        fleet = FleetProvider(SimpleNamespace(nodes=registered))  # type: ignore[arg-type]
        assert await fleet.profiles() == (orchard,)
        mac = needs(
            kind=ImageKind.VM,
            os=OperatingSystem.MACOS,
            arch=Arch.ARM64,
            network_mode=NetworkMode.OPEN,
            resources=Resources(storage_mb=1536),
        )
        assert await fleet.unserved(mac, user="root", role=SandboxRole.SOLVER) == (
            "Orchard storage_mb must be a whole GiB",
            "The agent account must not be root",
        )
        assert (
            await fleet.unserved(
                mac.model_copy(update={"resources": Resources()}),
                user="user",
                role=SandboxRole.SOLVER,
            )
            == ()
        )
        linux = await fleet.unserved(needs(), user="user", role=SandboxRole.SOLVER)
        assert linux[0] == "starts vm Sandboxes, not container"

    async def test_a_refused_request_raises_naming_the_provider_id(self) -> None:
        request = SandboxRequest(
            episode_id="e1",
            prepared_image=PreparedTaskImage(
                kind=ImageKind.CONTAINER,
                source="external-ref",
                input_identity=DIGEST,
                runtime_ref="image@" + DIGEST,
                prepared_identity=DIGEST,
                resolved_reference="image@" + DIGEST,
            ),
            resources=Resources(container_features=(ContainerFeature.PERF,)),
            network=NetworkPolicy(),
        )
        with pytest.raises(
            ProviderCapabilityError,
            match=r"^daytona cannot serve this Sandbox: gives no container features perf",
        ):
            await DaytonaProvider().accepts(request)


def test_a_solver_saves_one_snapshot_for_each_round_to_come_and_one_at_its_end_at_most() -> None:
    """Its end's save serves a snapshot verifier and retention alike; a shared solver, which
    its verifier runs in, is saved at its end only to keep it."""
    from orbit.core.capability import solver_saves

    assert solver_saves(2, snapshot_verified=False, kept_as_snapshot=False) == 2
    assert solver_saves(2, snapshot_verified=True, kept_as_snapshot=False) == 3
    assert solver_saves(2, snapshot_verified=False, kept_as_snapshot=True) == 3
    assert solver_saves(2, snapshot_verified=True, kept_as_snapshot=True) == 3
    assert solver_saves(0, snapshot_verified=False, kept_as_snapshot=False) == 0
