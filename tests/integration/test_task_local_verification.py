from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ale.core.errors import ArtifactTransferError
from ale.core.sandbox import ExecResult, ImageRef, PreparedTaskImage
from ale.core.taskspec import ImageKind, ImageSpec, VerifySpec
from ale.core.verdict import Status
from ale.run.environments.standard import StandardEnvironment
from ale.run.episode import _artifact_tree_identity, _Artifacts, run_episode
from ale.run.harnesses.builtin import OracleHarness
from ale.run.providers.docker import DockerProvider
from ale.run.scaffold import scaffold_task
from ale.run.task_images import (
    _OciBuild,
    prepare_verifier_image_result,
)
from ale.run.tasksets import load_tasks
from tests.support import provider_registry

pytestmark = [pytest.mark.integration, pytest.mark.needs_docker]

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
        StandardEnvironment(OracleHarness()),
        provider_registry(DockerProvider()),
        run_dir=run_dir,
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
    tmp_path: Path, kind: ImageKind
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
    tmp_path: Path, kind: ImageKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = load_tasks(scaffold_task(tmp_path / f"local-{kind}"))[0]
    (task.folder.root / "verify/Dockerfile").write_text("FROM fixture\n")
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


class ArtifactSandbox:
    def __init__(
        self,
        *,
        kind: str = "file",
        mode: int = 0o640,
        data: bytes = b"artifact",
        missing: bool = False,
        fail_read: bool = False,
    ) -> None:
        self.kind = kind
        self.mode = mode
        self.data = data
        self.missing = missing
        self.fail_read = fail_read
        self.writes: list[tuple[str, bytes]] = []
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []

    async def exec(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        command = [str(item) for item in argv]
        self.commands.append(command)
        if command[0] == "python3":
            if self.missing:
                return ExecResult(exit_code=1, stderr="missing")
            return ExecResult(
                exit_code=0,
                stdout=json.dumps({"kind": self.kind, "mode": self.mode}),
            )
        return ExecResult(exit_code=0)

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        if self.fail_read:
            raise OSError("transfer failed")
        return self.data

    async def write_file(self, path, data):  # type: ignore[no-untyped-def]
        self.writes.append((str(path), data))

    async def download_dir(self, source, target):  # type: ignore[no-untyped-def]
        root = Path(target)
        (root / "child").mkdir()
        (root / "child/data.txt").write_bytes(self.data)

    async def upload_dir(self, source, target):  # type: ignore[no-untyped-def]
        self.uploads.append((str(source), str(target)))


@pytest.mark.asyncio
async def test_artifact_file_snapshot_restores_exact_absolute_path_and_mode(
    tmp_path: Path,
) -> None:
    sandbox = ArtifactSandbox(mode=0o600)
    sink = _Artifacts(tmp_path)

    await sink.collect(sandbox, "/etc/ale/result.json", "result.json")
    await sink.restore(sandbox)

    assert sandbox.writes == [("/etc/ale/result.json", b"artifact")]
    assert ["chmod", "600", "--", "/etc/ale/result.json"] in sandbox.commands
    manifest = json.loads((tmp_path / "artifacts/snapshot.json").read_text())
    assert manifest["entries"][0]["source"] == "/etc/ale/result.json"


@pytest.mark.asyncio
async def test_artifact_directory_snapshot_and_restore(tmp_path: Path) -> None:
    sandbox = ArtifactSandbox(kind="directory", mode=0o750)
    sink = _Artifacts(tmp_path)

    await sink.collect(sandbox, "/var/lib/ale-report", "ale-report")
    await sink.restore(sandbox)

    assert sandbox.uploads == [(str(tmp_path / "artifacts/ale-report"), "/var/lib/ale-report")]
    assert ["chmod", "750", "--", "/var/lib/ale-report"] in sandbox.commands


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sandbox", "message"),
    [
        (ArtifactSandbox(missing=True), "missing"),
        (ArtifactSandbox(kind="symlink"), "unsupported symlink"),
        (ArtifactSandbox(kind="special"), "unsupported special"),
        (ArtifactSandbox(fail_read=True), "transfer failed"),
    ],
)
async def test_invalid_or_failed_artifact_capture_is_explicit(
    tmp_path: Path,
    sandbox: ArtifactSandbox,
    message: str,
) -> None:
    with pytest.raises(ArtifactTransferError, match=message):
        await _Artifacts(tmp_path).collect(sandbox, "/absolute/output", "output")


def test_artifact_directory_rejects_inner_symlinks_and_special_files(tmp_path: Path) -> None:
    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    (symlink_root / "regular").write_text("data")
    (symlink_root / "link").symlink_to("regular")
    with pytest.raises(ArtifactTransferError, match="symlink"):
        _artifact_tree_identity(symlink_root)

    special_root = tmp_path / "special"
    special_root.mkdir()
    os.mkfifo(special_root / "pipe")
    with pytest.raises(ArtifactTransferError, match="special"):
        _artifact_tree_identity(special_root)


@pytest.mark.asyncio
async def test_task_local_helper_and_framework_package_are_importable(tmp_path: Path) -> None:
    task_path = scaffold_task(tmp_path / "local-helper")
    (task_path / "verify" / "helper.py").write_text(
        "from ale_verify import checks\n"
        "def result():\n"
        "    return checks.text_equals('/home/user/output/result.txt', 'hello\\n')\n"
    )
    (task_path / "verify" / "verify.py").write_text(
        "from ale_verify import Verification\n"
        "from helper import result\n"
        "v = Verification()\n"
        "v.check('local', result())\n"
        "v.write()\n"
    )
    result = await execute(task_path, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"local": 1.0}


