"""Sandbox providers: local container, microVM, virtual machine and cloud container backends.

A provider creates and destroys sandboxes, attaches a guest transport, applies the
network policy, and declares its capability profiles. All in-sandbox operations go through
the guest service, never through provider-specific code paths.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ale.core.capability import (
    Arch,
    GpuHold,
    SandboxNeeds,
    round_verifier_resources,
    solver_saves,
    unmet,
)
from ale.core.config import DockerProviderConfig, RunConfig, RunTimeouts, SlurmProviderConfig
from ale.core.errors import ConfigError, ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import (
    Provider,
    Sandbox,
    SandboxCheck,
    SandboxRequest,
    SandboxRole,
    verifiers_needs,
)
from ale.core.task import Task
from ale.core.taskspec import (
    ContainerFeature,
    NetworkMode,
    NetworkPolicy,
    VerificationMode,
    VerifySpec,
)
from ale.run import capprobe, guestd

if TYPE_CHECKING:
    from ale.run.providers.fleet import FleetClient

__all__ = [
    "RETAINING_PROVIDERS",
    "SCHEDULERS",
    "ProviderRegistry",
    "leaf",
    "machine_arch",
    "missing_features",
    "provider_named",
]

#: Every Provider that can leave a sandbox behind. A retained sandbox outlives the run
#: configuration that made it, so listing and destroying consult all of them.
RETAINING_PROVIDERS = ("docker", "firecracker", "qemu", "daytona", "orchard", "slurm", "fleet")

#: The provider ids of the schedulers, which start Sandboxes on machines they manage; every
#: other provider id names a leaf, which starts the final Sandbox directly.
SCHEDULERS = ("daytona", "fleet")


def machine_arch() -> Arch:
    """The instruction set of this machine, which a leaf that runs its Sandboxes natively
    starts them on."""
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return Arch.ARM64
    if machine in {"x86_64", "amd64"}:
        return Arch.X86_64
    raise ProviderCapabilityError(f"unsupported machine architecture {machine}")


def leaf(
    provider_id: str,
    *,
    docker: DockerProviderConfig = DockerProviderConfig(),
    slurm: SlurmProviderConfig | None = None,
) -> Provider:
    """The leaf a provider id names: ``docker:<runtime>`` for each runtime Docker's table
    holds, ``qemu``, ``firecracker``, ``slurm`` or ``orchard``. ``docker`` holds a Docker leaf's
    settings: the Host GPUs it may lease and the margin it admits a shared one with."""
    if provider_id == "slurm":
        from ale.run.providers.slurm import SlurmProvider

        return SlurmProvider(slurm or SlurmProviderConfig())
    if provider_id.startswith("docker:"):
        from ale.run.providers.docker import RUNTIMES, DockerProvider

        runtime = provider_id.removeprefix("docker:")
        if runtime in RUNTIMES:
            return DockerProvider(
                gpus=docker.gpus,
                gpu_memory_margin_mb=docker.gpu_memory_margin_mb,
                runtime=runtime,
            )
    elif provider_id == "qemu":
        from ale.run.providers.qemu import QemuProvider

        return QemuProvider()
    elif provider_id == "firecracker":
        from ale.run.providers.firecracker import FirecrackerProvider

        return FirecrackerProvider()
    elif provider_id == "orchard":
        from ale.run.providers.orchard import OrchardProvider

        return OrchardProvider()
    raise ProviderCapabilityError(f"{provider_id!r} names no leaf")


async def missing_features(
    sandbox: Sandbox, features: Iterable[ContainerFeature]
) -> dict[ContainerFeature, str]:
    """The ``features`` that ``sandbox`` lacks, each with the probes that failed and how.

    ``capprobe`` runs as root in the Sandbox on the framework's Python. A feature with no
    probe of its own, ``nested_containers``, is not checked here.
    """
    probed = sorted(feature for feature in set(features) if capprobe.FEATURES[feature])
    if not probed:
        return {}
    result = await sandbox.exec(
        [guestd.PYTHON["linux"], "-I", "-c", Path(capprobe.__file__).read_text(), *probed],
        timeout_sec=120,
    )
    if not result.ok:
        raise ProviderStartError(
            f"the container feature probe failed: {result.stderr.strip()[-2000:]}"
        )
    report: dict[str, dict[str, str]] = json.loads(result.stdout)
    return {
        feature: ", ".join(f"{probe} {outcome}" for probe, outcome in failed)
        for feature in probed
        if (failed := [item for item in report[feature].items() if item[1] != "ok"])
    }


#: Which of a Task's prepared images a Sandbox runs: the solver's, the verifier's of
#: ``tests/`` or that of validation rounds.
_Image = Literal["solver", "verifier", "validation"]
_Planned = tuple[SandboxRole, str, SandboxNeeds, _Image]


def _sandboxes(
    task: Task, *, snapshot: bool, solver: bool, timeouts: RunTimeouts
) -> Iterator[_Planned]:
    """Each Sandbox an episode of ``task`` starts, as the episode protocol requests it: its
    role, the agent's account in it, what it needs and which prepared image it runs. A
    verifier that boots from a snapshot its solver saves, a snapshot verifier or a
    validation round's, runs on the leaf that saved it, so the solver needs what that
    verifier needs too, and as many saves in a row as
    :func:`~ale.core.capability.solver_saves` counts. Each verifier needs to be held for its
    limit, on the leaf that saved the snapshot it boots from, or in the solver it runs in."""
    spec = task.spec
    mode = spec.verify.environment_mode
    tests = timeouts.verification(spec)
    rounds = timeouts.verification(spec, validation=True) if task.validations else None
    placed_rounds = spec.validation_spec.environment_mode
    held = [
        tests if mode is not VerificationMode.SEPARATE else None,
        rounds if placed_rounds is not VerificationMode.SEPARATE else None,
    ]
    longest = max((limit for limit in held if limit is not None), default=None)
    snapshot_verifier = (
        spec.verify.resources.as_resources()
        if mode is VerificationMode.SNAPSHOT and spec.verify.resources is not None
        else None
    )
    verifier = verifiers_needs(
        spec.image.kind,
        spec.os,
        (
            (snapshot_verifier, spec.verify.network.mode),
            (round_verifier_resources(spec, task.validations), spec.validation_spec.network.mode),
        ),
    )
    saves = solver_saves(
        task.validations,
        snapshot_verified=mode is VerificationMode.SNAPSHOT,
        kept_as_snapshot=snapshot,
    )
    if solver:
        yield (
            SandboxRole.SHARED if mode is VerificationMode.SHARED else SandboxRole.SOLVER,
            spec.image.user,
            SandboxNeeds(
                kind=spec.image.kind,
                os=spec.os,
                arch=Arch.of(spec.os),
                # Able to apply the shared verifier's network too.
                network_mode=spec.solver_network.mode,
                resources=spec.resources,
                snapshot=saves > 0 or verifier is not None,
                saves=saves,
                verifier=verifier,
                duration_sec=longest,
            ),
            "solver",
        )
    if mode is VerificationMode.SNAPSHOT:
        assert verifier is not None
        verifier = verifier.model_copy(update={"duration_sec": tests})
        yield SandboxRole.VERIFIER, spec.image.user, verifier, "verifier"
    separate: list[tuple[VerifySpec, _Image]] = (
        [(spec.verify, "verifier")] if mode is VerificationMode.SEPARATE else []
    )
    validation = spec.validation
    if (
        task.validations
        and validation is not None
        and validation.environment_mode is VerificationMode.SEPARATE
        and (validation != spec.verify or task.validation_image_source_digest is not None)
    ):
        # A separate round verifier apart from tests/'s, on the verifier's Provider.
        separate.append((validation, "validation"))
    for placed, prepared in separate:
        image = placed.image or spec.image
        assert placed.resources is not None
        limit = rounds if prepared == "validation" else tests
        yield (
            SandboxRole.VERIFIER,
            image.user,
            SandboxNeeds(
                kind=image.kind,
                os=spec.os,
                arch=Arch.of(spec.os),
                network_mode=placed.network.mode,
                resources=placed.resources.as_resources(),
                duration_sec=limit,
            ),
            prepared,
        )


class ProviderRegistry:
    """The Run's Providers, each built from its settings when first asked for by its
    provider id, and the choice among them of the one that starts each Sandbox.

    The Fleet is reached through one connection, whatever asks for it.
    """

    def __init__(self, settings: RunConfig) -> None:
        self.settings = settings
        self._instances: dict[str, Provider] = {}
        self._fleet: FleetClient | None = None
        #: The leaves whose preflight passed, each with the needs it passed for and the sets
        #: of container features a Sandbox of it was seen to have when it asked for them.
        self._checked: dict[str, set[SandboxNeeds]] = {}
        self._ready: dict[str, set[frozenset[ContainerFeature]]] = {}
        #: The leaves whose preflight's check passed in a Sandbox of theirs.
        self._shown: set[str] = set()

    @property
    def fleet(self) -> FleetClient | None:
        """The Run's connection to its Fleet, once a Provider uses it."""
        return self._fleet

    async def close(self) -> None:
        if self._fleet is not None:
            await self._fleet.close()

    def get(self, provider_id: str) -> Provider:
        """The Provider ``provider_id`` names, whether or not ``sandbox.providers`` does: a
        Sandbox kept earlier is reached through the Provider that kept it."""
        if provider_id not in self._instances:
            self._instances[provider_id] = self._build(provider_id)
        return self._instances[provider_id]

    def _build(self, provider_id: str) -> Provider:
        settings = self.settings.sandbox
        if provider_id == "fleet":
            from ale.run.providers.fleet.provider import FleetProvider, fleet_client

            if not settings.fleet.url:
                raise ConfigError("the fleet Provider needs sandbox.fleet.url, its control plane")
            self._fleet = fleet_client(settings.fleet.url, settings.fleet.token_env)
            return FleetProvider(self._fleet)
        if provider_id == "daytona":
            from ale.run.providers.daytona import DaytonaProvider

            return DaytonaProvider(settings.daytona)
        return leaf(provider_id, docker=settings.docker, slurm=settings.slurm)

    async def select(
        self, tasks: Sequence[Task], *, solvers: bool = True, snapshots: bool = False
    ) -> tuple[str, ...]:
        """Choose, for each Task's solver and verifier of its own, the first Provider in
        ``sandbox.providers`` that serves its Sandbox, before anything is prepared or
        started. A snapshot verifier boots on the leaf that saved its solver's snapshot, so
        its solver and it take the first Provider one of whose leaves serves both.

        A Sandbox none serves refuses the whole selection, naming every such Task and role
        and why each Provider does not serve it. ``solvers=False`` leaves the solvers to
        their Tasks, for a Run that reuses solvers kept earlier; a snapshot verifier then
        boots on the Provider its Task names, which keeps the snapshot, or is refused.
        ``snapshots`` says the solvers save snapshots or boot from them.

        Returns what the Run says of its selection once at its start: for each leaf that may
        start Sandboxes sharing a GPU and only watches their ``gpu_memory_mb``, how many.
        """
        order = self.settings.sandbox.providers
        refused: list[str] = []
        watched: Counter[str] = Counter()
        for task in tasks:
            sandboxes = list(
                _sandboxes(
                    task, snapshot=snapshots, solver=solvers, timeouts=self.settings.timeouts
                )
            )
            groups: list[tuple[list[_Planned], Sequence[str]]]
            snapshot = task.spec.verify.environment_mode is VerificationMode.SNAPSHOT
            if snapshot and not solvers:
                # Its solver's snapshot is kept by the Provider that saved it, and only there.
                assert task.provider is not None, "a reused solver names its Provider"
                ((role, user, needs, _),) = sandboxes
                if why := await self.get(task.provider).unserved(needs, user=user, role=role):
                    raise ProviderCapabilityError(
                        f"{task.spec.name}: its verifier boots from the snapshot "
                        f"{task.provider} keeps, which does not serve it: {'; '.join(why)}"
                    )
                task.verifier_provider = task.provider
                continue
            if snapshot:
                # One choice for both: only the leaf that saved the snapshot boots from it.
                groups = [(sandboxes, order)]
            else:
                # Every separate verifier of a Task, of tests/ and of its rounds, runs on
                # the one Provider its Task names for verifiers.
                verifiers = [each for each in sandboxes if each[0] is SandboxRole.VERIFIER]
                groups = [
                    ([sandbox], order)
                    for sandbox in sandboxes
                    if sandbox[0] is not SandboxRole.VERIFIER
                ] + ([(verifiers, order)] if verifiers else [])
            for group, candidates in groups:
                reasons: list[str] = []
                label = " and ".join(_ROLE[role] for role, *_ in group)
                for provider_id in candidates:
                    provider = self.get(provider_id)
                    why = await _unserved(provider, group)
                    if not why:
                        for role, _, needs, _ in group:
                            if role is SandboxRole.VERIFIER:
                                task.verifier_provider = provider_id
                            else:
                                task.provider = provider_id
                            if needs.resources.gpus and needs.resources.gpu_memory_mb is not None:
                                watched.update(await _watching(provider, needs))
                        break
                    reasons.append(f"    {provider_id}: {'; '.join(why)}")
                else:
                    refused.append(f"  {task.spec.name} {label}:\n" + "\n".join(reasons))
        if refused:
            raise ProviderCapabilityError(
                f"no Provider in sandbox.providers ({', '.join(order)}) serves "
                + ("this Sandbox" if len(refused) == 1 else f"these {len(refused)} Sandboxes")
                + ":\n"
                + "\n".join(refused)
            )
        return tuple(
            f"{leaf} watches the gpu_memory_mb of {count} Sandbox{'es' if count > 1 else ''} "
            "rather than enforcing it: a process past its share is killed"
            for leaf, count in watched.items()
        )

    async def preflight(self, tasks: Sequence[Task], *, solvers: bool = True) -> None:
        """Check, before any of their episodes starts, that each leaf selected for ``tasks``
        can run here and gives the container features its Sandboxes need.

        Each leaf runs its own preflight with the needs of the Sandboxes it was selected
        for, again only for needs it has not passed for yet, its check and probes included,
        such as a GPU or another runtime a later Task asks for. Then, for each set of
        container features its Sandboxes ask for, it starts a throwaway Sandbox as it would
        the first that asks for that set, omitting GPUs when the leaf can serve it that way, and
        :func:`missing_features` probes it: a profile says what the leaf gives, and only the
        Host shows that it does. A Sandbox is probed by its own set, since a grant one
        feature brings can make another pass. Missing features refuse the Run, naming each
        leaf and feature. A check a leaf's preflight returns runs once, in the first such
        Sandbox of it, or in one started as its first Sandbox asks when none asks for a
        feature. A scheduler's leaves run on machines it manages, so they are not checked
        from here. ``solvers`` is as for :meth:`select`.
        """
        wanted: dict[str, list[SandboxNeeds]] = {}
        first: dict[tuple[str, frozenset[ContainerFeature]], SandboxRequest] = {}
        for task in tasks:
            planned = _sandboxes(
                task, snapshot=False, solver=solvers, timeouts=self.settings.timeouts
            )
            for role, user, needs, prepared in planned:
                verifier = role is SandboxRole.VERIFIER
                provider_id = task.verifier_provider if verifier else task.provider
                assert provider_id is not None, "select before preflight"
                if provider_id in SCHEDULERS:
                    continue
                wanted.setdefault(provider_id, []).append(needs)
                image = {
                    "solver": task.prepared_image,
                    "verifier": task.prepared_verifier_image,
                    "validation": task.prepared_validation_image,
                }[prepared]
                assert image is not None, "prepare before preflight"
                first.setdefault(
                    (provider_id, frozenset(needs.resources.container_features)),
                    SandboxRequest(
                        episode_id=f"preflight-{uuid.uuid4().hex[:8]}",
                        os=needs.os,
                        role=role,
                        prepared_image=image,
                        resources=needs.resources,
                        network=NetworkPolicy(mode=NetworkMode.BLOCK),
                        sudo=needs.resources.sudo,
                        user=user,
                    ),
                )
        checks: dict[str, SandboxCheck] = {}
        preflighted: dict[str, list[SandboxNeeds]] = {}
        for provider_id, needs in wanted.items():
            passed = self._checked.setdefault(provider_id, set())
            if fresh := [need for need in dict.fromkeys(needs) if need not in passed]:
                check = await self.get(provider_id).preflight(fresh)
                preflighted[provider_id] = fresh
                if check is not None and provider_id not in self._shown:
                    checks[provider_id] = check
            self._ready.setdefault(provider_id, set())
        probes = {
            (provider_id, features): request
            for (provider_id, features), request in first.items()
            if features and features not in self._ready[provider_id]
        }
        for provider_id in checks:
            if not any(leaf == provider_id for leaf, _ in probes):
                probes[(provider_id, frozenset())] = first[(provider_id, frozenset())]
        lacking: dict[str, dict[ContainerFeature, str]] = {}
        for (provider_id, features), request in probes.items():
            leaf = self.get(provider_id)
            if request.resources.gpus:
                cpu = request.model_copy(
                    update={
                        "resources": request.resources.model_copy(
                            update={"gpus": 0, "gpu_types": (), "gpu_memory_mb": None}
                        )
                    }
                )
                if not await leaf.unserved(cpu.needs, user=cpu.user, role=cpu.role):
                    request = cpu
            async with await leaf.create(request) as sandbox:
                if check := checks.pop(provider_id, None):
                    await check(sandbox)
                    self._shown.add(provider_id)
                missing = await missing_features(sandbox, features)
            if missing:
                lacking.setdefault(provider_id, {}).update(missing)
            else:
                self._ready[provider_id].add(features)
        if lacking:
            raise ProviderCapabilityError(
                "on this Host, a Sandbox of each leaf below lacks container features its "
                "Tasks need:\n"
                + "\n".join(
                    f"  {provider_id}: "
                    + "; ".join(f"{feature} ({why})" for feature, why in sorted(missing.items()))
                    for provider_id, missing in lacking.items()
                )
            )
        # Passed only now: a check or a probe that failed leaves its leaf's preflight, and
        # the check it returns, to run again when the registry is used again.
        for provider_id, fresh in preflighted.items():
            self._checked[provider_id].update(fresh)


