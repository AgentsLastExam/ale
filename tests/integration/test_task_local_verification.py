from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ale.core.sandbox import ImageRef, PreparedTaskImage
from ale.core.taskspec import ImageKind, ImageSpec, VerifySpec
from ale.core.verdict import Status
from ale.run.episode import run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.protocol.episode_protocol import EpisodeProtocol
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.task_images import (
    _OciBuild,
    prepare_verifier_image_result,
)
from ale.run.tasksets.manifest import load_tasks
from tests.support import CONTAINER_BASE, edit_task_config, provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]


@pytest.fixture
def framework_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake image identities are not Docker images; the layer is tested on real ones."""

    async def layer(image: str, *, user: str, sudo: bool, vm: bool = False) -> str:
        return image if image.startswith("sha256:") else DIGEST

    monkeypatch.setattr("ale.run.task_images.with_framework", layer)

    async def disk(image, **kwargs):
        return image, True

    monkeypatch.setattr("ale.run.task_images.framed_disk", disk)


DIGEST = "sha256:" + "d" * 64


def _prepared(kind: ImageKind, source: str = "solver-local") -> PreparedTaskImage:
    values: dict[str, object] = {
        "kind": kind,
        "source": source,
        "input_identity": DIGEST,
        "runtime_ref": "ale:local" if kind is ImageKind.CONTAINER else "/tmp/disk.qcow2",
        "prepared_identity": DIGEST,
    }
    if source == "external-ref":
        values["resolved_reference"] = "ghcr.io/acme/image@" + DIGEST
    else:
        values["image_source_identity"] = DIGEST
    if kind is ImageKind.VM and source != "external-ref":
        values["oci_identity"] = DIGEST
        values["materializer_identity"] = DIGEST
    return PreparedTaskImage.model_validate(values)


class _Provider:
    def __init__(self, kind: ImageKind) -> None:
        self.kind = kind
        self.inputs: list[ImageRef | PreparedTaskImage] = []

    async def prepare_image(self, image):  # type: ignore[no-untyped-def]
        self.inputs.append(image)
        return (
            image if isinstance(image, PreparedTaskImage) else _prepared(self.kind, "external-ref")
        )


class _Registry:
    def __init__(self) -> None:
        self.providers = {kind: _Provider(kind) for kind in ImageKind}

    def get(self, kind: ImageKind) -> _Provider:
        return self.providers[kind]


def task_repository(tmp_path: Path, name: str) -> tuple[Path, Path]:
    repository = tmp_path / "ale-tasks-demo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    task = scaffold_task(repository / "tasks" / name)
    return repository, task


async def execute(task_path: Path, run_dir: Path):  # type: ignore[no-untyped-def]
    return await run_episode(
        load_tasks(task_path)[0],
        EpisodeProtocol(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
        collect_artifacts=True,
    )


@pytest.mark.asyncio
async def test_shared_verification_has_no_second_prepared_image(tmp_path: Path) -> None:
    task = load_tasks(scaffold_task(tmp_path / "shared-image"))[0]
    task.prepared_image = _prepared(ImageKind.CONTAINER)
    assert await prepare_verifier_image_result(task, _Registry()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(ImageKind))
async def test_separate_verifier_reuses_solver_prepared_kind(
    tmp_path: Path, kind: ImageKind
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"reuse-{kind}"))[0]
    task.spec = task.spec.model_copy(
        update={
            "image": ImageSpec(kind=kind),
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            ),
        }
    )
    task.prepared_image = _prepared(kind)
    result = await prepare_verifier_image_result(task, _Registry())  # type: ignore[arg-type]
    assert result is not None
    assert result.image is task.prepared_image
    assert result.image.kind is kind
    assert result.steps[-1].outcome == "reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(ImageKind))
async def test_external_separate_verifier_uses_matching_provider(
    tmp_path: Path, kind: ImageKind, framework_layer: None
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"external-{kind}"))[0]
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "image": {"kind": kind, "ref": "ghcr.io/acme/verifier:v1"},
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            )
        }
    )
    task.prepared_image = _prepared(ImageKind.CONTAINER)
    registry = _Registry()
    result = await prepare_verifier_image_result(task, registry)  # type: ignore[arg-type]
    assert result is not None and result.image.kind is kind
    assert registry.get(kind).inputs == [ImageRef(kind=kind, reference="ghcr.io/acme/verifier:v1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(ImageKind))
async def test_local_separate_verifier_builds_declared_kind(
    tmp_path: Path, kind: ImageKind, monkeypatch: pytest.MonkeyPatch, framework_layer: None
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"local-{kind}"))[0]
    (task.folder.root / "tests/Dockerfile").write_text("FROM fixture\n")
    task.spec = task.spec.model_copy(
        update={
            "verify": VerifySpec.model_validate(
                {
                    "environment_mode": "separate",
                    "image": {"kind": kind},
                    "resources": {
                        "cpus": 1,
                        "memory_mb": 512,
                        "storage_mb": None,
                        "gpus": 0,
                    },
                }
            )
        }
    )
    task.verifier_image_source_digest = DIGEST
    build = _OciBuild(DIGEST, DIGEST, "ale:verifier", DIGEST, ())

    async def build_oci(**_kwargs: object) -> _OciBuild:
        return build

    monkeypatch.setattr("ale.run.task_images._build_oci", build_oci)
    if kind is ImageKind.VM:

        async def materialize(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return _prepared(ImageKind.VM, "verifier-local"), False

        monkeypatch.setattr(
            "ale.run.task_images._materialize_vm_with_status",
            materialize,
        )
    registry = _Registry()
    result = await prepare_verifier_image_result(task, registry)  # type: ignore[arg-type]
    assert result is not None
    assert result.image.kind is kind
    assert result.image.source == "verifier-local"


@pytest.mark.asyncio
async def test_task_local_helper_and_framework_package_are_importable(tmp_path: Path) -> None:
    task_path = scaffold_task(tmp_path / "local-helper")
    (task_path / "tests" / "helper.py").write_text(
        "from ale_verify import checks\n"
        "\n"
        "def result():\n"
        "    return checks.text_equals('/home/user/output/result.txt', 'hello\\n')\n"
    )
    (task_path / "tests" / "verify.py").write_text(
        "from ale_verify import write\n"
        "from helper import result\n"
        "v = {}\n"
        "v['local'] = result()['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task_path, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_solution_assets_are_staged_with_the_solution(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "solution-assets")
    asset = task / "solution/assets/result.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("hello\n")
    (task / "solution/solve.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cp assets/result.txt /home/user/output/result.txt\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_tests_assets_are_withheld_then_available_by_relative_path(
    tmp_path: Path,
) -> None:
    _, task = task_repository(tmp_path, "external-reference")
    setup = task / "environment/setup"
    setup.mkdir()
    (setup / "setup.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ntest ! -e /opt/ale/tests/assets/reference.txt\n"
    )
    (setup / "setup.sh").chmod(0o755)
    reference = task / "tests/assets/reference.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("withheld")
    (task / "tests" / "verify.py").write_text(
        "from ale_verify import checks, write\n"
        "v = {}\n"
        "v['reference'] = checks.text_equals('assets/reference.txt', 'withheld')['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_no_assets_verifier_does_not_receive_a_synthetic_asset_root(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "no-assets")
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "v = {}\n"
        "v['absent'] = float(not Path('assets').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
async def test_missing_requested_verify_asset_is_a_task_error_not_zero(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "missing-asset")
    (task / "tests" / "verify.py").write_text(
        "from pathlib import Path\nPath('assets/missing.json').read_text()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.TASK_ERROR
    assert result.verdict.rewards is None
    assert "verify failed with exit code" in result.verdict.failure.message


@pytest.mark.asyncio
async def test_separate_verifier_restores_file_and_directory_to_exact_paths(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "separate")
    edit_task_config(
        task,
        {
            "artifacts": ["/home/user/output/result.json", "/var/lib/ale-report"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment": {"cpus": 1, "memory_mb": 512}},
        },
    )
    (task / "instruction.md").write_text("Produce both declared outputs.\n")
    (task / "environment/Dockerfile").write_text(
        f"FROM {CONTAINER_BASE}\n"
        "RUN mkdir -p /home/user/output /var/lib/ale-report "
        "&& printf '{}\\n' > /home/user/output/result.json "
        "&& chown -R user:user /home/user/output /var/lib/ale-report\n"
    )
    (task / "tests/Dockerfile").write_text(f"FROM {CONTAINER_BASE}\n")
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        'printf \'{"status":"ok"}\\n\' > /home/user/output/result.json\n'
        "printf ready > /var/lib/ale-report/state.txt\n"
    )
    (task / "tests/verify.py").write_text(
        "from ale_verify import checks, write\n"
        "v = {}\n"
        "v['file'] = checks.json_value('/home/user/output/result.json', 'status', 'ok')['score']\n"
        "v['directory'] = checks.text_equals('/var/lib/ale-report/state.txt', 'ready')['score']\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )

    result = await execute(task, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    assert [outcome.roles for outcome in result.record.sandboxes] == [
        ("solver",),
        ("verifier",),
    ]
    snapshot = result.run_dir / "artifacts/snapshot.json"
    assert snapshot.is_file()
    assert '"source": "/var/lib/ale-report"' in snapshot.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["solver-image", "external-ref"])
async def test_separate_verifier_image_sources_restore_identically_without_setup(
    tmp_path: Path,
    named: bool,
) -> None:
    task = scaffold_task(tmp_path / ("separate-ref" if named else "separate-reuse"))
    verifier = {"cpus": 1, "memory_mb": 512} | ({"docker_image": CONTAINER_BASE} if named else {})
    edit_task_config(
        task,
        {
            "artifacts": ["/home/user/output/result.txt"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment": verifier},
        },
    )
    (task / "instruction.md").write_text("Write the requested output.\n")
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir()
    setup.write_text("#!/bin/sh\nset -eu\ntouch /tmp/solver-setup-ran\n")
    setup.chmod(0o755)
    (task / "solution/solve.sh").write_text(
        "#!/bin/sh\nset -eu\nprintf 'hello\\n' > /home/user/output/result.txt\n"
    )
    (task / "tests/verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import checks, write\n"
        "v = {}\n"
        "v['content'] = checks.text_equals('/home/user/output/result.txt', 'hello\\n')['score']\n"
        "v['fresh'] = float(not Path('/tmp/solver-setup-ran').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )

    result = await execute(task, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "separate"])
async def test_privileged_artifacts_glob_and_never_follow_links(tmp_path, mode):
    task = scaffold_task(tmp_path / f"copy-{mode}")
    (task / "instruction.md").write_text("Leave the prepared outputs for verification.\n")
    edit_task_config(
        task,
        {
            "artifacts": ["/root/answers/*.sgf", "/root/missing", "/root/answers/result.json"],
            "environment": {"memory_mb": 512},
            "verifier": {"environment_mode": mode},
        },
    )
    setup = task / "environment/setup/setup.sh"
    setup.parent.mkdir()
    setup.write_text(
        "#!/bin/sh\nset -eu\n"
        "mkdir -p /root/answers/result.json /root/private\n"
        "printf protected > /root/private/value\n"
        "chmod 000 /root/private/value\n"
        "ln -s /root/private/value /root/answers/game.sgf\n"
        "ln -s /root/missing /root/answers/broken.sgf\n"
        "ln -s /root/answers/result.json /root/answers/result.json/loop\n"
    )
    setup.chmod(0o755)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nset -eu\n[ $(id -u) -ne 0 ]\n")
    (task / "tests/verify.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from ale_verify import write\n"
        "assert os.geteuid() == 0\n"
        "v = {}\n"
        # A link the agent left is never followed, so the protected file it points at
        # cannot reach a separate verifier; a shared verifier sees the sandbox as left.
        + (
            "v['link'] = float(not os.path.lexists('/root/answers/game.sgf'))\n"
            if mode == "separate"
            else "v['link'] = float(os.path.islink('/root/answers/game.sgf'))\n"
        )
        + "v['directory'] = float(Path('/root/answers/result.json').is_dir())\n"
        "v['missing'] = float(not Path('/root/missing').exists())\n"
        "v['reward'] = sum(v.values()) / len(v.values())\n"
        "write(v)\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reward": 1.0}
    entries = json.loads((result.run_dir / "artifacts/snapshot.json").read_text())["entries"]
    assert not any(
        entry["source"].endswith(("game.sgf", "broken.sgf", "/loop")) for entry in entries
    )
