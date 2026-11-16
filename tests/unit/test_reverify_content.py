import os
from pathlib import Path

import pytest

from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "relative",
    [
        "instruction.md",
        "task.toml",
        "environment/Dockerfile",
        "environment/setup/setup.sh",
        "solution/solve.sh",
        "other.txt",
    ],
)
def test_non_verifier_change_requires_new_solver(tmp_path: Path, relative: str):
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(target.read_text() + "\n# changed\n" if target.exists() else "new")
    assert load_tasks(root)[0].non_verifier_digest != before


@pytest.mark.parametrize(
    "relative",
    [
        "tests/test.sh",
        "tests/assets/input",
        "environment/assets/input",
        "solution/assets/input",
    ],
)
def test_verifier_and_asset_changes_preserve_source_identity(tmp_path: Path, relative: str):
    """Asset bytes are identified by their commit, which reverification compares separately."""
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new verifier material")
    assert load_tasks(root)[0].non_verifier_digest == before


def test_loading_a_task_never_reads_asset_bytes(tmp_path: Path):
    root = scaffold_task(tmp_path / "task")
    unreadable = root / "environment" / "assets" / "large.bin"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b"x")
    unreadable.chmod(0)
    try:
        task = load_tasks(root)[0]
        assert task.non_verifier_digest.startswith("sha256:")
        assert task.task_digest.startswith("sha256:")
    finally:
        unreadable.chmod(0o600)


def test_timestamps_do_not_change_content_identity(tmp_path: Path):
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    os.utime(root / "environment/Dockerfile", (1, 1))
    assert load_tasks(root)[0].non_verifier_digest == before
    (root / "instruction.md").chmod(0o755)
    assert load_tasks(root)[0].non_verifier_digest != before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["outside", "missing_record", "image", "assets"])
async def test_reverify_refuses_changed_or_unrecorded_inputs_before_attaching(
    tmp_path, monkeypatch, capsys, change
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.unit.test_runtime_contracts import make_lock

    from ale.core.config import RunConfig
    from ale.run.cli import tasks as task_cli

    root = scaffold_task(tmp_path / "task")
    task = load_tasks(root)[0]
    lock = make_lock()
    declaration = task.spec.image
    if change == "image":
        # Declared otherwise when the solver ran, though no Task file changed since.
        declaration = declaration.model_copy(update={"user": "other"})
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={
                    "name": task.spec.name,
                    "non_verifier_digest": None
                    if change == "missing_record"
                    else task.non_verifier_digest,
                }
            ),
            "image": lock.image.model_copy(update={"declaration": declaration}),
        }
    )
    if change == "outside":
        instruction = root / "instruction.md"
        instruction.write_text(instruction.read_text() + "\nDifferent solver task")
    if change == "assets":
        # The source run had no assets; uncommitted ones cannot be proven unchanged.
        asset = root / "environment" / "assets" / "seed.json"
        asset.parent.mkdir(parents=True)
        asset.write_text("{}")
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: ((tmp_path / "source", SimpleNamespace(episode_id="old"), lock, "docker:old"),),
    )
    attach = AsyncMock(side_effect=AssertionError("must reject before attachment"))
    monkeypatch.setattr(task_cli, "_attach_retained", attach)
    code = await task_cli._reverify(str(root), tmp_path / "source", RunConfig(), tmp_path / "runs")
    assert code == 2
    attach.assert_not_called()
    refusal = {
        "outside": "content outside tests/ changed",
        "missing_record": "content outside tests/ changed",
        "image": "its image differs from the source run's",
        "assets": "Task assets differ from the source run",
    }
    assert refusal[change] in capsys.readouterr().err


@pytest.mark.asyncio
async def test_reverify_takes_the_solver_kind_from_its_run_and_reads_only_the_verifier(
    tmp_path, monkeypatch
):
    """A reused solver is not prepared again, so its registry is not read either: the
    tag may be gone, or name another kind of image by now."""
    from types import SimpleNamespace

    from tests.unit.test_runtime_contracts import DIGEST, make_lock

    from ale.core.config import RunConfig
    from ale.core.errors import ProviderStartError
    from ale.core.taskspec import ImageKind
    from ale.run.cli import tasks as task_cli

    root = scaffold_task(tmp_path / "task")
    (root / "environment/Dockerfile").unlink()
    config = root / "task.toml"
    config.write_text(
        config.read_text()
        .replace("[environment]\n", '[environment]\nimage = "ghcr.io/acme/solver:1"\n')
        .replace("[verifier]\n", '[verifier]\nenvironment_mode = "separate"\n')
        + '\n[verifier.environment]\nimage = "ghcr.io/acme/verifier:1"\n'
    )
    task = load_tasks(root)[0]
    assert task.spec.image.kind is None and task.spec.verify.image is not None
    recorded = task.spec.image.model_copy(update={"kind": ImageKind.VM})
    lock = make_lock()
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={"name": task.spec.name, "non_verifier_digest": task.non_verifier_digest}
            ),
            "image": lock.image.model_copy(
                update={
                    "declaration": recorded,
                    "source": "ref",
                    "image_source_identity": None,
                    "resolved_reference": f"ghcr.io/acme/solver@{DIGEST}",
                }
            ),
        }
    )
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: ((tmp_path / "source", SimpleNamespace(episode_id="old"), lock, "qemu:old"),),
    )
    monkeypatch.setattr(task_cli, "observe_task_assets", lambda task: None)
    read: list[str] = []

    async def classify(reference: str) -> ImageKind:
        read.append(reference)
        return ImageKind.CONTAINER

    prepared: list[tuple[object, object, object]] = []

    async def prepare_verifier(task, providers):  # type: ignore[no-untyped-def]
        prepared.append((task.spec.image, task.spec.verify.image.kind, task.prepared_image.kind))
        raise ProviderStartError("stop before the verifier starts")

    monkeypatch.setattr("ale.run.task_images.classify_image", classify)
    monkeypatch.setattr(task_cli, "prepare_verifier_image_result", prepare_verifier)
    code = await task_cli._reverify(str(root), tmp_path / "source", RunConfig(), tmp_path / "runs")
    assert code == 2
    assert read == ["ghcr.io/acme/verifier:1"]
    assert prepared == [(recorded, ImageKind.CONTAINER, ImageKind.VM)]
