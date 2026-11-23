"""Volumes and [verifier.validation]: which artifacts a Provider may mount, where validation/ runs,
what selection asks for it, and what a verifier's view of a volume leaves."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.support import (
    ROUNDS_WORK,
    MemorySandbox,
    RoundsProvider,
    TurnsHarness,
    Verifiers,
    provider_registry,
    rounds_task,
    write_task,
)
from tests.unit.test_runtime_contracts import DIGEST, make_lock

from orbit.core.capability import CapabilityProfile, round_verifier_resources
from orbit.core.config import RunTimeouts
from orbit.core.errors import ProviderCapabilityError
from orbit.core.sandbox import PreparedTaskImage, SandboxRequest, SandboxRole
from orbit.core.taskspec import NetworkMode, NetworkPolicy, Resources, VerificationMode
from orbit.core.verdict import Status
from orbit.run import artifact_files
from orbit.run.episode import run_episode
from orbit.run.protocol.episode_protocol import EpisodeProtocol
from orbit.run.provenance import ProvenanceInputs
from orbit.run.providers import _sandboxes
from orbit.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit

PREPARED = "sha256:" + "1" * 64
PREPARED_IMAGE = PreparedTaskImage(
    kind="container",
    source="external-ref",
    input_identity=DIGEST,
    runtime_ref="image@" + DIGEST,
    prepared_identity=DIGEST,
    resolved_reference="image@" + DIGEST,
)


def _task(tmp_path: Path, config: dict[str, object]):
    root = write_task(tmp_path / "task", config=config)
    (root / "validation").mkdir()
    (root / "validation" / "test.sh").write_text("#!/usr/bin/env bash\n")
    return load_tasks(root)[0]


def test_a_volume_candidate_is_an_absolute_path_without_a_glob(tmp_path: Path) -> None:
    """The outermost of nested ones; relative paths, globs and files named alike are not,
    and whether a candidate is a directory is the image's to say."""
    task = _task(
        tmp_path,
        {
            "artifacts": [
                "notes.txt",
                "/work",
                "/work/out",
                "/games/*.sgf",
                {"source": "/srv/app", "exclude": [".venv"]},
            ]
        },
    )
    assert task.spec.volume_candidates == ("/work", "/srv/app")


def test_validation_runs_where_its_table_places_it(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        {
            "agent": {"validation_rounds": 2},
            "verifier": {
                "environment_mode": "shared",
                "validation": {"environment": {"cpus": 4, "memory_mb": 8192}},
            },
        },
    )
    validation = task.spec.validation_spec
    assert task.spec.verify.environment_mode is VerificationMode.SHARED
    assert validation.environment_mode is VerificationMode.SEPARATE
    assert validation.resources is not None and validation.resources.cpus == 4
    # Without the table it runs as tests/ does.
    plain = _task(tmp_path / "plain", {"verifier": {"environment_mode": "snapshot"}})
    assert plain.spec.validation is None
    assert plain.spec.validation_spec is plain.spec.verify


def test_a_separate_validation_verifier_shares_the_tasks_verifier_provider(
    tmp_path: Path,
) -> None:
    """Selection asks for it as a verifier of the Task's, apart from a solver whose final
    verifier runs in it; a snapshot verifier of validation/ boots on the solver's leaf."""
    task = _task(
        tmp_path,
        {
            "agent": {"validation_rounds": 1},
            "verifier": {
                "validation": {"environment_mode": "separate", "environment": {"cpus": 3}}
            },
        },
    )
    task.validations = 1
    (solver_role, _, solver, _), (verifier_role, _, verifier, image) = _sandboxes(
        task, snapshot=False, solver=True, timeouts=RunTimeouts()
    )
    assert image == "validation"
    assert (solver_role, verifier_role) == (SandboxRole.SHARED, SandboxRole.VERIFIER)
    assert solver.verifier is None and verifier.resources.cpus == 3
    snapshot = _task(
        tmp_path / "snapshot",
        {
            "agent": {"validation_rounds": 1},
            "verifier": {
                "validation": {"environment_mode": "snapshot", "environment": {"cpus": 3}}
            },
        },
    )
    snapshot.validations = 1
    ((_, _, booted, _),) = _sandboxes(snapshot, snapshot=False, solver=True, timeouts=RunTimeouts())
    assert booted.verifier is not None and booted.verifier.resources.cpus == 3
    resources = round_verifier_resources(snapshot.spec)
    assert resources is not None and resources.cpus == 3


