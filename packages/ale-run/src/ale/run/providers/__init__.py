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
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ale.core.capability import Arch, SandboxNeeds
from ale.core.config import RunConfig
from ale.core.errors import ConfigError, ProviderCapabilityError, ProviderStartError
from ale.core.sandbox import Provider, Sandbox, SandboxCheck, SandboxRequest, SandboxRole
from ale.core.task import Task
from ale.core.taskspec import ContainerFeature, NetworkMode, NetworkPolicy, VerificationMode
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
RETAINING_PROVIDERS = ("docker", "firecracker", "qemu", "daytona", "orchard", "fleet")

#: The provider ids of the schedulers, which start Sandboxes on machines they manage; every
#: other provider id names a leaf, which starts them on this one.
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


def leaf(provider_id: str, *, gpus: tuple[int, ...] = ()) -> Provider:
    """The leaf a provider id names: ``docker:<runtime>`` for each runtime Docker's table
    holds, ``qemu``, ``firecracker`` or ``orchard``. ``gpus`` are the Host GPUs a Docker
    leaf may lease, all of them when empty."""
    if provider_id.startswith("docker:"):
        from ale.run.providers.docker import RUNTIMES, DockerProvider

        runtime = provider_id.removeprefix("docker:")
        if runtime in RUNTIMES:
            return DockerProvider(gpus=gpus, runtime=runtime)
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


def _sandboxes(
    task: Task, *, snapshot: bool, solver: bool
) -> Iterator[tuple[SandboxRole, str, SandboxNeeds]]:
    """Each Sandbox an episode of ``task`` starts, as the episode protocol requests it: its
    role, the agent's account in it, and what it needs."""
    spec = task.spec
    separate = spec.verify.environment_mode is VerificationMode.SEPARATE
    if solver:
        yield (
            SandboxRole.SOLVER if separate else SandboxRole.SHARED,
            spec.image.user,
            SandboxNeeds(
                kind=spec.image.kind,
                os=spec.os,
                arch=Arch.of(spec.os),
                network_mode=spec.network.mode,
                resources=spec.resources,
                snapshot=snapshot,
            ),
        )
    if separate:
        image = spec.verify.image or spec.image
        assert spec.verify.resources is not None
        yield (
            SandboxRole.VERIFIER,
            image.user,
            SandboxNeeds(
                kind=image.kind,
                os=spec.os,
                arch=Arch.of(spec.os),
                network_mode=NetworkMode.OPEN,
                resources=spec.verify.resources.as_resources(),
            ),
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
        return leaf(provider_id, gpus=settings.docker.gpus)

    async def select(
        self, tasks: Sequence[Task], *, solvers: bool = True, snapshots: bool = False
    ) -> None:
        """Choose, for each Task's solver and separate verifier, the first Provider in
        ``sandbox.providers`` that serves its Sandbox, before anything is prepared or
        started.

        A Sandbox none serves refuses the whole selection, naming every such Task and role
        and why each Provider does not serve it. ``solvers=False`` leaves the solvers to
        their Tasks, for a Run that reuses solvers kept earlier; ``snapshots`` says the
        solvers save snapshots or boot from them.
        """
        order = self.settings.sandbox.providers
        refused: list[str] = []
        for task in tasks:
            for role, user, needs in _sandboxes(task, snapshot=snapshots, solver=solvers):
                reasons: list[str] = []
                verifier = role is SandboxRole.VERIFIER
                for provider_id in order:
                    why = await self.get(provider_id).unserved(needs, user=user, role=role)
                    if not why:
                        if verifier:
                            task.verifier_provider = provider_id
                        else:
                            task.provider = provider_id
                        break
                    reasons.append(f"    {provider_id}: {'; '.join(why)}")
                else:
                    label = "verifier" if verifier else "solver"
                    refused.append(f"  {task.spec.name} {label}:\n" + "\n".join(reasons))
        if refused:
            raise ProviderCapabilityError(
                f"no Provider in sandbox.providers ({', '.join(order)}) serves "
                + ("this Sandbox" if len(refused) == 1 else f"these {len(refused)} Sandboxes")
                + ":\n"
                + "\n".join(refused)
            )

    async def preflight(self, tasks: Sequence[Task], *, solvers: bool = True) -> None:
        """Check, before any of their episodes starts, that each leaf selected for ``tasks``
        can run here and gives the container features its Sandboxes need.

        Each leaf runs its own preflight with the needs of the Sandboxes it was selected
        for, again only for needs it has not passed for yet, its check and probes included,
        such as a GPU or another runtime a later Task asks for. Then, for each set of
        container features its Sandboxes ask for, it starts a throwaway Sandbox as it would
        the first that asks for that set, with the same grants but no GPU, and
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
            for role, user, needs in _sandboxes(task, snapshot=False, solver=solvers):
                verifier = role is SandboxRole.VERIFIER
                provider_id = task.verifier_provider if verifier else task.provider
                assert provider_id is not None, "select before preflight"
                if provider_id in SCHEDULERS:
                    continue
                wanted.setdefault(provider_id, []).append(needs)
                image = task.prepared_verifier_image if verifier else task.prepared_image
                assert image is not None, "prepare before preflight"
                first.setdefault(
                    (provider_id, frozenset(needs.resources.container_features)),
                    SandboxRequest(
                        episode_id=f"preflight-{uuid.uuid4().hex[:8]}",
                        os=needs.os,
                        role=role,
                        prepared_image=image,
                        resources=needs.resources.model_copy(update={"gpus": 0, "gpu_types": ()}),
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
            async with await self.get(provider_id).create(request) as sandbox:
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
