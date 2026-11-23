"""[verifier.validation]: the verifier of validation rounds, which has every key [verifier] has
and inherits each it does not set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support import (
    RoundsProvider,
    TurnsHarness,
    Verifiers,
    edit_task_config,
    rounds_task,
)
from tests.unit.test_contracts import _BLOCK, _OPEN, _PYPI, config
from tests.unit.test_validation_rounds import _run, _task

from ale.core.config import RunTimeouts
from ale.core.errors import TaskDefinitionError
from ale.core.sandbox import SandboxRole
from ale.core.taskspec import NetworkMode, ValidationTable, VerificationMode
from ale.core.verdict import Status
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.scaffold import scaffold_task
from ale.run.secrets import resolve_task_variables
from ale.run.task_images import shares_verifier_image
from ale.run.tasksets.manifest import ManifestTask, load_tasks

pytestmark = pytest.mark.unit

_SEPARATE = {
    "environment_mode": "separate",
    "environment": {"cpus": 2, "memory_mb": 2048, "image": "registry.example/v:1"},
}


def test_each_key_it_sets_replaces_the_verifiers_and_tables_merge_key_by_key() -> None:
    resolved = config(
        {
            "verifier": {
                "timeout_sec": 600,
                "env": {"A": "1", "B": "2"},
                "user": "grader",
                "environment_mode": "separate",
                "environment": {
                    "cpus": 2,
                    "memory_mb": 2048,
                    "image": "registry.example/v:1",
                    "env": {"X": "1"},
                    "workdir": "grade",
                },
                "network_mode": "allowlist",
                "allowed_hosts": ["pypi.org"],
                "validation": {
                    "timeout_sec": 60,
                    "env": {"B": "3"},
                    "user": 0,
                    "environment": {"cpus": 8, "env": {"Y": "2"}},
                    "network_mode": "no-network",
                },
            }
        }
    )
    table = resolved.verifier.for_validation()
    assert (table.timeout_sec, table.env, table.user) == (60, {"A": "1", "B": "3"}, 0)
    assert table.mode is VerificationMode.SEPARATE and table.validation is None
    environment = table.environment
    assert environment is not None
    assert (environment.cpus, environment.memory_mb, environment.image, environment.workdir) == (
        8,
        2048,
        "registry.example/v:1",
        "grade",
    )
    assert environment.env == {"X": "1", "Y": "2"}
    # A network it names replaces the inherited one whole.
    assert (table.network_mode, table.allowed_hosts) == ("no-network", None)
    assert resolved.network_of(table) == _BLOCK
    assert resolved.verifier_network == _PYPI


def test_without_keys_of_its_own_it_is_the_verifiers() -> None:
    verifier = config({"verifier": {**_SEPARATE, "network_mode": "no-network"}}).verifier
    assert verifier.for_validation() == verifier
    assert (
        verifier.model_copy(update={"validation": ValidationTable()}).for_validation() == verifier
    )


def test_it_inherits_only_what_its_placement_uses() -> None:
    shared = config({"verifier": {**_SEPARATE, "validation": {"environment_mode": "shared"}}})
    table = shared.verifier.for_validation()
    assert table.mode is VerificationMode.SHARED and table.environment is None
    snapshot = config({"verifier": {**_SEPARATE, "validation": {"environment_mode": "snapshot"}}})
    table = snapshot.verifier.for_validation()
    assert table.environment is not None
    assert (table.environment.cpus, table.environment.image) == (2, None)
    # An environment declared over a shared verifier makes it separate, as Harbor resolves one.
    apart = config(
        {"verifier": {"environment_mode": "shared", "validation": {"environment": {"cpus": 4}}}}
    )
    assert apart.verifier.for_validation().mode is VerificationMode.SEPARATE
    # Over a snapshot verifier it stays one, which takes no image.
    with pytest.raises(ValidationError, match=r"\[verifier.validation\] over what it inherits"):
        config(
            {
                "verifier": {
                    "environment_mode": "snapshot",
                    "validation": {"environment": {"image": "registry.example/v:1"}},
                }
            }
        )


def test_its_environment_network_replaces_the_inherited_baseline() -> None:
    resolved = config(
        {
            "verifier": {
                "environment_mode": "separate",
                "environment": {"cpus": 2, "network_mode": "no-network"},
                "validation": {"environment": {"network_mode": "public"}},
            }
        }
    )
    assert resolved.verifier_network == _BLOCK
    assert resolved.network_of(resolved.verifier.for_validation()) == _OPEN


def _folder(tmp_path: Path, files: dict[str, str], verifier: dict[str, Any]) -> ManifestTask:
    root = scaffold_task(tmp_path / "task")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    edit_task_config(root, {"agent": {"validation_rounds": 1}, "verifier": verifier})
    task = load_tasks(root)[0]
    task.validations = 1
    return task


_VALIDATION = {"validation/test.sh": "#!/usr/bin/env bash\n"}


def test_a_task_builds_its_validation_verifier_as_it_builds_its_verifier(tmp_path: Path) -> None:
    task = rounds_task(
        tmp_path,
        rounds=1,
        mode="separate",
        validation={
            "timeout_sec": 30,
            "env": {"SPLIT": "val"},
            "user": "grader",
            "network_mode": "no-network",
            "environment": {"image": "registry.example/val:1", "cpus": 2},
        },
    )
    spec = task.spec
    validation = spec.validation
    assert validation is not None and validation.environment_mode is VerificationMode.SEPARATE
    assert validation.image is not None and validation.image.ref == "registry.example/val:1"
    assert validation.resources is not None
    assert (validation.resources.cpus, validation.resources.memory_mb) == (2, 2048)
    assert (validation.user, validation.network.mode) == ("grader", NetworkMode.BLOCK)
    assert spec.verify.network.mode is NetworkMode.OPEN and spec.verify.user is None
    assert spec.validation_timeout == 30 != spec.timeouts.verify
    assert spec.variables.validation == {"SPLIT": "val"}
    assert spec.variables.verification(VerificationMode.SEPARATE, validation=True)["SPLIT"] == "val"
    assert "SPLIT" not in spec.variables.verification(VerificationMode.SEPARATE)
    assert not shares_verifier_image(task)


def test_a_task_without_it_keeps_its_specification(tmp_path: Path) -> None:
    """Nothing it would add is in the dump, so the spec_hash is the one it had."""
    task = rounds_task(tmp_path, rounds=1, mode="separate")
    dumped = task.spec.model_dump(mode="json")
    assert dumped["validation"] is None and "validation" not in dumped["timeouts"]
    assert not {"validation", "validation_environment"} & dumped["variables"].keys()
    assert task.spec.validation_timeout == task.spec.timeouts.verify
    assert shares_verifier_image(task)


def test_validation_dockerfile_builds_the_image_of_a_separate_validation_verifier(
    tmp_path: Path,
) -> None:
    built = _folder(
        tmp_path / "built",
        {**_VALIDATION, "validation/Dockerfile": "FROM python:3.12\n"},
        {"environment_mode": "shared", "validation": {"environment_mode": "separate"}},
    )
    assert built.spec.validation is not None and built.spec.validation.image is not None
    assert built.spec.validation.image.ref is None
    assert built.validation_image_source_digest is not None
    assert not shares_verifier_image(built)
    # As tests/Dockerfile needs a separate verifier, validation/Dockerfile needs one too.
    with pytest.raises(TaskDefinitionError, match=r"validation/Dockerfile requires separate"):
        _folder(
            tmp_path / "shared",
            {**_VALIDATION, "validation/Dockerfile": "FROM python:3.12\n"},
            {"environment_mode": "separate", "validation": {"environment_mode": "shared"}},
        )


def test_without_an_image_of_its_own_it_runs_the_one_tests_dockerfile_builds(
    tmp_path: Path,
) -> None:
    task = _folder(
        tmp_path,
        {**_VALIDATION, "tests/Dockerfile": "FROM python:3.12\n"},
        {"environment_mode": "separate", "validation": {"timeout_sec": 30}},
    )
    assert task.spec.validation is not None
    assert task.spec.validation.image == task.spec.verify.image is not None
    assert task.validation_image_source_digest is None
    assert shares_verifier_image(task)


@pytest.mark.asyncio
async def test_rounds_run_with_its_limit_variables_and_network_and_the_runlock_records_it(
    tmp_path: Path, verifiers: Verifiers, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits: list[tuple[str, float | None]] = []
    deadline = EpisodeProtocol._with_deadline

    async def recorded(self, ctx, phase, seconds, coro):  # type: ignore[no-untyped-def]
        limits.append((phase.value, seconds))
        return await deadline(self, ctx, phase, seconds, coro)

    monkeypatch.setattr(EpisodeProtocol, "_with_deadline", recorded)
    provider = RoundsProvider()
    task = _task(
        tmp_path,
        rounds=1,
        environment={"cpus": 1, "memory_mb": 1024, "env": {"SPLIT": "test"}},
        validation={"timeout_sec": 45, "env": {"SPLIT": "val"}, "network_mode": "no-network"},
    )
    resolve_task_variables([task], oracle=False, protected={})
    assert {item.table for item in task.variables.provenance} == {  # type: ignore[union-attr]
        "environment",
        "verifier.validation",
    }
    result = await _run(tmp_path, provider, task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert [env["SPLIT"] for env in verifiers.envs] == ["val", "test"]
    assert ("validate", 45) in limits and ("verify", task.spec.timeouts.verify) in limits
    (validator,) = provider.booted(SandboxRole.VERIFIER)
    assert validator.network.mode is NetworkMode.BLOCK
    first = provider.booted(SandboxRole.SHARED)[0]
    assert (first.verifier_network_mode, first.round_verifier_network_mode) == (
        NetworkMode.OPEN,
        NetworkMode.BLOCK,
    )
    lock = result.lock
    assert lock is not None and lock.validation is not None
    assert (lock.validation.mode, lock.validation.network.mode) == (
        VerificationMode.SHARED,
        NetworkMode.BLOCK,
    )
    assert lock.verification.network.mode is NetworkMode.OPEN


@pytest.mark.asyncio
async def test_a_runlock_records_no_validation_verifier_where_it_is_the_verifiers(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    task = _task(tmp_path, rounds=1)
    result = await _run(tmp_path, RoundsProvider(), task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.lock is not None and result.lock.validation is None
    assert "validation" not in result.lock.model_dump(mode="json")


def test_gpu_keys_are_checked_as_they_resolve() -> None:
    gpus = {"environment_mode": "separate", "environment": {"cpus": 1, "gpus": 1}}
    gpus["environment"] |= {"gpu_types": ["A100"], "gpu_memory_mb": 1000}
    # A GPU model of its own, over the verifier's GPU.
    other = config({"verifier": {**gpus, "validation": {"environment": {"gpu_types": ["H100"]}}}})
    environment = other.verifier.for_validation().environment
    assert environment is not None and (environment.gpus, environment.gpu_types) == (1, ("H100",))
    # No GPUs inherits no GPU model or memory.
    none = config({"verifier": {**gpus, "validation": {"environment": {"gpus": 0}}}})
    environment = none.verifier.for_validation().environment
    assert environment is not None
    assert (environment.gpus, environment.gpu_types, environment.gpu_memory_mb) == (0, None, None)
    with pytest.raises(ValidationError, match=r"\[verifier.validation.environment\].gpu_types"):
        config(
            {"verifier": {**gpus, "validation": {"environment": {"gpus": 0, "gpu_types": ["T4"]}}}}
        )


def test_a_network_its_environment_names_replaces_the_verifiers_own() -> None:
    resolved = config(
        {
            "verifier": {
                "environment_mode": "separate",
                "environment": {"cpus": 1},
                "network_mode": "public",
                "validation": {"environment": {"network_mode": "no-network"}},
            }
        }
    )
    assert resolved.verifier_network == _OPEN
    assert resolved.network_of(resolved.verifier.for_validation()) == _BLOCK


def test_validation_dockerfile_builds_its_image_whatever_image_the_verifier_names(
    tmp_path: Path,
) -> None:
    task = _folder(
        tmp_path,
        {**_VALIDATION, "validation/Dockerfile": "FROM python:3.12\n"},
        {
            "environment_mode": "separate",
            "environment": {"cpus": 2, "image": "registry.example/tests:1"},
        },
    )
    assert task.spec.verify.image is not None
    assert task.spec.verify.image.ref == "registry.example/tests:1"
    assert task.spec.validation is not None and task.spec.validation.image is not None
    assert task.spec.validation.image.ref is None
    assert task.validation_image_source_digest is not None
    with pytest.raises(TaskDefinitionError, match="validation/Dockerfile names its base"):
        _folder(
            tmp_path / "both",
            {**_VALIDATION, "validation/Dockerfile": "FROM python:3.12\n"},
            {
                "environment_mode": "separate",
                "validation": {"environment": {"image": "registry.example/val:1"}},
            },
        )


@pytest.mark.asyncio
async def test_a_proxied_network_of_the_rounds_alone_starts_the_egress_proxy(
    tmp_path: Path,
) -> None:
    from ale.run.cli.tasks import _egress_proxy

    task = rounds_task(
        tmp_path,
        rounds=1,
        validation={"network_mode": "allowlist", "allowed_hosts": ["pypi.org"]},
    )
    assert not task.spec.network.proxied and not task.spec.verify.network.proxied
    proxy, registry = await _egress_proxy([task], None)
    try:
        assert proxy is not None and registry is not None
    finally:
        if proxy is not None:
            await proxy.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verifier", "validation", "prepared"),
    [
        # Booted from the solver's snapshot, on its leaf: nothing of its own to prepare.
        ({}, {"environment_mode": "snapshot", "environment": {"cpus": 2}}, ["solver"]),
        ({}, {"environment_mode": "separate"}, ["solver", "validation"]),
        (
            {"environment_mode": "separate", "environment": {"cpus": 2}},
            {"timeout_sec": 30},
            ["solver", "verifier"],
        ),
    ],
)
async def test_a_run_prepares_the_image_of_a_separate_validation_verifier_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier: dict[str, Any],
    validation: dict[str, Any],
    prepared: list[str],
) -> None:
    from tests.support import provider_registry
    from tests.unit.test_validation_rounds import IMAGE

    from ale.run import cli
    from ale.run.task_images import ImagePreparationResult

    asked: list[str] = []

    async def preparing(task: Any, provider: Any, *, role: str = "solver") -> Any:
        asked.append(role)
        return ImagePreparationResult(task=str(task.spec.name), role=role, image=IMAGE, steps=())

    monkeypatch.setattr(cli.tasks, "prepare_task_image_result", preparing)
    monkeypatch.setattr(cli.tasks, "prepare_verifier_image_result", preparing)
    root = scaffold_task(tmp_path / "task")
    (root / "validation").mkdir()
    (root / "validation" / "test.sh").write_text("#!/usr/bin/env bash\n")
    edit_task_config(
        root,
        {"agent": {"validation_rounds": 1}, "verifier": {**verifier, "validation": validation}},
    )
    task = load_tasks(root)[0]
    task.validations = 1
    await cli.tasks._prepare_images([task], provider_registry(RoundsProvider()))
    assert asked == prepared
    assert (task.prepared_validation_image is not None) == (
        task.spec.validation_spec.environment_mode is VerificationMode.SEPARATE
    )


@pytest.mark.asyncio
async def test_a_snapshot_validation_verifier_is_recorded_with_the_image_it_booted(
    tmp_path: Path, verifiers: Verifiers
) -> None:
    task = _task(
        tmp_path,
        rounds=1,
        validation={"environment_mode": "snapshot", "environment": {"cpus": 2}},
    )
    result = await _run(tmp_path, RoundsProvider(), task, TurnsHarness())
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    lock = result.lock
    assert lock is not None and lock.validation is not None
    assert lock.validation.mode is VerificationMode.SNAPSHOT
    assert lock.validation.image is not None and lock.validation.resources is not None
    assert lock.validation.resources.requested.cpus == 2


def test_a_validation_verifier_of_its_own_built_image_is_a_sandbox_to_select_and_check(
    tmp_path: Path,
) -> None:
    """Placed as tests/'s separate verifier is, but of the image validation/Dockerfile builds."""
    from ale.run.providers import _sandboxes

    task = _folder(
        tmp_path,
        {
            **_VALIDATION,
            "tests/Dockerfile": "FROM python:3.12\n",
            "validation/Dockerfile": "FROM python:3.12\n",
        },
        {"environment_mode": "separate", "environment": {"cpus": 2}},
    )
    assert task.spec.validation == task.spec.verify
    images = [
        image for *_, image in _sandboxes(task, snapshot=False, solver=True, timeouts=RunTimeouts())
    ]
    assert images == ["solver", "verifier", "validation"]