def test_a_sandbox_boots_from_a_snapshot_or_views_one(tmp_path: Path) -> None:
    image = {
        "kind": "container",
        "source": "external-ref",
        "input_identity": PREPARED,
        "runtime_ref": "ref",
        "resolved_reference": "ref",
        "prepared_identity": PREPARED,
    }
    base = {
        "episode_id": "e",
        "prepared_image": image,
        "resources": Resources(),
        "network": NetworkPolicy(mode=NetworkMode.OPEN),
    }
    viewer = SandboxRequest.model_validate(base | {"volumes_from": "docker:snapshot:s"})
    assert viewer.needs.snapshot
    with pytest.raises(ValueError, match="boots from a snapshot or mounts views"):
        SandboxRequest.model_validate(
            base | {"volumes_from": "docker:snapshot:s", "snapshot": "docker:snapshot:t"}
        )


def test_a_view_keeps_what_copying_would_have_kept(tmp_path: Path) -> None:
    """Links, special files and what ``exclude`` leaves out go; what a link pointed at stays."""
    outside = tmp_path / "outside"
    outside.write_text("the framework's")
    root = tmp_path / "view"
    (root / "nested" / ".venv" / "lib").mkdir(parents=True)
    (root / "kept.txt").write_text("kept")
    (root / "nested" / "kept.pt").write_text("kept")
    (root / "nested" / "skip.pyc").write_text("excluded")
    (root / "nested" / ".venv" / "lib" / "x.py").write_text("excluded")
    (root / "link").symlink_to(outside)
    (root / "nested" / "dir-link").symlink_to(root / "nested")
    os.mkfifo(root / "fifo")
    artifact_files.prune(str(root), [".venv", "*.pyc"])
    left = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    assert left == ["kept.txt", "nested", "nested/kept.pt"]
    assert outside.read_text() == "the framework's"
    (tmp_path / "linked").symlink_to(root)
    with pytest.raises(ValueError, match="is a link"):
        artifact_files.prune(str(tmp_path / "linked"))


class _RefusesViews(RoundsProvider):
    """Mounts every candidate as a volume, but its leaf refuses each verifier that would
    view one, as a Fleet Node too small for it does."""

    async def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(
            profile.model_copy(update={"volumes": True}) for profile in await super().profiles()
        )

    async def create(self, request: SandboxRequest) -> MemorySandbox:
        if request.volumes_from is not None:
            self.requests.append(request)
            raise ProviderCapabilityError("this Node cannot hold that verifier")
        sandbox = await super().create(request)
        sandbox.volumes = request.volumes
        return sandbox


@pytest.mark.asyncio
async def test_a_verifier_refused_its_views_gets_the_volumes_copied_from_the_snapshot(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    """A Sandbox booted from the snapshot, with the solver's resources, gives the volume to
    copy, and a verifier that mounts none scores it: views spare a copy, never the score."""
    provider = _RefusesViews()
    task = rounds_task(tmp_path, rounds=1, mode="separate", artifacts=[ROUNDS_WORK])
    task.prepared_image = PREPARED_IMAGE
    lock = make_lock()
    result = await run_episode(
        task,
        EpisodeProtocol(TurnsHarness(), validation_rounds=1),
        provider_registry(provider),
        run_dir=tmp_path / "runs",
        provenance=ProvenanceInputs(
            source=lock.task.source,
            agent=lock.agent,
            gateway=lock.gateway,
            config_hash=lock.config_hash,
            framework=lock.framework,
            validation_rounds=1,
        ),
    )
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert [item.reward for item in result.record.validation_rounds] == [0.33]
    assert result.verdict.rewards == {"reward": 0.67}
    verifiers_asked = [r for r in provider.requests if r.role is SandboxRole.VERIFIER]
    # For the round and for tests/: a viewer refused, a reader of the snapshot with the
    # solver's size, and a verifier of its own size that mounts nothing.
    for viewer, reader, verifier in (verifiers_asked[:3], verifiers_asked[3:]):
        assert viewer.volumes_from is not None and reader.snapshot == viewer.volumes_from
        assert (reader.resources.cpus, verifier.resources.cpus) == (1, 4)
        assert verifier.volumes_from is None and verifier.snapshot is None
    assert provider.snapshots == {}
