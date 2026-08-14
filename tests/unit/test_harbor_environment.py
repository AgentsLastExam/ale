from __future__ import annotations

from pathlib import Path

import pytest

from ale.core.config import RunConfig
from ale.core.errors import TaskDefinitionError, VerifierOutputError
from ale.core.taskspec import ImageKind, VerificationMode
from ale.run.harbor.docker import HarborDockerProvider
from ale.run.harbor.environment import _read_rewards, _resolve_env_value
from ale.run.harbor.providers import HarborProviderRegistry
from ale.run.harbor.task import HarborTask, load_harbor_tasks
from ale.run.scaffold import scaffold_task
from ale.run.tasksets import load_tasks

pytestmark = pytest.mark.unit


def _task(
    root: Path,
    *,
    environment: str = 'docker_image = "example/task:1"\ncpus = 2\nmemory = "2G"\nstorage = "10G"',
    verifier: str = "timeout_sec = 45",
    extra: str = "",
) -> Path:
    root.mkdir(parents=True)
    (root / "environment").mkdir()
    (root / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (root / "instruction.md").write_text("Do the task.\n")
    (root / "solution").mkdir()
    (root / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n")
    (root / "tests").mkdir()
    (root / "tests" / "test.sh").write_text("#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    (root / "task.toml").write_text(
        'version = "1.0"\n\n'
        '[metadata]\ncategory = "test"\n\n'
        f"[verifier]\n{verifier}\n\n"
        "[agent]\ntimeout_sec = 90\n\n"
        f"[environment]\n{environment}\n"
        f"{extra}"
    )
    return root


def test_loads_legacy_terminal_bench_shape_and_prefers_prebuilt_image(
    tmp_path: Path,
) -> None:
    task = load_harbor_tasks(_task(tmp_path / "demo"))[0]

    assert isinstance(task, HarborTask)
    assert task.spec.environment == "harbor"
    assert task.spec.image.ref == "example/task:1"
    assert task.spec.resources.model_dump() == {
        "cpus": 2,
        "memory_mb": 2048,
        "storage_mb": 10240,
        "gpus": 0,
        "sudo": False,
    }
    assert task.spec.timeouts.agent == 90
    assert task.folder.image_dockerfile is None


def test_local_dockerfile_is_used_when_no_prebuilt_ref(tmp_path: Path) -> None:
    task = load_harbor_tasks(_task(tmp_path / "local", environment="cpus = 1\nmemory_mb = 512"))[0]

    assert task.spec.image.ref is None
    assert task.folder.image_dockerfile == task.folder.image_dir / "Dockerfile"
    assert task.image_source_digest is not None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("os", '"windows"', "os"),
        ("tpu", '{ type = "v4", topology = "2x2" }', "tpu"),
        ("gpu_types", '["H100"]', "gpu_types"),
        ("skills_dir", '"/skills"', "skills_dir"),
    ],
)
def test_rejects_out_of_scope_environment_fields(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    with pytest.raises(TaskDefinitionError, match=message):
        load_harbor_tasks(
            _task(
                tmp_path / field,
                environment=f'docker_image = "example/task:1"\n{field} = {value}',
            )
        )


def test_rejects_compose_and_multi_step_tasks(tmp_path: Path) -> None:
    compose = _task(tmp_path / "compose")
    (compose / "environment" / "docker-compose.yaml").write_text("services: {}\n")
    with pytest.raises(TaskDefinitionError, match="Docker Compose"):
        load_harbor_tasks(compose)

    multi = _task(tmp_path / "multi")
    with (multi / "task.toml").open("a") as manifest:
        manifest.write('\n[[steps]]\nname = "one"\n')
    with pytest.raises(TaskDefinitionError, match="steps"):
        load_harbor_tasks(multi)


def test_maps_separate_verifier_ref_local_and_solver_reuse(tmp_path: Path) -> None:
    external = _task(
        tmp_path / "external",
        verifier=(
            'timeout_sec = 45\nenvironment_mode = "separate"\n\n'
            '[verifier.environment]\ndocker_image = "example/verifier:1"\n'
            "cpus = 1\nmemory_mb = 512"
        ),
    )
    external_task = load_harbor_tasks(external)[0]
    assert external_task.spec.verify.environment_mode is VerificationMode.SEPARATE
    assert external_task.spec.verify.image is not None
    assert external_task.spec.verify.image.ref == "example/verifier:1"
    assert external_task.folder.verifier_dockerfile is None

    local = _task(
        tmp_path / "local-verifier",
        verifier='timeout_sec = 45\nenvironment_mode = "separate"',
    )
    (local / "tests" / "Dockerfile").write_text("FROM ubuntu:24.04\nCOPY . /tests\n")
    local_task = load_harbor_tasks(local)[0]
    assert local_task.spec.verify.image is not None
    assert local_task.spec.verify.image.ref is None
    assert local_task.folder.verifier_dockerfile is not None

    reused = _task(
        tmp_path / "reuse",
        verifier='timeout_sec = 45\nenvironment_mode = "separate"',
    )
    reused_task = load_harbor_tasks(reused)[0]
    assert reused_task.spec.verify.image is None
    assert reused_task.folder.verifier_dockerfile is None


def test_generic_loader_rejects_mixed_protocol_collection(tmp_path: Path) -> None:
    root = tmp_path / "mixed"
    scaffold_task(root / "standard")
    _task(root / "harbor")
    with pytest.raises(TaskDefinitionError, match=r"mixed task\.yaml and task\.toml"):
        load_tasks(root)


def test_harbor_registry_uses_its_adapter_and_rejects_vms() -> None:
    registry = HarborProviderRegistry(RunConfig())
    assert isinstance(registry.get(ImageKind.CONTAINER), HarborDockerProvider)
    assert registry.get(ImageKind.CONTAINER) is registry.get(ImageKind.CONTAINER)
    with pytest.raises(ValueError, match="only container"):
        registry.get(ImageKind.VM)


def test_environment_variable_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_TEST_VALUE", "present")
    assert _resolve_env_value("x-${HARBOR_TEST_VALUE}-y") == "x-present-y"
    assert _resolve_env_value("${HARBOR_MISSING:-fallback}") == "fallback"
    with pytest.raises(TaskDefinitionError, match="HARBOR_MISSING"):
        _resolve_env_value("${HARBOR_MISSING}")


class _RewardSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read_file(self, path) -> bytes:  # type: ignore[no-untyped-def]
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc


@pytest.mark.asyncio
async def test_reads_harbor_json_before_text_reward() -> None:
    rewards = await _read_rewards(
        _RewardSandbox(
            {
                "/logs/verifier/reward.json": b'{"correctness": 0.75}',
                "/logs/verifier/reward.txt": b"1",
            }
        )  # type: ignore[arg-type]
    )
    assert rewards == {"correctness": 0.75}


@pytest.mark.asyncio
async def test_rejects_missing_or_malformed_harbor_rewards() -> None:
    with pytest.raises(VerifierOutputError):
        await _read_rewards(_RewardSandbox({}))  # type: ignore[arg-type]
    with pytest.raises(VerifierOutputError):
        await _read_rewards(
            _RewardSandbox({"/logs/verifier/reward.json": b'{"reward": "bad"}'})  # type: ignore[arg-type]
        )