@pytest.mark.asyncio
async def test_oracle_assets_are_staged_with_the_oracle(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "oracle-assets")
    asset = task / "oracle/assets/result.txt"
    asset.parent.mkdir(parents=True)
    asset.write_text("hello\n")
    (task / "oracle/run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cp assets/result.txt /home/user/output/result.txt\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"content": 1.0, "overall": 1.0}


@pytest.mark.asyncio
async def test_verify_assets_are_withheld_then_available_by_relative_path(
    tmp_path: Path,
) -> None:
    _, task = task_repository(tmp_path, "external-reference")
    setup = task / "setup"
    setup.mkdir()
    (setup / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ntest ! -e /opt/ale/verify/assets/reference.txt\n"
    )
    (setup / "run.sh").chmod(0o755)
    reference = task / "verify/assets/reference.txt"
    reference.parent.mkdir(parents=True)
    reference.write_text("withheld")
    (task / "verify" / "verify.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('reference', checks.text_equals('assets/reference.txt', 'withheld'))\n"
        "v.write()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"reference": 1.0}


@pytest.mark.asyncio
async def test_no_assets_verifier_does_not_receive_a_synthetic_asset_root(tmp_path: Path) -> None:
    task = scaffold_task(tmp_path / "no-assets")
    (task / "verify" / "verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import CheckResult, Verification\n"
        "v = Verification()\n"
        "v.check('absent', CheckResult(float(not Path('assets').exists())))\n"
        "v.write()\n"
    )
    result = await execute(task, tmp_path / "runs")
    assert result.verdict.rewards == {"absent": 1.0}


@pytest.mark.asyncio
async def test_missing_requested_verify_asset_is_a_task_error_not_zero(
    tmp_path: Path,
) -> None:
    task = scaffold_task(tmp_path / "missing-asset")
    (task / "verify" / "verify.py").write_text(
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
    (task / "task.yaml").write_text(
        "spec_type: core/v1\n"
        "name: separate\n"
        "image: {kind: container}\n"
        "resources: {cpus: 1, memory_mb: 512}\n"
        "artifacts: [/home/user/output/result.json, /var/lib/ale-report]\n"
        "verify:\n"
        "  environment_mode: separate\n"
        "  image: {kind: container}\n"
        "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    (task / "instruction.md").write_text("Produce both declared outputs.\n")
    (task / "image/Dockerfile").write_text(
        "FROM ghcr.io/agentslastexam/sandbox-base-cli:latest\n"
        "RUN mkdir -p /home/user/output /var/lib/ale-report "
        "&& printf '{}\\n' > /home/user/output/result.json "
        "&& chown -R user:user /home/user/output /var/lib/ale-report\n"
    )
    (task / "verify/Dockerfile").write_text("FROM ghcr.io/agentslastexam/sandbox-base-cli:latest\n")
    (task / "oracle/run.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        'printf \'{"status":"ok"}\\n\' > /home/user/output/result.json\n'
        "printf ready > /var/lib/ale-report/state.txt\n"
    )
    (task / "verify/verify.py").write_text(
        "from ale_verify import Verification, checks\n"
        "v = Verification()\n"
        "v.check('file', checks.json_value('/home/user/output/result.json', 'status', 'ok'))\n"
        "v.check('directory', checks.text_equals('/var/lib/ale-report/state.txt', 'ready'))\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
    )

    result = await execute(task, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"file": 1.0, "directory": 1.0, "overall": 1.0}
    assert [outcome.roles for outcome in result.record.sandboxes] == [
        ("solver",),
        ("verifier",),
    ]
    snapshot = result.run_dir / "artifacts/snapshot.json"
    assert snapshot.is_file()
    assert '"source": "/var/lib/ale-report"' in snapshot.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_line",
    [
        "",
        "  image:\n    kind: container\n    ref: ghcr.io/agentslastexam/sandbox-base-cli:latest\n",
    ],
    ids=["solver-image", "external-ref"],
)
async def test_separate_verifier_image_sources_restore_identically_without_setup(
    tmp_path: Path,
    image_line: str,
) -> None:
    task = scaffold_task(tmp_path / ("separate-ref" if image_line else "separate-reuse"))
    (task / "task.yaml").write_text(
        "spec_type: core/v1\n"
        "name: separate-source\n"
        "image: {kind: container}\n"
        "resources: {cpus: 1, memory_mb: 512}\n"
        "artifacts: [/home/user/output/result.txt]\n"
        "verify:\n"
        "  environment_mode: separate\n"
        f"{image_line}"
        "  resources: {cpus: 1, memory_mb: 512, storage_mb: null, gpus: 0}\n"
    )
    (task / "instruction.md").write_text("Write the requested output.\n")
    setup = task / "setup/run.sh"
    setup.parent.mkdir()
    setup.write_text("#!/bin/sh\nset -eu\ntouch /tmp/solver-setup-ran\n")
    setup.chmod(0o755)
    (task / "oracle/run.sh").write_text(
        "#!/bin/sh\nset -eu\nprintf 'hello\\n' > /home/user/output/result.txt\n"
    )
    (task / "verify/verify.py").write_text(
        "from pathlib import Path\n"
        "from ale_verify import CheckResult, Verification, checks\n"
        "v = Verification()\n"
        "v.check('content', checks.text_equals('/home/user/output/result.txt', 'hello\\n'))\n"
        "v.check('fresh', CheckResult(float(not Path('/tmp/solver-setup-ran').exists())))\n"
        "v.aggregate('overall')\n"
        "v.write()\n"
    )

    result = await execute(task, tmp_path / "runs")

    assert result.verdict.status is Status.COMPLETED, result.verdict.failure
    assert result.verdict.rewards == {"content": 1.0, "fresh": 1.0, "overall": 1.0}
