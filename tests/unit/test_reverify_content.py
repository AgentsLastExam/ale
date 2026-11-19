import os
from pathlib import Path

import pytest
from tests.support import edit_task_config

from ale.core.result import SandboxOutcome
from ale.run.scaffold import scaffold_task
from ale.run.tasksets.manifest import load_tasks

pytestmark = pytest.mark.unit


def _kept(handle: str) -> SandboxOutcome:
    return SandboxOutcome(
        roles=("solver",),
        requested="keep",
        outcome="retained",
        provider=handle.partition(":")[0],
        handle=handle,
    )


@pytest.mark.parametrize(
    "relative",
    [
        "instruction.md",
        "environment/Dockerfile",
        "environment/setup/setup.sh",
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


def test_calibration_cases_preserve_source_identity(tmp_path: Path):
    """No case, the reference solution included, reaches an evaluated agent, so a cheating
    case added while tests/ is fixed leaves the runs that exposed the cheat reverifiable."""
    root = scaffold_task(tmp_path / "task")
    before = load_tasks(root)[0].non_verifier_digest
    (root / "calibration/solution/solve.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    edit_task_config(
        root,
        {
            "solution": {"env": {"REGION": "eu"}},
            "calibration": [{"name": "hardcoded-output", "expect": {"reward": 0}}],
        },
    )
    (root / "calibration/hardcoded-output").mkdir(parents=True)
    (root / "calibration/hardcoded-output/solve.sh").write_text("true\n")
    assert load_tasks(root)[0].non_verifier_digest == before
    edit_task_config(root, {"agent": {"timeout_sec": 301}})
    assert load_tasks(root)[0].non_verifier_digest != before


@pytest.mark.parametrize(
    "relative",
    [
        "tests/test.sh",
        "tests/assets/input",
        "environment/assets/input",
        "calibration/solution/assets/input",
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
        lambda _: (
            (tmp_path / "source", SimpleNamespace(episode_id="old"), lock, _kept("docker:old")),
        ),
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
        lambda _: (
            (tmp_path / "source", SimpleNamespace(episode_id="old"), lock, _kept("qemu:old")),
        ),
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


@pytest.mark.asyncio
async def test_reverify_fails_only_the_task_whose_image_no_registry_answers_for(
    tmp_path, monkeypatch, capsys
):
    """One unreachable image is that Task's failure, not the whole reverification's."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.unit.test_runtime_contracts import make_lock

    from ale.core.config import RunConfig
    from ale.core.errors import ProviderStartError
    from ale.run.cli import tasks as task_cli

    root = scaffold_task(tmp_path / "task")
    config = root / "task.toml"
    config.write_text(
        config.read_text().replace("[verifier]\n", '[verifier]\nenvironment_mode = "separate"\n')
        + '\n[verifier.environment]\nimage = "ghcr.io/acme/gone:1"\n'
    )
    task = load_tasks(root)[0]
    lock = make_lock()
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={"name": task.spec.name, "non_verifier_digest": task.non_verifier_digest}
            ),
            "image": lock.image.model_copy(update={"declaration": task.spec.image}),
        }
    )
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: (
            (tmp_path / "source", SimpleNamespace(episode_id="old"), lock, _kept("docker:old")),
        ),
    )

    async def unreachable(reference: str) -> object:
        raise ProviderStartError(f"cannot read the manifest of {reference}")

    monkeypatch.setattr("ale.run.task_images.classify_image", unreachable)
    prepare = AsyncMock(side_effect=AssertionError("its verifier was prepared"))
    monkeypatch.setattr(task_cli, "prepare_verifier_image_result", prepare)
    code = await task_cli._reverify(str(root), tmp_path / "source", RunConfig(), tmp_path / "runs")
    assert code == task_cli.EXIT_SOME_FAILED
    prepare.assert_not_called()
    (refusal,) = capsys.readouterr().err.splitlines()
    assert refusal.startswith(f"{task.spec.name}: ")
    assert refusal.endswith("cannot read the manifest of ghcr.io/acme/gone:1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "kept", "refusal"),
    [
        ("snapshot", "retained", "kept its solver running instead"),
        ("snapshot", "unserved", "its verifier boots from the snapshot docker:runc keeps"),
        ("separate", "shared", "kept the Sandbox its shared verifier ran in"),
        ("snapshot", "shared-snapshot", "kept the Sandbox its shared verifier ran in"),
    ],
)
async def test_reverify_refuses_a_source_its_verification_cannot_use(
    tmp_path, monkeypatch, capsys, mode, kept, refusal
):
    """A snapshot verifier needs its source's solver snapshot, on a Provider that serves it,
    and shared verification the solver running; a separate verifier takes either. A verifier
    of its own needs the solver as its agent left it, not a Sandbox a shared verifier
    changed."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.support import edit_task_config
    from tests.unit.test_runtime_contracts import make_lock

    from ale.core.config import RunConfig
    from ale.run.cli import tasks as task_cli

    root = scaffold_task(tmp_path / "task")
    # docker:runc gives no perf, which this snapshot verifier asks for.
    environment = {"container_features": ["perf"]} if kept == "unserved" else {"cpus": 1}
    edit_task_config(root, {"verifier": {"environment_mode": mode, "environment": environment}})
    task = load_tasks(root)[0]
    lock = make_lock()
    lock = lock.model_copy(
        update={
            "task": lock.task.model_copy(
                update={"name": task.spec.name, "non_verifier_digest": task.non_verifier_digest}
            ),
            "image": lock.image.model_copy(update={"declaration": task.spec.image}),
        }
    )
    running = kept in {"retained", "shared"}
    outcome = SandboxOutcome(
        roles=("solver", "verifier") if kept.startswith("shared") else ("solver",),
        requested="keep" if running else "snapshot",
        outcome="retained" if running else "snapshotted",
        provider="docker",
        handle="docker:old" if running else "docker:snapshot:old",
    )
    monkeypatch.setattr(
        task_cli,
        "_retained_source_episodes",
        lambda _: ((tmp_path / "source", SimpleNamespace(episode_id="old"), lock, outcome),),
    )
    monkeypatch.setattr(task_cli, "observe_task_assets", lambda task: None)
    attach = AsyncMock(side_effect=AssertionError("must reject before attachment"))
    monkeypatch.setattr(task_cli, "_attach_retained", attach)
    episode = AsyncMock(side_effect=AssertionError("must reject before any episode"))
    monkeypatch.setattr(task_cli, "run_episode", episode)
    code = await task_cli._reverify(str(root), tmp_path / "source", RunConfig(), tmp_path / "runs")
    assert code == task_cli.EXIT_SOME_FAILED
    attach.assert_not_called()
    episode.assert_not_called()
    assert refusal in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["snapshot", "separate"])
async def test_a_fleet_solver_is_reverified_with_the_image_its_nodes_keep(tmp_path, mode):
    """The Fleet prepares images on its Nodes, under the key the Run gives them: a solver
    of the Fleet's, reused by a verifier that boots from its snapshot or from its image, is
    named by that key again, which the Fleet then accepts."""
    from types import SimpleNamespace

    from tests.support import edit_task_config
    from tests.unit.test_runtime_contracts import make_lock

    from ale.core.config import RunConfig
    from ale.run.cli import tasks as task_cli
    from ale.run.providers import ProviderRegistry
    from ale.run.providers.fleet import FleetProvider
    from ale.run.task_images import prepare_verifier_image_result

    root = scaffold_task(tmp_path / "task")
    edit_task_config(root, {"verifier": {"environment_mode": mode, "environment": {"cpus": 1}}})
    task = load_tasks(root)[0]
    fleet = FleetProvider(SimpleNamespace(recipes={}))  # type: ignore[arg-type]
    registry = ProviderRegistry(RunConfig())
    registry._instances["fleet"] = fleet
    task.provider = task.verifier_provider = "fleet"
    image = await task_cli._reused_solver_image(task, make_lock(), registry)
    assert await fleet.prepare_image(image) is image
    task.prepared_image = image
    reused = await prepare_verifier_image_result(task, fleet)
    assert reused is not None and await fleet.prepare_image(reused.image) is image