#: How a selection names each role.
_ROLE = {
    SandboxRole.SHARED: "solver",
    SandboxRole.SOLVER: "solver",
    SandboxRole.VERIFIER: "verifier",
}


async def _unserved(provider: Provider, group: Sequence[_Planned]) -> list[str]:
    """Why ``provider`` does not serve every Sandbox of ``group``, each reason prefixed with
    its role when there are several; and, when it serves each, why none of its leaves
    serves a solver and its snapshot verifier together."""
    if len(group) == 1:
        ((role, user, needs, _),) = group
        return list(await provider.unserved(needs, user=user, role=role))
    why = [
        f"{_ROLE[role]}: {reason}"
        for role, user, needs, _ in group
        for reason in await provider.unserved(
            needs.model_copy(update={"verifier": None}), user=user, role=role
        )
    ]
    if why:
        return why
    for role, user, needs, _ in group:
        if needs.verifier is not None and await provider.unserved(needs, user=user, role=role):
            return ["no one of its leaves serves both the solver and its snapshot verifier"]
    return []


async def _watching(provider: Provider, needs: SandboxNeeds) -> list[str]:
    """The leaves of ``provider``'s profiles that serve ``needs`` and only watch the memory a
    Sandbox that shares a GPU uses: ``provider`` itself for a leaf, any of a scheduler's."""
    return list(
        dict.fromkeys(
            profile.provider
            for profile in await provider.profiles()
            if profile.gpu_hold is GpuHold.WATCHED and not unmet(profile, needs)
        )
    )


def provider_named(name: str) -> Provider:
    """A default-configured Provider, for work on sandboxes it retained earlier.

    Retained handles name the Provider that made a sandbox; that name, not the current run
    configuration, decides who can list or destroy it. The Fleet is the one whose control
    plane ``$ALE_FLEET_URL`` names.
    """
    if name == "docker":
        return leaf("docker:runc")
    if name in {"firecracker", "qemu", "orchard"}:
        return leaf(name)
    if name == "slurm":
        # Its snapshots are listed from the cache below the home, whatever the partitions.
        return leaf("slurm")
    if name == "daytona":
        from ale.run.providers.daytona import DaytonaProvider

        return DaytonaProvider()
    if name == "fleet":
        from ale.run.providers.fleet.provider import FleetProvider, fleet_client

        url = os.environ.get("ALE_FLEET_URL", "")
        if not url:
            raise ProviderCapabilityError("set ALE_FLEET_URL to the Fleet's control plane")
        return FleetProvider(fleet_client(url, "ALE_FLEET_TOKEN"))
    raise ProviderCapabilityError(f"unknown provider {name!r}")
